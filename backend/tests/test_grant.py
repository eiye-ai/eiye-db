"""scripts/grant.py resolves sources by name or id before writing a policy.

The resolution is the part worth pinning: a grant aimed at the wrong source is
an access-control mistake, and two sources may share a name.

The script lives outside the package, so it is loaded by path rather than
imported.
"""

import importlib.util
from pathlib import Path

import pytest

GRANT = Path(__file__).resolve().parents[2] / "scripts" / "grant.py"

REGISTERED = [
    {"id": "11111111-1111-1111-1111-111111111111", "name": "customers"},
    {"id": "22222222-2222-2222-2222-222222222222", "name": "orders"},
    {"id": "33333333-3333-3333-3333-333333333333", "name": "orders"},
]


@pytest.fixture(scope="module")
def grant():
    spec = importlib.util.spec_from_file_location("grant", GRANT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_resolves_a_name(grant):
    matched, missing = grant.resolve(["customers"], REGISTERED)
    assert matched == [("11111111-1111-1111-1111-111111111111", "customers")] and missing == []


def test_resolves_an_id(grant):
    matched, missing = grant.resolve(["22222222-2222-2222-2222-222222222222"], REGISTERED)
    assert matched == [("22222222-2222-2222-2222-222222222222", "orders")] and missing == []


def test_refuses_an_ambiguous_name(grant):
    """Two sources named 'orders'. Granting whichever sorted first would be a
    silent access-control mistake, so it refuses and asks for an id."""
    matched, missing = grant.resolve(["orders"], REGISTERED)
    assert matched == [] and len(missing) == 1 and "ambiguous" in missing[0]


def test_reports_an_unknown_source(grant):
    matched, missing = grant.resolve(["nope"], REGISTERED)
    assert matched == [] and missing == ["nope"]
