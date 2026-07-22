"""Core data model validation tests."""

import pytest
from pydantic import ValidationError

from eiye_db.models import (
    AskRequest,
    ConnectionStatus,
    DataSource,
    DataSourceType,
    PIIResult,
)


def test_datasource_valid():
    ds = DataSource(name="orders-db", type=DataSourceType.POSTGRESQL)
    assert ds.status == ConnectionStatus.DISCOVERED
    assert ds.created_at is not None
    assert ds.config == {}


def test_datasource_rejects_unknown_type():
    with pytest.raises(ValidationError):
        DataSource(name="bad", type="oracle-cloud-fusion")


def test_datasource_accepts_type_string_coercion():
    ds = DataSource(name="files", type="filesystem")
    assert ds.type is DataSourceType.FILE_SYSTEM


def test_datasource_rejects_bad_pii_risk_level():
    with pytest.raises(ValidationError):
        DataSource(name="x", type=DataSourceType.CSV, pii_risk_level="High")


def test_enum_str_yields_value():
    assert f"{DataSourceType.POSTGRESQL}" == "postgresql"
    assert str(ConnectionStatus.CONNECTED) == "connected"


def test_ask_request_bounds():
    with pytest.raises(ValidationError):
        AskRequest(question="")
    with pytest.raises(ValidationError):
        AskRequest(question="q" * 501)
    assert AskRequest(question="how many customers").limit == 100


def test_pii_result_defaults():
    r = PIIResult(text="hello")
    assert r.risk_score == 0.0
    assert r.entities == []
    assert r.anonymized_text is None


def test_mutable_defaults_not_shared():
    a = DataSource(name="a", type=DataSourceType.CSV)
    b = DataSource(name="b", type=DataSourceType.CSV)
    a.tags.append("x")
    assert b.tags == []
