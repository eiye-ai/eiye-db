"""ABAC policy engine: per-source access and per-column masking.

Default posture is ALLOW (no policies = today's behavior); set
EIYE_ABAC_DEFAULT_DENY=true to require an explicit allow policy for every
non-admin access. Either way, evaluation order is fixed: explicit deny wins
over explicit allow wins over the default. Admin identities bypass policies —
they are the governors who write them.

Column masking is a deny-shaped condition: a deny policy with
conditions={"columns": [...]} masks those columns out of query results
instead of blocking the source outright. Masks from every applicable policy
accumulate.

Policies are metadata, not data: they never contain row values, and every
mutation is admin-gated and audited by the API layer.
"""

import uuid
from datetime import datetime, timezone
from typing import Any

from eiye_db import db
from eiye_db.config import settings

ACTIONS = {"read", "discover"}
EFFECTS = {"allow", "deny"}
WILDCARD = "*"


class PolicyError(Exception):
    """Invalid policy definition."""


class PolicyDenied(Exception):
    """Access blocked by policy (or by default-deny with no allow).

    str(exc) is the caller-facing message and stays generic — policy names
    reveal what is being protected. `detail` carries the specifics for the
    audit trail, which is admin-only.
    """

    def __init__(self, detail: str):
        super().__init__("access denied by policy")
        self.detail = detail


def _validate(
    name: str, effect: str, resource_id: str, actions: list[str], subjects: list[str], conditions: dict
) -> None:
    if not name or len(name) > 255:
        raise PolicyError("policy name must be 1-255 characters")
    if effect not in EFFECTS:
        raise PolicyError(f"effect must be one of {sorted(EFFECTS)}")
    if not actions or not set(actions) <= ACTIONS:
        raise PolicyError(f"actions must be a non-empty subset of {sorted(ACTIONS)}")
    if not subjects or not all(isinstance(s, str) and s for s in subjects):
        raise PolicyError("subjects must be a non-empty list of key ids (or ['*'])")
    if not isinstance(resource_id, str) or not resource_id:
        raise PolicyError("resource_id must be a datasource id or '*'")
    unknown = set(conditions) - {"columns"}
    if unknown:
        raise PolicyError(f"unknown condition keys: {sorted(unknown)}")
    columns = conditions.get("columns", [])
    if not isinstance(columns, list) or not all(isinstance(c, str) and c for c in columns):
        raise PolicyError("conditions.columns must be a list of column names")
    if columns and (effect != "deny" or actions != ["read"]):
        # Masking only means something on read; on any other shape it would
        # silently do nothing (or silently NOT deny) — refuse instead.
        raise PolicyError("column masking requires effect='deny' and actions=['read']")


def _to_dict(r: db.PolicyRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "effect": r.effect,
        "resource_type": r.resource_type,
        "resource_id": r.resource_id,
        "actions": r.actions,
        "subjects": r.subjects,
        "conditions": r.conditions,
        "created_at": r.created_at.isoformat(),
    }


def create(
    name: str,
    description: str,
    effect: str,
    resource_id: str,
    actions: list[str],
    subjects: list[str],
    conditions: dict | None = None,
) -> dict[str, Any]:
    conditions = conditions or {}
    _validate(name, effect, resource_id, actions, subjects, conditions)
    # Description is free text that gets re-served to reviewers of the policy
    # list: cap + redact, same posture as proposal rationales.
    from eiye_db import pii

    now = datetime.now(timezone.utc)
    row = db.PolicyRow(
        id=str(uuid.uuid4()),
        name=name,
        description=pii.redact_text(description[:500])[0],
        effect=effect,
        resource_type="datasource",
        resource_id=resource_id,
        actions=actions,
        subjects=subjects,
        conditions=conditions,
        created_at=now,
        updated_at=now,
    )
    with db.session() as s:
        if s.query(db.PolicyRow).filter_by(name=name).first():
            raise PolicyError(f"policy name already exists: {name}")
        s.add(row)
        s.commit()
        return _to_dict(row)


def list_policies() -> list[dict[str, Any]]:
    with db.session() as s:
        return [_to_dict(r) for r in s.query(db.PolicyRow).order_by(db.PolicyRow.name).all()]


def delete(policy_id: str) -> dict[str, Any] | None:
    """Delete a policy; returns its definition (for the audit trail) or None."""
    with db.session() as s:
        row = s.get(db.PolicyRow, policy_id)
        if row is None:
            return None
        removed = _to_dict(row)
        s.delete(row)
        s.commit()
        return removed


def _applies(r: db.PolicyRow, key_id: str, action: str, datasource_id: str) -> bool:
    return (
        action in r.actions
        and (WILDCARD in r.subjects or key_id in r.subjects)
        and r.resource_id in (WILDCARD, datasource_id)
    )


def check(key_id: str, is_admin: bool, action: str, datasource_id: str) -> set[str]:
    """Enforce policy for one access; returns the columns to mask.

    Raises PolicyDenied when an explicit deny matches, or when default-deny
    is on and no allow policy matches. Admin bypasses entirely.
    """
    if is_admin:
        return set()
    masked: set[str] = set()
    allowed = False
    with db.session() as s:
        rows = s.query(db.PolicyRow).all()
    for r in rows:
        if not _applies(r, key_id, action, datasource_id):
            continue
        if r.effect == "deny":
            columns = (r.conditions or {}).get("columns", [])
            if columns:
                masked.update(columns)
            else:
                raise PolicyDenied(f"denied by policy '{r.name}'")
        else:
            allowed = True
    if settings.abac_default_deny and not allowed:
        raise PolicyDenied(f"no allow policy grants '{action}' (default-deny is on)")
    return masked


def explain(key_id: str, is_admin: bool, datasource_ids: list[str]) -> list[dict[str, Any]]:
    """What one subject may do with each source, decided by the same functions
    that enforce it.

    Default-deny is hard to operate blind. The refusal a caller sees is
    deliberately generic (policy names reveal what is being protected), so from
    the outside a missing allow and an explicit deny look identical, and the
    only way to tell them apart is to read the whole policy table by hand. This
    calls check() and permits() rather than reimplementing their order, because
    an explanation that could drift from enforcement is worse than none.
    """
    reviewed = []
    for ds_id in datasource_ids:
        try:
            masked = sorted(check(key_id, is_admin, "read", ds_id))
            read = True
        except PolicyDenied:
            masked, read = [], False
        reviewed.append(
            {
                "datasource_id": ds_id,
                "read": read,
                "discover": permits(key_id, is_admin, "discover", ds_id),
                "masked_columns": masked,
            }
        )
    return reviewed


def permits(key_id: str, is_admin: bool, action: str, datasource_id: str) -> bool:
    """Non-raising check, for filtering metadata listings (no audit — a
    filtered listing is not a denied access attempt)."""
    try:
        check(key_id, is_admin, action, datasource_id)
        return True
    except PolicyDenied:
        return False
