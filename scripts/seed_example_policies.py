#!/usr/bin/env python
"""Load the example ABAC policies into a running eiye_db server.

Policies carrying a placeholder are skipped unless you supply the substitution:
REPLACE-WITH-DATASOURCE-ID needs --datasource-id, REPLACE-WITH-KEY-ID needs
--subject. Seeding a placeholder verbatim would create a policy that silently
never matches, which in an access-control file reads as configured and is not.
Re-running is safe: a policy whose name already exists is reported and skipped.

These are shape references. For an ordinary grant, scripts/grant.py resolves
sources by name and writes the policy for you.

Examples:
  seed_example_policies.py                                   # localhost:8000, open dev mode
  seed_example_policies.py --url http://localhost:8010 --api-key "$EIYE_ADMIN_API_KEY"
  seed_example_policies.py --datasource-id 9a850582-...      # also the per-source examples
  seed_example_policies.py --datasource-id 9a850582-... --subject support-agent   # and the named-key one

Policy management is admin-only: pass the ADMIN key when any key is set.
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
SUBJECT_PLACEHOLDER = "REPLACE-WITH-KEY-ID"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="http://localhost:8000", help="eiye_db server base URL")
    ap.add_argument("--api-key", default=None, help="admin API key (omit in open dev mode)")
    ap.add_argument("--datasource-id", default=None, help="substitute for the per-source example policies")
    ap.add_argument("--subject", default=None, help="key id to substitute into the named-key example")
    args = ap.parse_args()

    policies = json.loads(EXAMPLES.read_text())
    seeded = skipped = 0
    for p in policies:
        needs = []
        if p["resource_id"] == PLACEHOLDER and args.datasource_id is None:
            needs.append("--datasource-id")
        if SUBJECT_PLACEHOLDER in p["subjects"] and args.subject is None:
            needs.append("--subject")
        if needs:
            print(f"skip {p['name']}: needs {' and '.join(needs)}")
            skipped += 1
            continue
        if p["resource_id"] == PLACEHOLDER:
            p = {**p, "resource_id": args.datasource_id}
        if SUBJECT_PLACEHOLDER in p["subjects"]:
            p = {**p, "subjects": [args.subject if x == SUBJECT_PLACEHOLDER else x for x in p["subjects"]]}
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
