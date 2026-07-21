"""Metric catalog: named, governed query templates ("define once, run consistently").

A metric binds a name to a connector-specific request template with typed,
strictly-validated parameters. Execution goes through the same governance chain
as every ad-hoc query (read-only connector → PII redaction → audit), so a
metric is deterministic by construction: same definition + same params = same
governed query, with no per-run improvisation by the caller.

Trust model: human-authored metrics (admin-created) are approved immediately;
agent-proposed metrics stay candidates until a human approves. Only approved
metrics execute.
"""

import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from eiye_db import db

_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
# String parameter values: no quotes, semicolons, braces, slashes, or newlines
# (fullmatch rejects trailing \n), and the '--' bigram is blocked separately so
# SQL comments can't be smuggled. This makes values inert INSIDE quoted SQL
# literals / path segments / URL params — templates should keep string params
# inside quotes; unquoted placement lets a caller alter query semantics even
# with this allowlist.
_STRING_VALUE_RE = re.compile(r"[A-Za-z0-9_ .@-]{0,200}")


class CatalogError(Exception):
    """Invalid metric definition or invalid parameters for execution."""


class MetricNotApproved(CatalogError):
    """The metric exists but has not been human-approved for execution."""


def _check_value(name: str, spec: dict[str, Any], value: Any) -> str:
    """Validate one parameter value against its spec; return its rendering."""
    if spec["type"] == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise CatalogError(f"parameter {name!r} must be a finite number")
        return str(value)
    value = str(value)
    if not _STRING_VALUE_RE.fullmatch(value) or "--" in value:
        raise CatalogError(
            f"parameter {name!r} contains disallowed characters "
            "(allowed: letters, digits, space, _ . @ -, no '--', max 200)"
        )
    return value


def _to_dict(r: db.MetricRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "description": r.description,
        "datasource_id": r.datasource_id,
        "request_template": r.request_template,
        "params": r.params,
        "source": r.source,
        "status": r.status,
    }


def _template_placeholders(template: Any) -> set[str]:
    found: set[str] = set()

    def _walk(v: Any) -> None:
        if isinstance(v, str):
            found.update(_PLACEHOLDER_RE.findall(v))
        elif isinstance(v, dict):
            for k, x in v.items():
                _walk(k)
                _walk(x)
        elif isinstance(v, list):
            for x in v:
                _walk(x)

    _walk(template)
    return found


def validate_definition(request_template: dict[str, Any], params: dict[str, Any]) -> None:
    """Reject inconsistent definitions at authoring time, not run time."""
    for name, spec in params.items():
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            raise CatalogError(f"invalid parameter name: {name!r}")
        if not isinstance(spec, dict) or spec.get("type") not in ("string", "number"):
            raise CatalogError(f"parameter {name!r} needs a type of 'string' or 'number'")
        if "default" in spec:
            _check_value(name, spec, spec["default"])  # an unrunnable default fails now, not at first run
    placeholders = _template_placeholders(request_template)
    undeclared = placeholders - set(params)
    if undeclared:
        raise CatalogError(f"template references undeclared parameters: {sorted(undeclared)}")
    unused = set(params) - placeholders
    if unused:
        raise CatalogError(f"declared parameters never used in the template: {sorted(unused)}")


def substitute(
    request_template: dict[str, Any], params_spec: dict[str, Any], params: dict[str, Any]
) -> dict[str, Any]:
    """Fill the template with strictly-validated parameter values.

    Strings are allowlist-validated (quotes/semicolons/braces are rejected
    outright — combined with the connectors' read-only enforcement this keeps
    template injection inert); numbers must be real numbers. Unknown or missing
    parameters are errors, not surprises.
    """
    unknown = set(params) - set(params_spec)
    if unknown:
        raise CatalogError(f"unknown parameters: {sorted(unknown)}")

    resolved: dict[str, str] = {}
    for name, spec in params_spec.items():
        if name in params:
            value = params[name]
        elif "default" in spec:
            value = spec["default"]
        else:
            raise CatalogError(f"missing required parameter: {name}")
        resolved[name] = _check_value(name, spec, value)

    def _walk(v: Any) -> Any:
        if isinstance(v, str):
            return _PLACEHOLDER_RE.sub(lambda m: resolved[m.group(1)], v)
        if isinstance(v, dict):
            return {_walk(k): _walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_walk(x) for x in v]
        return v

    return _walk(request_template)


def create(
    name: str,
    description: str,
    datasource_id: str,
    request_template: dict[str, Any],
    params: dict[str, Any],
    source: str,
) -> dict[str, Any]:
    validate_definition(request_template, params)
    # Free-text is capped and PII-redacted before persisting — descriptions are
    # re-served to reviewers and agents, so they must not carry raw PII.
    from eiye_db import pii

    description = pii.redact_text(description[:500])[0]
    now = datetime.now(timezone.utc)
    row = db.MetricRow(
        id=str(uuid.uuid4()),
        name=name,
        description=description,
        datasource_id=datasource_id,
        request_template=request_template,
        params=params,
        source=source,
        status="approved" if source == "human" else "candidate",
        created_at=now,
        updated_at=now,
    )
    with db.session() as s:
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            raise CatalogError(f"metric name already exists: {name}")
        s.refresh(row)
        return _to_dict(row)


def list_metrics(status: str | None = None) -> list[dict[str, Any]]:
    with db.session() as s:
        q = s.query(db.MetricRow)
        if status:
            q = q.filter(db.MetricRow.status == status)
        return [_to_dict(r) for r in q.order_by(db.MetricRow.created_at).all()]


def get(metric_id: str) -> dict[str, Any] | None:
    with db.session() as s:
        row = s.get(db.MetricRow, metric_id)
        return _to_dict(row) if row else None


def set_status(metric_id: str, status: str) -> tuple[dict[str, Any] | None, str | None]:
    """Apply a human review. Returns (metric, previous_status)."""
    with db.session() as s:
        row = s.get(db.MetricRow, metric_id)
        if row is None:
            return None, None
        previous = row.status
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        s.commit()
        return _to_dict(row), previous


def delete(metric_id: str) -> bool:
    with db.session() as s:
        row = s.get(db.MetricRow, metric_id)
        if row is None:
            return False
        s.delete(row)
        s.commit()
        return True


def delete_for_datasource(datasource_id: str) -> int:
    """Metrics die with their datasource — a metric over a deleted source is a trap."""
    with db.session() as s:
        n = s.query(db.MetricRow).filter(db.MetricRow.datasource_id == datasource_id).delete()
        s.commit()
        return n


def build_request(metric: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """The governed request for one metric execution. Approved metrics only."""
    if metric["status"] != "approved":
        raise MetricNotApproved(f"metric is not approved (status: {metric['status']})")
    return substitute(metric["request_template"], metric["params"], params)


def export_yaml_lines(datasource_ids: set[str] | None = None) -> list[str]:
    """Approved metrics for the semantic-model YAML export. `datasource_ids`
    is a visibility filter (policy-scoped export)."""
    import json

    q = json.dumps
    lines = ["metrics:"]
    approved = list_metrics(status="approved")
    if datasource_ids is not None:
        approved = [m for m in approved if m["datasource_id"] in datasource_ids]
    if not approved:
        lines.append("  []")
    for m in approved:
        lines.append(f"  - name: {q(m['name'])}")
        lines.append(f"    description: {q(m['description'])}")
        lines.append(f"    datasource: {q(m['datasource_id'])}")
        lines.append(f"    template: {q(json.dumps(m['request_template']))}")
        lines.append(f"    params: {q(json.dumps(m['params']))}")
    return lines
