"""Datasource registry: CRUD over the metadata store."""

import uuid
from datetime import datetime, timezone

from sqlalchemy.exc import IntegrityError

from eiye_db import db
from eiye_db.models import ConnectionStatus, DataSource, DataSourceCreate, DataSourceUpdate


def _to_model(row: db.DataSourceRow) -> DataSource:
    return DataSource(
        id=row.id,
        name=row.name,
        type=row.type,
        status=row.status,
        config=row.config or {},
        pii_risk_level=row.pii_risk_level,
        tags=row.tags or [],
        description=row.description,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_connected=row.last_connected,
        meta=row.meta or {},
    )


def create(req: DataSourceCreate) -> DataSource:
    now = datetime.now(timezone.utc)
    row = db.DataSourceRow(
        id=str(uuid.uuid4()),
        name=req.name,
        type=req.type,
        status=ConnectionStatus.DISCOVERED,
        config=req.config,
        tags=req.tags,
        description=req.description,
        created_at=now,
        updated_at=now,
    )
    with db.session() as s:
        s.add(row)
        try:
            s.commit()
        except IntegrityError:
            raise ValueError(f"datasource name already exists: {req.name}")
        s.refresh(row)
        return _to_model(row)


def list_all() -> list[DataSource]:
    with db.session() as s:
        rows = s.query(db.DataSourceRow).order_by(db.DataSourceRow.created_at).all()
        return [_to_model(r) for r in rows]


def get(datasource_id: str) -> DataSource | None:
    with db.session() as s:
        row = s.get(db.DataSourceRow, datasource_id)
        return _to_model(row) if row else None


def update(datasource_id: str, req: DataSourceUpdate) -> DataSource | None:
    with db.session() as s:
        row = s.get(db.DataSourceRow, datasource_id)
        if row is None:
            return None
        for field in ("name", "config", "description", "tags"):
            value = getattr(req, field)
            if value is not None:
                setattr(row, field, value)
        row.updated_at = datetime.now(timezone.utc)
        s.commit()
        s.refresh(row)
        return _to_model(row)


def delete(datasource_id: str) -> bool:
    with db.session() as s:
        row = s.get(db.DataSourceRow, datasource_id)
        if row is None:
            return False
        # Relationships and metrics touching this datasource die with it —
        # stale approved semantics over a deleted source would mislead agents.
        s.query(db.RelationshipRow).filter(
            (db.RelationshipRow.from_datasource_id == datasource_id)
            | (db.RelationshipRow.to_datasource_id == datasource_id)
        ).delete()
        s.query(db.MetricRow).filter(db.MetricRow.datasource_id == datasource_id).delete()
        # Policies scoped to this source die with it too — a stale policy
        # naming a dead id can never match and only misleads the policy list.
        s.query(db.PolicyRow).filter(db.PolicyRow.resource_id == datasource_id).delete()
        s.delete(row)
        s.commit()
        return True


def set_status(datasource_id: str, status: ConnectionStatus, connected: bool = False) -> None:
    with db.session() as s:
        row = s.get(db.DataSourceRow, datasource_id)
        if row is None:
            return
        row.status = status
        row.updated_at = datetime.now(timezone.utc)
        if connected:
            row.last_connected = row.updated_at
        s.commit()


def set_schema(datasource_id: str, schema: dict) -> None:
    with db.session() as s:
        row = s.get(db.DataSourceRow, datasource_id)
        if row is None:
            return
        row.schema_json = schema
        row.updated_at = datetime.now(timezone.utc)
        s.commit()


def get_schema(datasource_id: str) -> dict | None:
    with db.session() as s:
        row = s.get(db.DataSourceRow, datasource_id)
        return row.schema_json if row else None
