"""PII detection and redaction: regex baseline + optional spaCy NER (names/locations)."""

import re
from functools import lru_cache
from typing import Any

from eiye_db.config import settings

PATTERNS: dict[str, re.Pattern] = {
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b(?:\+?1[\s.-])?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    "credit_card": re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    "ipv4": re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.){3}(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\b"),
}


def luhn_valid(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)]
    if len(digits) < 13:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def redact_text(text: str) -> tuple[str, dict[str, int]]:
    """Replace detected PII with [REDACTED:<type>] tokens; return counts by type."""
    counts: dict[str, int] = {}

    for pii_type, pattern in PATTERNS.items():
        def _sub(m: re.Match) -> str:
            if pii_type == "credit_card" and not luhn_valid(m.group()):
                return m.group()
            counts[pii_type] = counts.get(pii_type, 0) + 1
            return f"[REDACTED:{pii_type}]"

        text = pattern.sub(_sub, text)
    return text, counts


# spaCy NER entity labels we treat as PII, mapped to our redaction type.
_NER_LABELS = {"PERSON": "name", "GPE": "location", "LOC": "location", "FAC": "location"}


@lru_cache(maxsize=1)
def _load_ner():
    # Only the NER pipe is needed; disabling the rest speeds inference several-fold.
    # Raises loudly if unusable — never a silent fail-open: OSError when the model
    # isn't installed, ImportError when the optional `ner` extra isn't.
    import spacy

    return spacy.load(settings.pii_ner_model, disable=["tagger", "parser", "lemmatizer", "attribute_ruler"])


def redact_entities(text: str) -> tuple[str, dict[str, int]]:
    """Mask PERSON/LOCATION spans via spaCy NER. No-op unless pii_ner_enabled.

    The whole string is scanned in windows of pii_ner_max_chars (which bounds the
    cost of a single NER pass), chunked on line/space boundaries so entities are
    not split. Nothing past a cap is silently skipped — no fail-open on the tail.
    """
    if not settings.pii_ner_enabled or not text.strip():
        return text, {}
    nlp = _load_ner()
    window = max(1, settings.pii_ner_max_chars)
    spans: list[tuple[int, int, str]] = []
    pos, n = 0, len(text)
    while pos < n:
        end = min(pos + window, n)
        if end < n:
            # Prefer a newline boundary, then a space, so a name isn't split across windows.
            boundary = text.rfind("\n", pos, end)
            if boundary <= pos:
                boundary = text.rfind(" ", pos, end)
            if boundary > pos:
                end = boundary
        doc = nlp(text[pos:end])
        for e in doc.ents:
            if e.label_ in _NER_LABELS:
                spans.append((pos + e.start_char, pos + e.end_char, _NER_LABELS[e.label_]))
        pos = end if end > pos else n
    if not spans:
        return text, {}
    counts: dict[str, int] = {}
    # Replace right-to-left so earlier character offsets stay valid.
    for start, end, kind in sorted(spans, reverse=True):
        text = text[:start] + f"[REDACTED:{kind}]" + text[end:]
        counts[kind] = counts.get(kind, 0) + 1
    return text, counts


def redact_structure(obj: Any) -> tuple[Any, dict[str, int]]:
    """Recursively redact PII in all strings of a JSON-like structure."""
    totals: dict[str, int] = {}

    def _merge(counts: dict[str, int]) -> None:
        for k, v in counts.items():
            totals[k] = totals.get(k, 0) + v

    def _walk(value: Any) -> Any:
        if isinstance(value, str):
            # NER first, on the ORIGINAL text: regex substitution would strip the
            # linguistic context spaCy needs to tag names next to an email/SSN.
            redacted, ent_counts = redact_entities(value)
            _merge(ent_counts)
            redacted, counts = redact_text(redacted)
            _merge(counts)
            return redacted
        # Numeric PII, e.g. a card number stored as a bigint. bool is an int
        # subclass, so exclude it. Only convert to str when redaction fires;
        # ordinary numbers keep their type. Note: bare-digit SSN/phone without
        # separators are not matched by the regex baseline (NER is post-wedge).
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            redacted, counts = redact_text(str(value))
            if counts:
                _merge(counts)
                return redacted
            return value
        if isinstance(value, dict):
            # Redact keys as well as values — REST sources can return objects
            # keyed by PII (e.g. {"alice@example.com": {...}}).
            return {_walk(k): _walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_walk(v) for v in value]
        return value

    return _walk(obj), totals
