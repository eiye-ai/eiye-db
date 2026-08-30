"""API-key authentication."""

from dataclasses import dataclass

from fastapi import Header, HTTPException

from eiye_db.config import settings


@dataclass
class Identity:
    key_id: str
    is_admin: bool


def require_api_key(x_api_key: str | None = Header(None)) -> Identity:
    """FastAPI dependency. Open dev mode requires *both* keys to be unset.

    Keying dev mode on EIYE_API_KEY alone would make an admin-key-only
    deployment fully open and fully admin, which silently voids every
    `is_admin` gate in the API. Boot refuses that configuration outright
    (main.lifespan); this is the same rule enforced per request.
    """
    if settings.api_key is None and settings.admin_api_key is None:
        return Identity(key_id="dev", is_admin=True)
    if x_api_key is not None and settings.admin_api_key is not None and x_api_key == settings.admin_api_key:
        return Identity(key_id="admin", is_admin=True)
    if x_api_key is not None and x_api_key == settings.api_key:
        return Identity(key_id="primary", is_admin=False)
    raise HTTPException(status_code=401, detail="Invalid or missing API key")
