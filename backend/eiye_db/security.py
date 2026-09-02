"""API-key authentication."""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone

from fastapi import Header, HTTPException

from eiye_db.config import NamedKey, settings

INVALID_KEY = "Invalid or missing API key"

# key ids the two single-key settings occupy. The named map may not reuse one
# while its setting is live (refused at boot in main.lifespan).
RESERVED_KEY_IDS = ("primary", "admin")


@dataclass
class Identity:
    key_id: str
    is_admin: bool


def auth_configured() -> bool:
    """True once *any* credential setting is present.

    Dev mode is the absence of all three, not of EIYE_API_KEY alone. Keying it
    on one setting would make a deployment secured by either of the others read
    as unconfigured -- fully open and fully admin -- which silently voids every
    `is_admin` gate in the API.
    """
    return settings.api_key is not None or settings.admin_api_key is not None or bool(settings.api_keys)


def _matches(presented: str, expected: str) -> bool:
    """Constant-time comparison. `==` returns as soon as two bytes differ, and
    that timing difference recovers a secret one byte at a time."""
    return secrets.compare_digest(presented.encode(), expected.encode())


def _named_key(presented: str) -> tuple[str, NamedKey] | None:
    digest = hashlib.sha256(presented.encode()).hexdigest()
    for key_id, entry in settings.api_keys.items():
        if secrets.compare_digest(digest, entry.sha256):
            return key_id, entry
    return None


def require_api_key(x_api_key: str | None = Header(None)) -> Identity:
    """FastAPI dependency. Open dev mode requires *every* key setting to be unset.

    Boot refuses the half-configured states outright (main.lifespan); this is
    the same rule enforced per request. The two single-key settings resolve
    first and keep their reserved ids, so an existing deployment authenticates
    exactly as it did before the named map existed.
    """
    if not auth_configured():
        return Identity(key_id="dev", is_admin=True)
    if not x_api_key:
        # Empty, not just absent: an empty setting compares equal to an empty
        # header, so `EIYE_API_KEY=` would otherwise admit a caller sending one.
        raise HTTPException(status_code=401, detail=INVALID_KEY)
    if settings.admin_api_key is not None and _matches(x_api_key, settings.admin_api_key):
        return Identity(key_id="admin", is_admin=True)
    if settings.api_key is not None and _matches(x_api_key, settings.api_key):
        return Identity(key_id="primary", is_admin=False)
    found = _named_key(x_api_key)
    if found is None:
        raise HTTPException(status_code=401, detail=INVALID_KEY)
    key_id, entry = found
    if entry.expires_at is not None and entry.expires_at <= datetime.now(timezone.utc):
        # Distinct from "invalid" on purpose: an expired credential is a
        # rotation that did not happen, and reporting it as a bad key sends the
        # operator hunting for a typo that is not there.
        raise HTTPException(
            status_code=401,
            detail=f"API key '{key_id}' expired at {entry.expires_at.isoformat()}",
        )
    return Identity(key_id=key_id, is_admin=entry.is_admin)
