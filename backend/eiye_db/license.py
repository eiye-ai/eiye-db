"""Entitlements: what this deployment is licensed to do.

The license is a signed claim set, verified offline against a public key
embedded below. There is no phone-home: air-gapped and network-restricted
deployments are exactly the buyers, and a governance component that fails
because it could not reach a vendor endpoint is not one anybody approves.

**This is measurement, not DRM.** The source is available, so anyone determined
to patch out a check can. That is fine and expected: the enforcement that
matters is the license itself (BSL 1.1, see /LICENSE), under which exceeding the
Additional Use Grant is unlicensed use rather than a bypassed counter. This
module's job is to make usage *visible and measurable* so compliance is
checkable and honest customers stay inside their tier by accident rather than by
effort.

Two postures, deliberately different:

* **Unlicensed (free).** Limits come from the Additional Use Grant and are hard:
  5 datasources, 1,000 queries/calendar month. Exceeding them is outside the
  license, so the software declines rather than helping you breach it.
* **Licensed.** Datasource registration is capped (a create operation — refusing
  it denies nobody access to data they already have), but query overage warns
  and records instead of blocking. A paying customer is never taken offline
  mid-month by their own vendor; overage is a true-up conversation, not an
  outage.

Expiry degrades, never severs: new registrations and commercial features stop,
already-registered sources keep serving, and the audit trail and semantic-model
export stay readable no matter what. A customer may have a regulatory retention
obligation against that audit log — locking them out of it to force a renewal
would put them out of compliance, which is both wrong and the fastest way off an
approved-vendor list.
"""

import base64
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

# Ed25519 public key of the licensor. The matching private key never ships and
# never enters this repository; see scripts/issue_license.py.
LICENSOR_PUBLIC_KEY = "00AFxu6WyHWFT9U+NEA/ETMGXuecklgXti5EbQ1nTLk="

# The BSL 1.1 Additional Use Grant, in code. These MUST match /LICENSE — they
# are the same boundary expressed twice, and the licence text is authoritative.
FREE_MAX_DATASOURCES = 5
FREE_MAX_QUERIES_PER_MONTH = 1000

# Expired licences keep serving for this long before commercial features and new
# registrations stop. Renewal pressure should come from visible degradation, not
# from a surprise outage on a Friday.
GRACE_PERIOD_DAYS = 30


class LicenseError(Exception):
    """A licence file exists but cannot be trusted (bad signature, malformed)."""


class LicenseLimitExceeded(Exception):
    """The operation would exceed what this deployment is licensed for."""


@dataclass(frozen=True)
class Entitlements:
    tier: str
    max_datasources: int
    max_queries_per_month: int
    features: frozenset[str] = field(default_factory=frozenset)
    customer: str | None = None
    license_id: str | None = None
    expires_at: datetime | None = None

    @property
    def licensed(self) -> bool:
        """False for the free Additional Use Grant, True for a signed licence."""
        return self.license_id is not None

    @property
    def expired(self) -> bool:
        return self.expires_at is not None and _now() > self.expires_at

    @property
    def in_grace(self) -> bool:
        """Expired, but inside the window where everything still works."""
        if not self.expired:
            return False
        return _now() <= self.expires_at + timedelta(days=GRACE_PERIOD_DAYS)

    @property
    def degraded(self) -> bool:
        """Expired past grace: no new registrations, no commercial features."""
        return self.expired and not self.in_grace

    def has_feature(self, name: str) -> bool:
        return name in self.features and not self.degraded

    def summary(self) -> dict[str, Any]:
        """Licence state for /status and the UI. No signature material, and no
        customer name unless licensed — an unlicensed deployment has nothing to
        disclose."""
        out: dict[str, Any] = {
            "tier": self.tier,
            "licensed": self.licensed,
            "max_datasources": self.max_datasources,
            "max_queries_per_month": self.max_queries_per_month,
            "features": sorted(self.features),
        }
        if self.licensed:
            out |= {
                "customer": self.customer,
                "license_id": self.license_id,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "expired": self.expired,
                "in_grace": self.in_grace,
                "degraded": self.degraded,
            }
        return out


def _now() -> datetime:
    return datetime.now(timezone.utc)


FREE_TIER = Entitlements(
    tier="free",
    max_datasources=FREE_MAX_DATASOURCES,
    max_queries_per_month=FREE_MAX_QUERIES_PER_MONTH,
)


def _parse_expiry(raw: str | None) -> datetime | None:
    if not raw:
        return None
    parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    # A naive timestamp would compare-fail against aware ones at runtime; treat
    # it as UTC rather than raising, since licence files are hand-issued.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def verify(blob: str) -> Entitlements:
    """Verify a licence file's signature and return its entitlements.

    The signature covers the exact base64 payload bytes, not a re-serialization
    of the parsed claims — so a licence cannot be invalidated (or quietly
    altered) by JSON key ordering or whitespace.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        envelope = json.loads(blob)
        payload_b64 = envelope["claims"]
        signature = base64.b64decode(envelope["signature"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise LicenseError(f"malformed license file: {type(e).__name__}")

    key = Ed25519PublicKey.from_public_bytes(base64.b64decode(LICENSOR_PUBLIC_KEY))
    try:
        key.verify(signature, payload_b64.encode())
    except InvalidSignature:
        raise LicenseError("license signature is not valid for this build")

    try:
        claims = json.loads(base64.b64decode(payload_b64))
        return Entitlements(
            tier=str(claims["tier"]),
            max_datasources=int(claims["max_datasources"]),
            max_queries_per_month=int(claims["max_queries_per_month"]),
            features=frozenset(claims.get("features", [])),
            customer=claims.get("customer"),
            license_id=str(claims["license_id"]),
            expires_at=_parse_expiry(claims.get("expires_at")),
        )
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
        raise LicenseError(f"malformed license claims: {type(e).__name__}")


def load() -> Entitlements:
    """Current entitlements: the signed licence if configured, else free tier.

    Raises LicenseError if a licence *is* configured but unusable — an operator
    who paid and mis-deployed the file must find out at boot, not by silently
    running on free-tier limits and hitting them in production.
    """
    from eiye_db.config import settings

    if not settings.license_file:
        return FREE_TIER
    try:
        with open(settings.license_file) as fh:
            blob = fh.read()
    except OSError as e:
        raise LicenseError(f"cannot read license file {settings.license_file!r}: {e.strerror}")
    return verify(blob)


@lru_cache(maxsize=1)
def current() -> Entitlements:
    """Cached entitlements — the licence file is read once per process.

    Licences change on renewal, not per request, and this sits on the query hot
    path. `current.cache_clear()` after changing the file (or in tests), same
    idiom as `pii._load_ner`. Note that expiry is still evaluated live: the
    cached object computes `expired`/`degraded` from the clock on each access,
    so a licence lapses without a restart.
    """
    return load()
