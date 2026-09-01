"""scripts/quickstart.py must keep up with the connectors.

It fell behind the enum twice — MySQL and SQL Server shipped, then SQLite and
S3, and `--type` kept offering the same three choices it had on day one. The
README points new users at this script, so a connector missing from it is a
connector they cannot reach. Asserting the coverage is cheaper than remembering.

The script lives outside the package (it is a script, not a module), so it is
loaded by path rather than imported.
"""

import importlib.util
from pathlib import Path

import pytest

from eiye_db.models import DataSourceType

QUICKSTART = Path(__file__).resolve().parents[2] / "scripts" / "quickstart.py"


@pytest.fixture(scope="module")
def quickstart():
    spec = importlib.util.spec_from_file_location("quickstart", QUICKSTART)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_type_has_a_shorthand_flag(quickstart):
    assert set(quickstart.SHORTHAND) == {t.value for t in DataSourceType}


def test_sample_request_matches_each_connector(quickstart):
    # The shapes in models.SourceQueryRequest. A connector reached through the
    # wrong field name fails at query time with a confusing error, which is the
    # last step of the walkthrough and the worst place to lose someone.
    assert quickstart.sample_request("postgresql", "users") == {"sql": "SELECT * FROM users"}
    assert quickstart.sample_request("sqlite", "users") == {"sql": "SELECT * FROM users"}
    assert quickstart.sample_request("mysql", "users") == {"sql": "SELECT * FROM users"}
    assert quickstart.sample_request("sqlserver", "users") == {"sql": "SELECT * FROM users"}
    assert quickstart.sample_request("s3", "q1.csv") == {"key": "q1.csv"}
    assert quickstart.sample_request("filesystem", "q1.csv") == {"path": "q1.csv"}
    assert quickstart.sample_request("rest_api", "/orders") == {"path": "/orders"}


def test_config_json_is_merged_over_the_shorthand(quickstart):
    args = _args(quickstart, type="s3", bucket="b", config='{"prefix": "exports/", "region": "us-east-1"}')
    assert quickstart.build_config(args) == {"bucket": "b", "prefix": "exports/", "region": "us-east-1"}


def test_config_alone_can_supply_the_required_key(quickstart):
    args = _args(quickstart, type="s3", config='{"bucket": "b"}')
    assert quickstart.build_config(args) == {"bucket": "b"}


def test_missing_required_flag_exits(quickstart):
    with pytest.raises(SystemExit, match="--dsn is required"):
        quickstart.build_config(_args(quickstart, type="postgresql"))


def test_malformed_config_exits(quickstart):
    with pytest.raises(SystemExit, match="not valid JSON"):
        quickstart.build_config(_args(quickstart, type="s3", bucket="b", config="{oops"))


def _args(quickstart, **overrides):
    import argparse

    fields = {dest: None for dest, _key, _coerce in quickstart.SHORTHAND.values()}
    return argparse.Namespace(**{**fields, "config": None, **overrides})
