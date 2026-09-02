#!/usr/bin/env python3
"""Mint a named API key for the deployment's EIYE_API_KEYS map.

    python scripts/mint_key.py --id support-agent
    python scripts/mint_key.py --id ops --admin --expires 2027-01-01

The secret is printed once and stored nowhere. Only its SHA-256 goes into the
map, so a leaked config file or a shell history full of `env` output leaks no
working credential.

A plain SHA-256 is sound here only because this script chooses the secret:
`secrets.token_urlsafe(32)` is 256 bits, which is out of reach of brute force
at hashing speed. A passphrase a human picked is not, so do not hand-write a
digest of one into the map -- mint it here.

Each id becomes an ABAC subject (`subjects` in a policy) and the audit
principal on every row that key produces. Give one per agent; that is the whole
reason the map exists, since EIYE_API_KEY resolves every HTTP caller to
key_id="primary".
"""

import argparse
import hashlib
import json
import secrets
import sys
from datetime import datetime, timezone

RESERVED = ("primary", "admin")


def mint(key_id: str, is_admin: bool, expires: str | None) -> tuple[str, dict]:
    key = secrets.token_urlsafe(32)
    entry: dict = {"sha256": hashlib.sha256(key.encode()).hexdigest(), "is_admin": is_admin}
    if expires:
        entry["expires_at"] = datetime.strptime(expires, "%Y-%m-%d").replace(tzinfo=timezone.utc).isoformat()
    return key, {key_id: entry}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--id", required=True, help="key id: the ABAC subject and the audit principal")
    p.add_argument("--admin", action="store_true", help="grant the admin surface (which bypasses ABAC entirely)")
    p.add_argument("--expires", default=None, help="YYYY-MM-DD (UTC); omit for a key that does not expire")
    args = p.parse_args()

    if args.id in RESERVED:
        print(
            f"'{args.id}' is reserved for EIYE_API_KEY / EIYE_ADMIN_API_KEY; pick another id",
            file=sys.stderr,
        )
        return 2

    key, entry = mint(args.id, args.admin, args.expires)
    print("Secret — shown once, not recoverable. Copy it now:\n")
    print(f"    {key}\n")
    print("Then add this to EIYE_API_KEYS, merged into whatever the map already holds:\n")
    print(json.dumps(entry, indent=2))
    print("\nThe map is read at startup, so adding or revoking a key takes a restart.")
    if args.admin:
        print("This key is an admin: it bypasses ABAC and can read unredacted PII.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
