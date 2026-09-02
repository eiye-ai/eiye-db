#!/usr/bin/env python3
"""Grant a subject access to datasources, for default-deny deployments.

    grant.py --subject support-agent --source customers --source orders
    grant.py --subject support-agent --all-sources
    grant.py --subject support-agent --source customers --actions read

With EIYE_ABAC_DEFAULT_DENY=true every non-admin caller is denied until an
allow policy names it. Writing those by hand means composing policy JSON and
looking up datasource ids, which is why the hardened posture has been easy to
turn on and hard to run. This is the missing half.

A subject is a key id: an entry in EIYE_API_KEYS, the reserved ids `primary`
and `admin`, or whatever an MCP client puts in EIYE_KEY_ID. Sources are named
or given by id; names are resolved against the registry.

Policy management is admin-only, so pass the admin key whenever auth is set:

    grant.py --subject support-agent --all-sources --api-key "$EIYE_ADMIN_API_KEY"

Re-running is safe. A policy whose name already exists is reported and skipped,
so this cannot quietly widen an existing grant. To remove one, take the id it
printed and DELETE /api/v1/policies/{id}.

Check the result with:

    curl -s localhost:8000/api/v1/access/support-agent -H "X-API-Key: $EIYE_ADMIN_API_KEY"
"""

import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_ACTIONS = ["read", "discover"]


def _call(url: str, api_key: str | None, path: str, payload: dict | None = None) -> tuple[int, object]:
    req = urllib.request.Request(
        f"{url}{path}",
        data=json.dumps(payload).encode() if payload is not None else None,
        headers={"Content-Type": "application/json", **({"X-API-Key": api_key} if api_key else {})},
        method="POST" if payload is not None else "GET",
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def resolve(sources: list[str], registered: list[dict]) -> tuple[list[tuple[str, str]], list[str]]:
    """Map each --source (name or id) to (id, name). Returns unmatched separately."""
    by_id = {d["id"]: d["name"] for d in registered}
    by_name: dict[str, list[str]] = {}
    for d in registered:
        by_name.setdefault(d["name"], []).append(d["id"])
    matched, missing = [], []
    for s in sources:
        if s in by_id:
            matched.append((s, by_id[s]))
        elif len(by_name.get(s, [])) == 1:
            matched.append((by_name[s][0], s))
        elif len(by_name.get(s, [])) > 1:
            # Names are not unique in the registry, so refuse rather than
            # granting access to whichever one happened to sort first.
            missing.append(f"{s} (ambiguous: {len(by_name[s])} sources share this name — use an id)")
        else:
            missing.append(s)
    return matched, missing


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--subject", required=True, help="key id to grant: an EIYE_API_KEYS entry, 'primary', or an MCP EIYE_KEY_ID")
    p.add_argument("--source", action="append", default=[], metavar="NAME_OR_ID", help="repeatable; datasource name or id")
    p.add_argument("--all-sources", action="store_true", help="grant on every source, present and future ('*')")
    p.add_argument("--actions", default=",".join(DEFAULT_ACTIONS), help=f"comma-separated (default: {','.join(DEFAULT_ACTIONS)})")
    p.add_argument("--url", default="http://localhost:8000", help="eiye_db server base URL")
    p.add_argument("--api-key", default=None, help="admin API key (omit only in open dev mode)")
    args = p.parse_args()

    if args.all_sources == bool(args.source):
        print("pass either --all-sources or one or more --source, not both and not neither", file=sys.stderr)
        return 2
    actions = [a.strip() for a in args.actions.split(",") if a.strip()]
    if not actions:
        print("--actions must name at least one action", file=sys.stderr)
        return 2

    if args.all_sources:
        targets = [("*", "all sources")]
    else:
        status, registered = _call(args.url, args.api_key, "/api/v1/datasources")
        if status != 200:
            print(f"cannot list datasources: HTTP {status} {registered}", file=sys.stderr)
            print("listing registrations is admin-only — pass --api-key with the admin key.", file=sys.stderr)
            return 1
        targets, missing = resolve(args.source, registered)
        if missing:
            print("no such datasource: " + ", ".join(missing), file=sys.stderr)
            return 1

    granted = skipped = 0
    for resource_id, label in targets:
        suffix = "all" if resource_id == "*" else label
        body = {
            "name": f"allow-{args.subject}-{suffix}",
            "description": f"Grant {args.subject} {'+'.join(actions)} on {label}. Written by scripts/grant.py.",
            "effect": "allow",
            "resource_id": resource_id,
            "actions": actions,
            "subjects": [args.subject],
        }
        status, created = _call(args.url, args.api_key, "/api/v1/policies", body)
        if status == 201:
            print(f"granted {'+'.join(actions)} on {label} -> {args.subject}  (policy {created['id']})")
            granted += 1
        else:
            print(f"skip {body['name']}: HTTP {status} {created}")
            skipped += 1

    print(f"\n{granted} granted, {skipped} skipped")
    if granted:
        print(f"Check it: curl -s {args.url}/api/v1/access/{args.subject} -H 'X-API-Key: <admin key>'")
    print("A deny policy still beats an allow: explicit deny > explicit allow > default.")
    return 0 if granted or skipped else 1


if __name__ == "__main__":
    raise SystemExit(main())
