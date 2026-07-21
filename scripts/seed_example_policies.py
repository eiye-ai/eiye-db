#!/usr/bin/env python
"""Load the example ABAC policies into a running eiye_db server.

Policies with a REPLACE-WITH-DATASOURCE-ID placeholder are skipped unless you
pass --datasource-id to substitute a real one. Re-running is safe: a policy
whose name already exists is reported and skipped (409/400 from the server).

Examples:
  seed_example_policies.py                                   # localhost:8000, open dev mode
  seed_example_policies.py --url http://localhost:8010 --api-key "$EIYE_ADMIN_API_KEY"
  seed_example_policies.py --datasource-id 9a850582-...      # also seed the per-source examples

Policy management is admin-only: pass the ADMIN key when EIYE_API_KEY is set.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "policies" / "example_policies.json"
PLACEHOLDER = "REPLACE-WITH-DATASOURCE-ID"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000", help="eiye_db server base URL")
    ap.add_argument("--api-key", default=None, help="admin API key (omit in open dev mode)")
    ap.add_argument("--datasource-id", default=None, help="substitute for the per-source example policies")
    args = ap.parse_args()

    policies = json.loads(EXAMPLES.read_text())
    seeded = skipped = 0
    for p in policies:
        if p["resource_id"] == PLACEHOLDER:
            if args.datasource_id is None:
                print(f"skip {p['name']}: needs --datasource-id")
                skipped += 1
                continue
            p = {**p, "resource_id": args.datasource_id}
        req = urllib.request.Request(
            f"{args.url}/api/v1/policies",
            data=json.dumps(p).encode(),
            headers={"Content-Type": "application/json", **({"X-API-Key": args.api_key} if args.api_key else {})},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                created = json.loads(resp.read())
                print(f"seeded {created['name']} ({created['effect']} on {created['resource_id']})")
                seeded += 1
        except urllib.error.HTTPError as e:
            print(f"skip {p['name']}: HTTP {e.code} {e.read().decode()[:200]}")
            skipped += 1
    print(f"\n{seeded} seeded, {skipped} skipped")
    return 0 if seeded or skipped else 1


if __name__ == "__main__":
    sys.exit(main())
