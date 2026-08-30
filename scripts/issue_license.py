#!/usr/bin/env python3
"""Issue a signed eiye_db license file. Licensor-side tool — not for customers.

The Ed25519 private key must NEVER enter this repository or ship to a customer.
Keep it offline (password manager, HSM, or an encrypted volume) and pass it in:

    EIYE_LICENSE_SIGNING_KEY=$(cat /secure/eiye-signing.key) \\
      python scripts/issue_license.py \\
        --customer "Acme Corp" --tier pro \\
        --datasources 50 --queries 250000 \\
        --features sso,compliance_reports \\
        --expires 2027-08-30 \\
        --out acme.license

The customer sets EIYE_LICENSE_FILE to the resulting path. Verification is
offline against the public key embedded in eiye_db/license.py, so this tool and
the deployment never need to reach each other.

Anyone holding the private key can mint licenses for every deployment in the
field. Losing it means re-keying every customer; leaking it means the paid tiers
are unenforceable until you do. Treat it accordingly.
"""

import argparse
import base64
import json
import os
import sys
import uuid
from datetime import datetime, timezone


def build_claims(args: argparse.Namespace) -> dict:
    features = [f.strip() for f in (args.features or "").split(",") if f.strip()]
    expires = datetime.strptime(args.expires, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return {
        "license_id": args.license_id or str(uuid.uuid4()),
        "customer": args.customer,
        "tier": args.tier,
        "max_datasources": args.datasources,
        "max_queries_per_month": args.queries,
        "features": features,
        "issued_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": expires.isoformat(),
    }


def sign(claims: dict, signing_key_b64: str) -> str:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(signing_key_b64))
    # Sign the exact payload bytes the verifier will check, so no JSON
    # re-serialization sits between signing and verification.
    payload_b64 = base64.b64encode(json.dumps(claims, sort_keys=True).encode()).decode()
    signature = base64.b64encode(key.sign(payload_b64.encode())).decode()
    return json.dumps({"claims": payload_b64, "signature": signature}, indent=2) + "\n"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--customer", required=True, help="Licensee name, as it should appear in /status")
    p.add_argument("--tier", required=True, choices=["starter", "pro", "business", "enterprise"])
    p.add_argument("--datasources", type=int, required=True, help="max registered datasources")
    p.add_argument("--queries", type=int, required=True, help="max metered queries per calendar month")
    p.add_argument("--expires", required=True, help="YYYY-MM-DD (UTC)")
    p.add_argument("--features", default="", help="comma-separated, e.g. sso,compliance_reports")
    p.add_argument("--license-id", default=None, help="defaults to a random uuid4")
    p.add_argument("--out", required=True, help="output path for the license file")
    args = p.parse_args()

    signing_key = os.environ.get("EIYE_LICENSE_SIGNING_KEY")
    if not signing_key:
        print("EIYE_LICENSE_SIGNING_KEY is not set (base64 Ed25519 private key)", file=sys.stderr)
        return 2

    claims = build_claims(args)
    with open(args.out, "w") as fh:
        fh.write(sign(claims, signing_key))
    print(f"Wrote {args.out}")
    print(json.dumps(claims, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
