"""Deterministic grading. The model never grades. Ever. (DESIGN.md §9.)

Normalization happens on the *grading* side, not the storage side -- stems and
keys are stored exactly as the source wrote them, and comparison normalizes
both sides at read time. DATA_SOURCING_MATH.md §5 specifies the rules; they
live here so every importer inherits them instead of each reimplementing a
slightly different `\\frac` handler.

Anything that would require fuzzy matching is not graded at all. An item whose
key cannot be compared exactly is dropped at ingest, because a grader that
sometimes guesses is worse than no item.
"""

from __future__ import annotations

import re
import unicodedata

_FRAC = re.compile(r"\\[dt]?frac\s*\{([^{}]+)\}\s*\{([^{}]+)\}")
_STRIP_WRAPPERS = re.compile(r"[$\\\s]|\\left|\\right|\\!|\\,")


def normalize_answer(raw: str | None) -> str:
    """Canonical form for comparison. Returns '' for a missing answer."""
    if raw is None:
        return ""
    text = unicodedata.normalize("NFKC", str(raw)).strip()

    # \frac{a}{b} -> a/b, repeatedly for nested cases.
    for _ in range(4):
        new = _FRAC.sub(r"\1/\2", text)
        if new == text:
            break
        text = new

    text = text.replace("\\%", "").replace("%", "")
    text = _STRIP_WRAPPERS.sub("", text)
    # Thousands separators, but not decimal commas in "1,5" -- only strip a
    # comma that sits between three trailing digits.
    text = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
    text = text.rstrip(".")
    return text.casefold()


def _as_number(text: str) -> float | None:
    try:
        if "/" in text:
            num, _, den = text.partition("/")
            return float(num) / float(den)
        return float(text)
    except (ValueError, ZeroDivisionError):
        return None


def grade(given: str | None, key: str | None) -> bool:
    """Exact comparison after normalization, with numeric equivalence.

    Numeric equivalence exists so `0.5`, `1/2` and `.50` all match a key of
    `1/2`. It is not fuzzy matching: two values either are the same number or
    they are not.
    """
    g, k = normalize_answer(given), normalize_answer(key)
    if not k:
        raise ValueError("cannot grade an item with no answer key")
    if g == k:
        return True

    gn, kn = _as_number(g), _as_number(k)
    if gn is not None and kn is not None:
        return abs(gn - kn) < 1e-9
    return False


def is_gradable(key: str | None) -> bool:
    """Whether an item can be graded deterministically at all.

    Used at ingest: items without a verifiable key are dropped rather than
    stored and skipped later.
    """
    return bool(normalize_answer(key))
