"""PII detection and redaction tests."""

import pytest

from eiye_db import pii
from eiye_db.config import settings
from eiye_db.pii import luhn_valid, redact_structure, redact_text


def test_redacts_email():
    text, counts = redact_text("contact alice@example.com now")
    assert text == "contact [REDACTED:email] now"
    assert counts == {"email": 1}


def test_redacts_ssn():
    text, counts = redact_text("SSN: 123-45-6789")
    assert "[REDACTED:ssn]" in text
    assert counts == {"ssn": 1}


def test_redacts_phone():
    text, _ = redact_text("call (617) 555-1234 or 617-555-9876")
    assert text.count("[REDACTED:phone]") == 2


def test_redacts_valid_credit_card_only():
    # 4532015112830366 passes Luhn; 1234567812345678 does not
    text, counts = redact_text("card 4532015112830366 vs 1234567812345678")
    assert "[REDACTED:credit_card]" in text
    assert "1234567812345678" in text
    assert counts == {"credit_card": 1}


def test_luhn():
    assert luhn_valid("4532015112830366")
    assert not luhn_valid("1234567812345678")


def test_redacts_ipv4():
    text, _ = redact_text("host 192.168.1.100 responded")
    assert text == "host [REDACTED:ipv4] responded"


def test_clean_text_untouched():
    text, counts = redact_text("nothing sensitive here")
    assert text == "nothing sensitive here"
    assert counts == {}


def test_redacts_pii_in_dict_keys():
    obj = {"alice@example.com": {"balance": 100}}
    redacted, counts = redact_structure(obj)
    assert "alice@example.com" not in redacted
    assert redacted["[REDACTED:email]"] == {"balance": 100}
    assert counts == {"email": 1}


def test_redacts_numeric_credit_card():
    # card number stored as an int (e.g. a bigint column)
    redacted, counts = redact_structure([{"card": 4532015112830366}])
    assert redacted[0]["card"] == "[REDACTED:credit_card]"
    assert counts == {"credit_card": 1}


def test_bool_and_plain_numbers_untouched():
    redacted, counts = redact_structure([{"active": True, "count": 42, "ratio": 3.14}])
    assert redacted[0] == {"active": True, "count": 42, "ratio": 3.14}
    assert counts == {}


def test_redact_structure_recurses():
    obj = {
        "user": {"email": "bob@corp.io", "name": "Bob"},
        "notes": ["ip is 10.0.0.1", 42, None],
    }
    redacted, counts = redact_structure(obj)
    assert redacted["user"]["email"] == "[REDACTED:email]"
    assert redacted["user"]["name"] == "Bob"
    assert redacted["notes"][0] == "ip is [REDACTED:ipv4]"
    assert redacted["notes"][1] == 42
    assert counts == {"email": 1, "ipv4": 1}


@pytest.fixture
def ner_on(monkeypatch):
    spacy = pytest.importorskip("spacy")
    try:
        spacy.load(settings.pii_ner_model)
    except OSError:
        pytest.skip(f"spaCy model {settings.pii_ner_model!r} not installed")
    pii._load_ner.cache_clear()
    monkeypatch.setattr(settings, "pii_ner_enabled", True)
    yield
    pii._load_ner.cache_clear()


def test_ner_disabled_by_default_keeps_names():
    # With NER off (the default), a bare name/city is not redacted.
    redacted, counts = redact_structure([{"name": "Dave Hernandez", "city": "Boston"}])
    assert redacted[0] == {"name": "Dave Hernandez", "city": "Boston"}
    assert "name" not in counts and "location" not in counts


def test_ner_redacts_person_and_location(ner_on):
    redacted, counts = redact_structure([{"name": "Dave Hernandez", "email": "d@x.com", "city": "Boston"}])
    assert redacted[0]["name"] == "[REDACTED:name]"
    assert redacted[0]["email"] == "[REDACTED:email]"  # regex baseline still runs alongside NER
    assert redacted[0]["city"] == "[REDACTED:location]"
    assert counts["name"] == 1 and counts["location"] == 1 and counts["email"] == 1


def test_ner_enabled_but_unusable_fails_loud(monkeypatch):
    # Enabled + unusable must raise, never silently leave PII in. Two distinct
    # causes, same requirement: the model isn't installed (spaCy raises OSError)
    # or spaCy itself isn't (ImportError, the shape CI runs in — the `ner` extra
    # is optional). Asserting both keeps this invariant covered where the other
    # NER tests skip.
    pii._load_ner.cache_clear()
    monkeypatch.setattr(settings, "pii_ner_enabled", True)
    monkeypatch.setattr(settings, "pii_ner_model", "does_not_exist_model_xyz")
    with pytest.raises((OSError, ImportError)):
        pii.redact_entities("Alice Johnson called from Boston")
    pii._load_ner.cache_clear()


def test_ner_scans_full_string_beyond_window(ner_on, monkeypatch):
    # Regression: a name past pii_ner_max_chars must NOT silently leak (no cap-drop).
    monkeypatch.setattr(settings, "pii_ner_max_chars", 24)
    text = "filler filler filler filler Alice Johnson called"
    redacted, counts = pii.redact_entities(text)
    assert "Alice Johnson" not in redacted
    assert counts.get("name", 0) >= 1


def test_ner_name_adjacent_to_regex_hit(ner_on):
    # Regression: NER runs on the original text, so a name beside an email is still caught.
    redacted, _ = redact_structure([{"note": "Contact Sarah Johnson at sarah@corp.io today"}])
    note = redacted[0]["note"]
    assert "[REDACTED:name]" in note and "[REDACTED:email]" in note
    assert "Sarah" not in note and "Johnson" not in note
