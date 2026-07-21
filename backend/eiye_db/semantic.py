"""Semantic layer, Tier 1: relationships between columns across tables and datasources.

Trust model (see GOALS "Semantic Layer Strategy"): structural facts (real foreign
keys) are auto-approved and always supersede heuristic guesses at the same
endpoints; heuristic candidates are deterministic, explainable guesses that stay
non-authoritative until a human approves them. Human decisions on heuristic rows
survive re-detection; structural facts cannot be rejected (the source database
itself declares them). A link's identity is undirected — A→B and B→A are the
same relationship.
"""

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.exc import IntegrityError

from eiye_db import db

_ID_SUFFIXES = ("id", "key", "code", "uuid")
_GENERIC_KEYS = {"id", "key", "code", "uuid"}

# Type families: columns join within a family; numeric~text is also allowed
# because ids are commonly stored as strings on one side.
_NUMERIC = {"integer", "bigint", "smallint", "number", "numeric", "real", "double precision"}
_TEMPORAL = {"date", "time", "timestamp", "timestamp without time zone", "timestamp with time zone", "datetime"}
_BOOLEAN = {"boolean", "bool"}


def _family(type_name: str) -> str:
    t = type_name.lower()
    if t in _NUMERIC:
        return "numeric"
    if t in _TEMPORAL:
        return "temporal"
    if t in _BOOLEAN:
        return "boolean"
    return "text"


def _types_compatible(a: str, b: str) -> bool:
    fa, fb = _family(a), _family(b)
    if fa == fb:
        return fa not in ("temporal", "boolean")  # joining on timestamps/bools is never a key join
    return {fa, fb} == {"numeric", "text"}


def _norm(name: str) -> str:
    """Normalize a column name for matching: camelCase/snake-case/punctuation-invariant."""
    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", name)  # camelCase -> camel_Case
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _is_key_like(name: str) -> bool:
    return _norm(name).endswith(_ID_SUFFIXES)


def _singular(word: str) -> str:
    if word.endswith("ies"):
        return word[:-3] + "y"
    if word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if word.endswith("s"):
        return word[:-1]
    return word


def _table_stem(table: str) -> str:
    """'customers.csv' -> 'customer', 'order_items' -> 'orderitem', 'addresses' -> 'address'."""
    stem = table.rsplit("/", 1)[-1].split(".", 1)[0]
    return _singular(_norm(stem))


def _undirected(ds_a: str, tab_a: str, col_a: str, ds_b: str, tab_b: str, col_b: str) -> tuple:
    """Direction-insensitive identity for a link: A→B and B→A are the same edge."""
    return tuple(sorted([(ds_a, tab_a, col_a), (ds_b, tab_b, col_b)]))


def _rel_undirected(rel: dict[str, Any]) -> tuple:
    return _undirected(
        rel["from_datasource_id"],
        rel["from_table"],
        rel["from_column"],
        rel["to_datasource_id"],
        rel["to_table"],
        rel["to_column"],
    )


def _row_undirected(r: db.RelationshipRow) -> tuple:
    return _undirected(
        r.from_datasource_id, r.from_table, r.from_column, r.to_datasource_id, r.to_table, r.to_column
    )


def detect_candidates(schemas: list[tuple[str, str, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Propose candidate joins across all tables of all datasources.

    ``schemas`` is [(datasource_id, datasource_name, tables)] using the connector
    table shape. Deterministic and explainable: every candidate carries a
    confidence and a human-readable rationale. Only key-like columns are
    considered, which keeps false positives (and output size) down.
    """
    # Flatten once with precomputed norms/stems/families so the O(n^2) pair loop
    # below does only cheap string comparisons (no per-pair regex work).
    cols: list[tuple[str, str, str, str, str, str]] = []  # ds, table, col, norm, stem, family
    for ds_id, _ds_name, tables in schemas:
        for t in tables:
            stem = _table_stem(t["name"])
            for f in t.get("fields") or []:
                if _is_key_like(f["name"]):
                    cols.append((ds_id, t["name"], f["name"], _norm(f["name"]), stem, _family(str(f.get("type", "")))))

    out: list[dict[str, Any]] = []
    seen: set[tuple] = set()
    for i, (ds_a, tab_a, col_a, na, stem_a, fam_a) in enumerate(cols):
        for ds_b, tab_b, col_b, nb, stem_b, fam_b in cols[i + 1 :]:
            if ds_a == ds_b and tab_a == tab_b:
                continue  # no self-joins within one table
            if fam_a != fam_b and {fam_a, fam_b} != {"numeric", "text"}:
                continue
            if fam_a == fam_b and fam_a in ("temporal", "boolean"):
                continue

            confidence = 0.0
            rationale = ""
            if na == nb and na not in _GENERIC_KEYS:
                # e.g. customer_id <-> customerId
                confidence = 0.9
                rationale = f"column names match after normalization ({col_a!r} ~ {col_b!r})"
            elif na == f"{stem_b}id" or na == f"{stem_b}{nb}":
                # e.g. orders.customer_id -> customers.id
                confidence = 0.8
                rationale = f"{tab_a}.{col_a} names the {tab_b} table's key ({col_b})"
            elif nb == f"{stem_a}id" or nb == f"{stem_a}{na}":
                confidence = 0.8
                rationale = f"{tab_b}.{col_b} names the {tab_a} table's key ({col_a})"
            if not confidence:
                continue

            key = _undirected(ds_a, tab_a, col_a, ds_b, tab_b, col_b)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                {
                    "from_datasource_id": ds_a,
                    "from_table": tab_a,
                    "from_column": col_a,
                    "to_datasource_id": ds_b,
                    "to_table": tab_b,
                    "to_column": col_b,
                    "kind": "candidate_join",
                    "source": "heuristic",
                    "confidence": confidence,
                    "rationale": rationale,
                }
            )
    return out


def _to_dict(r: db.RelationshipRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "from_datasource_id": r.from_datasource_id,
        "from_table": r.from_table,
        "from_column": r.from_column,
        "to_datasource_id": r.to_datasource_id,
        "to_table": r.to_table,
        "to_column": r.to_column,
        "kind": r.kind,
        "source": r.source,
        "status": r.status,
        "confidence": r.confidence,
        "rationale": r.rationale,
    }


def _upgrade_to_structural(row: db.RelationshipRow, rel: dict[str, Any], now: datetime) -> None:
    """Structural facts supersede heuristic guesses at the same endpoints.

    A human rejection of a heuristic guess must not veto the database's own
    metadata — the row is upgraded in place (preserving its id) and approved.
    Also fixes direction: the stored endpoints take the FK's child→parent order.
    """
    row.from_datasource_id = rel["from_datasource_id"]
    row.from_table = rel["from_table"]
    row.from_column = rel["from_column"]
    row.to_datasource_id = rel["to_datasource_id"]
    row.to_table = rel["to_table"]
    row.to_column = rel["to_column"]
    row.kind = rel["kind"]
    row.source = "structural"
    row.status = "approved"
    row.confidence = 1.0
    row.rationale = rel["rationale"]
    row.updated_at = now


def upsert(rels: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Insert relationships that don't exist yet (undirected identity).

    Existing heuristic rows — and the human decisions on them — are preserved,
    with one exception: an incoming *structural* relationship upgrades a
    heuristic row at the same endpoints to approved ground truth.
    """
    now = datetime.now(timezone.utc)
    created: list[dict[str, Any]] = []
    with db.session() as s:
        existing: dict[tuple, db.RelationshipRow] = {_row_undirected(r): r for r in s.query(db.RelationshipRow).all()}
        for rel in rels:
            key = _rel_undirected(rel)
            row = existing.get(key)
            if row is not None:
                if rel["source"] == "structural" and row.source != "structural":
                    _upgrade_to_structural(row, rel, now)
                continue
            row = db.RelationshipRow(
                id=str(uuid.uuid4()),
                status="approved" if rel["source"] == "structural" else "candidate",
                created_at=now,
                updated_at=now,
                **rel,
            )
            s.add(row)
            existing[key] = row  # dedup within this batch too
            created.append(_to_dict(row))
        try:
            s.commit()
        except IntegrityError:
            # A concurrent writer inserted the same directed endpoints first;
            # its row is equivalent, so losing this race is fine.
            s.rollback()
            return []
    return created


def sync_structural(datasource_id: str, fks: list[dict[str, Any]]) -> None:
    """Reconcile a datasource's structural (FK) relationships after discovery.

    Single transaction: current FKs are inserted (or upgrade a heuristic row at
    the same endpoints); structural rows whose FK no longer exists in the source
    are deleted. Existing structural rows keep their id across re-discovery.
    """
    now = datetime.now(timezone.utc)
    rels = [
        {
            "from_datasource_id": datasource_id,
            "from_table": fk["from_table"],
            "from_column": fk["from_column"],
            "to_datasource_id": datasource_id,
            "to_table": fk["to_table"],
            "to_column": fk["to_column"],
            "kind": "foreign_key",
            "source": "structural",
            "confidence": 1.0,
            "rationale": "foreign key constraint in the source database",
        }
        for fk in fks
    ]
    current_keys = {_rel_undirected(r) for r in rels}
    with db.session() as s:
        existing: dict[tuple, db.RelationshipRow] = {_row_undirected(r): r for r in s.query(db.RelationshipRow).all()}
        for rel in rels:
            row = existing.get(_rel_undirected(rel))
            if row is None:
                s.add(
                    db.RelationshipRow(
                        id=str(uuid.uuid4()), status="approved", created_at=now, updated_at=now, **rel
                    )
                )
            elif row.source != "structural":
                _upgrade_to_structural(row, rel, now)
        for key, row in existing.items():
            stale = (
                row.source == "structural"
                and row.from_datasource_id == datasource_id
                and key not in current_keys
            )
            if stale:
                s.delete(row)
        s.commit()


def prune_stale_candidates(schemas: list[tuple[str, str, list[dict[str, Any]]]]) -> int:
    """Drop candidate rows whose endpoints no longer exist in the current schemas.

    Only unreviewed candidates are pruned — approved/rejected rows are human
    decisions and survive schema drift (they surface as-is until reviewed).
    """
    live: set[tuple[str, str, str]] = set()
    ds_ids: set[str] = set()
    for ds_id, _name, tables in schemas:
        ds_ids.add(ds_id)
        for t in tables:
            for f in t.get("fields") or []:
                live.add((ds_id, t["name"], f["name"]))

    pruned = 0
    with db.session() as s:
        for row in s.query(db.RelationshipRow).filter(db.RelationshipRow.status == "candidate").all():
            for ds, tab, col in (
                (row.from_datasource_id, row.from_table, row.from_column),
                (row.to_datasource_id, row.to_table, row.to_column),
            ):
                if ds in ds_ids and (ds, tab, col) not in live:
                    s.delete(row)
                    pruned += 1
                    break
        s.commit()
    return pruned


def delete_for_datasource(datasource_id: str) -> int:
    """Drop every relationship touching this datasource (both directions)."""
    with db.session() as s:
        n = (
            s.query(db.RelationshipRow)
            .filter(
                (db.RelationshipRow.from_datasource_id == datasource_id)
                | (db.RelationshipRow.to_datasource_id == datasource_id)
            )
            .delete()
        )
        s.commit()
        return n


def list_relationships(status: str | None = None, datasource_id: str | None = None) -> list[dict[str, Any]]:
    with db.session() as s:
        q = s.query(db.RelationshipRow)
        if status:
            q = q.filter(db.RelationshipRow.status == status)
        if datasource_id:
            q = q.filter(
                (db.RelationshipRow.from_datasource_id == datasource_id)
                | (db.RelationshipRow.to_datasource_id == datasource_id)
            )
        return [_to_dict(r) for r in q.order_by(db.RelationshipRow.created_at).all()]


def set_status(relationship_id: str, status: str) -> tuple[dict[str, Any] | None, str | None]:
    """Apply a human review. Returns (relationship, previous_status).

    Structural rows cannot be reviewed — the source database declares them, so
    rejecting one here would just misrepresent the source. Returns (None,
    "structural") in that case; (None, None) if the id is unknown.
    """
    with db.session() as s:
        row = s.get(db.RelationshipRow, relationship_id)
        if row is None:
            return None, None
        if row.source == "structural":
            return None, "structural"
        previous = row.status
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        s.commit()
        return _to_dict(row), previous


def export_yaml() -> str:
    """The approved semantic model as YAML ("semantic-layer-as-code").

    Hand-emitted; scalars are JSON-quoted (valid YAML double-quoted style), so
    table/column names containing YAML metacharacters stay intact.
    """
    q = json.dumps  # JSON string escaping == YAML double-quoted scalar escaping
    lines = ["# eiye_db semantic model — approved relationships", "relationships:"]
    approved = list_relationships(status="approved")
    if not approved:
        lines.append("  []")
    for r in approved:
        lines.append(f"  - from: {q(r['from_datasource_id'] + '/' + r['from_table'] + '.' + r['from_column'])}")
        lines.append(f"    to: {q(r['to_datasource_id'] + '/' + r['to_table'] + '.' + r['to_column'])}")
        lines.append(f"    kind: {q(r['kind'])}")
        lines.append(f"    source: {q(r['source'])}")
        lines.append(f"    confidence: {r['confidence']}")
    return "\n".join(lines) + "\n"
