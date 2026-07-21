"""Entity resolution: deterministic cross-source matching of entity names.

Ported from OpenPlanter's investigation scripts (entity_resolution.py), then
hardened: OpenPlanter stripped descriptive words (GROUP, SERVICES, HOLDINGS…)
before its exact tier, which merges genuinely different entities ("Acme
Holdings" vs "Acme Partners") at top confidence. Here only identity-free
LEGAL forms (LLC, INC, CORP…) are stripped for the exact tier; descriptive
words are ignored solely at a dedicated low tier that discloses what it
ignored. Stdlib-only by design.

Matches are ANALYSIS OUTPUT, not governed truth: they are computed per call
over two governed query results and never persisted. That keeps row-level
values (potentially PII) out of the metadata store — the draft→approve→enforce
loop governs schema-level semantics, not row matches. Both input sides arrive
through run_query, so matching only ever sees what the caller could see.
"""

import re
import unicodedata

# Legal boilerplate carries no identity: "ACME LLC" and "ACME, Inc." are the
# same entity for matching purposes.
_LEGAL = [
    r"\bINC\.?\b", r"\bLLC\.?\b", r"\bL\.?L\.?C\.?\b", r"\bCORP\.?\b",
    r"\bCORPORATION\b", r"\bINCORPORATED\b", r"\bCO\.?\b", r"\bCOMPANY\b",
    r"\bLTD\.?\b", r"\bLIMITED\b",
]
# Descriptive words are weaker evidence but still distinguish entities
# ("Acme Holdings" vs "Acme Partners" may be different firms), so they are
# never stripped from the primary form — only ignored at the low "core name"
# tier, which discloses what it ignored.
_DESCRIPTIVE = {
    "GROUP", "SERVICE", "SERVICES", "ENTERPRISE", "ENTERPRISES",
    "HOLDING", "HOLDINGS", "INTERNATIONAL", "AMERICA", "AMERICAS",
    "ASSOCIATE", "ASSOCIATES", "PARTNER", "PARTNERS", "SOLUTION",
    "SOLUTIONS", "TECHNOLOGY", "TECHNOLOGIES", "CONSULTING", "MANAGEMENT",
}
_LEGAL_RE = re.compile("|".join(_LEGAL))
_PUNCT_RE = re.compile(r"[.,;:!@#$%^&*()_\-+=\[\]{}|\\/<>~`\"']")
_WS_RE = re.compile(r"\s+")

# Redaction markers must never participate in matching: two redacted emails
# would "exact match" while telling us nothing about the underlying entities.
_REDACTED = "[REDACTED"

_MIN_NORM_LEN = 3  # normalized names shorter than this are too generic
_MIN_TOKEN_LEN = 4  # tokens shorter than this (OF, THE…) carry no signal
_OVERLAP_RATIO = 0.6  # share of the shorter name's tokens that must overlap
_MIN_OVERLAP = 2  # and at least this many significant tokens
_MAX_VALUE_LEN = 500  # entity names aren't longer; bounds matching cost


def normalize_entity(name: str) -> str:
    """'Acme Widgets, LLC' -> 'ACME WIDGETS'. Only legal forms are stripped."""
    name = unicodedata.normalize("NFKC", name).upper().strip()
    name = _LEGAL_RE.sub("", name)
    name = _PUNCT_RE.sub(" ", name)
    return _WS_RE.sub(" ", name).strip()


def _core(norm: str) -> str:
    """Name minus descriptive words: 'ACME HOLDINGS' -> 'ACME'."""
    return " ".join(t for t in norm.split() if t not in _DESCRIPTIVE)


def _significant(norm: str) -> frozenset:
    return frozenset(t for t in norm.split() if len(t) >= _MIN_TOKEN_LEN)


def _usable(v) -> tuple[str, str] | None:
    """(original, normalized) if the value can name an entity, else None."""
    if not isinstance(v, str):
        return None  # dicts/lists/numbers from connectors are not entity names
    s = v.strip()[:_MAX_VALUE_LEN]
    if not s or _REDACTED in s:
        return None
    norm = normalize_entity(s)
    if len(norm) < _MIN_NORM_LEN or not any(c.isalpha() for c in norm):
        return None  # too generic, or purely numeric (an ID, not a name)
    return s, norm


def _distinct(values: list) -> dict[str, list[str]]:
    """Distinct usable values: normalized form -> every original spelling.

    All colliding spellings are kept (first is the representative) so a
    collision surfaces as variants instead of silently vanishing.
    """
    out: dict[str, list[str]] = {}
    for v in values:
        u = _usable(v)
        if u:
            s, norm = u
            spellings = out.setdefault(norm, [])
            if s not in spellings:
                spellings.append(s)
    return out


def match_values(left: list, right: list) -> dict:
    """Match two value lists; each left entity gets its best right match.

    Tiers (first hit wins):
    - exact (high): identical after legal-form normalization
    - token_set (medium): same words with the same multiplicity, reordered
    - core (low): identical only after ignoring descriptive words — disclosed
    - token_overlap (low): >60% of the shorter name's significant tokens
      shared, minimum 2

    Returns {"matches": [...], "left_distinct": n, "right_distinct": n};
    matches are sorted best-first and carry every colliding spelling.
    """
    left_d, right_d = _distinct(left), _distinct(right)

    # All lookups are built in sorted-norm order so collisions resolve
    # deterministically, not by dict iteration order.
    right_by_tokens: dict[str, str] = {}
    right_by_core: dict[str, str] = {}
    for rnorm in sorted(right_d):
        right_by_tokens.setdefault(" ".join(sorted(rnorm.split())), rnorm)
        core = _core(rnorm)
        if len(core) >= _MIN_NORM_LEN:
            right_by_core.setdefault(core, rnorm)
    right_sig = {rnorm: _significant(rnorm) for rnorm in right_d}
    token_index: dict[str, set[str]] = {}
    for rnorm, sig in right_sig.items():
        for tok in sig:
            token_index.setdefault(tok, set()).add(rnorm)

    matches = []
    for norm in sorted(left_d):
        if norm in right_d:
            matches.append((0, norm, norm, "exact", "high", "identical after normalization"))
            continue
        tokens = " ".join(sorted(norm.split()))
        if tokens in right_by_tokens:
            matches.append((1, norm, right_by_tokens[tokens], "token_set", "medium", "same words, different order"))
            continue
        core = _core(norm)
        if len(core) >= _MIN_NORM_LEN and core in right_by_core:
            rnorm = right_by_core[core]
            ignored = sorted({t for t in norm.split() + rnorm.split() if t in _DESCRIPTIVE})
            matches.append(
                (2, norm, rnorm, "core", "low", f"same name ignoring descriptive words ({', '.join(ignored)})")
            )
            continue
        lsig = _significant(norm)
        candidates: set[str] = set()
        for tok in lsig:
            candidates |= token_index.get(tok, set())
        best_overlap, best_norm = 0, None
        for rnorm in sorted(candidates):
            rsig = right_sig[rnorm]
            overlap = len(lsig & rsig)
            if overlap / min(len(lsig), len(rsig)) > _OVERLAP_RATIO and overlap > best_overlap:
                best_overlap, best_norm = overlap, rnorm
        if best_norm and best_overlap >= _MIN_OVERLAP:
            matches.append(
                (3, norm, best_norm, "token_overlap", "low", f"{best_overlap} significant tokens shared")
            )

    matches.sort(key=lambda m: (m[0], m[1]))
    return {
        "matches": [
            {
                "left_value": left_d[lnorm][0],
                "left_variants": left_d[lnorm],
                "right_value": right_d[rnorm][0],
                "right_variants": right_d[rnorm],
                "normalized": lnorm,
                "match_type": match_type,
                "confidence": confidence,
                "rationale": rationale,
            }
            for _, lnorm, rnorm, match_type, confidence, rationale in matches
        ],
        "left_distinct": len(left_d),
        "right_distinct": len(right_d),
    }
