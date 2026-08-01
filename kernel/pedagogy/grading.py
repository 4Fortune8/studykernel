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


CHOICE_LETTERS = "ABCDEFGH"


def choice_values(choices: list[str] | None, key: str | None) -> list[str] | None:
    """What each choice must submit for `grade` to be able to judge it.

    Packs disagree about what a multiple-choice key *is*. The MMLU-derived
    items store a letter (`"C"`); other sources store the option's text. A
    front end offering the options as buttons has to submit whichever form
    this item's grader will recognise, and it cannot ask the key directly --
    `Served` has no key field, which is the point of §4.1.

    So the choice is made here, from the *shape* of the key alone, and the
    whole list is returned. That leaks nothing: "this pack keys by letter" is
    not a fact about which letter is right. Returning the list rather than a
    per-choice answer is what keeps it that way -- there is no call here that
    takes one choice and says something about it.

    A key that is neither a letter nor one of the options is not gradable as a
    selection, so the caller gets `None` and should fall back to a text box
    rather than render options that cannot match.
    """
    if not choices:
        return None
    letters = list(CHOICE_LETTERS[: len(choices)])
    normalized = normalize_answer(key)
    if normalized in [letter.casefold() for letter in letters]:
        return letters
    if any(normalized == normalize_answer(choice) for choice in choices):
        return list(choices)
    return None


# How a single option relates to the verdict, for a front end to mark it with.
KEY = "key"
GIVEN = "given"
BOTH = "both"


def mark_choices(
    values: list[str] | None, key: str | None, given: str | None
) -> list[str | None]:
    """Label each option `KEY`, `GIVEN`, `BOTH`, or `None`, in order.

    Computed here and not in a template because deciding whether an option
    *is* the key is answer matching, and answer matching is this module's job.
    A template comparing strings would miss the normalization -- a key of
    `"c"`, or an option keyed by text with different spacing -- and would then
    disagree with the verdict printed directly beside it. Two contradictory
    accounts of the same attempt on one screen is worse than showing neither.

    `values` is `choice_values` output, so the comparison is against whatever
    form this item's grader actually recognises.
    """
    marks: list[str | None] = []
    for value in values or []:
        is_key = grade(value, key)
        is_given = bool(given) and grade(value, given)
        if is_key and is_given:
            marks.append(BOTH)
        elif is_key:
            marks.append(KEY)
        elif is_given:
            marks.append(GIVEN)
        else:
            marks.append(None)
    return marks


NUMERIC = "numeric"

# What a numeric answer box will accept. Written to admit everything `grade`
# can already read -- integers, decimals, leading-dot decimals, thousands
# commas, a sign, `a/b`, `\frac{a}{b}`, and the `$`/`%` wrappers
# `normalize_answer` strips -- so the guard rail never rejects an answer that
# would have been marked correct. What it excludes is letters, which is the
# only way a right answer gets typed wrong here: writing back the variable you
# solved for instead of the value you solved it to.
_NUM = r"[+\-]?[\d,]*\.?\d+"
_TEX_FRAC = rf"\\[dt]?frac\s*\{{\s*{_NUM}\s*\}}\s*\{{\s*{_NUM}\s*\}}"
NUMERIC_INPUT_PATTERN = (
    rf"\s*\$?\s*(?:[+\-](?!\s*[+\-])\s*)?(?:{_TEX_FRAC}|{_NUM}(?:\s*/\s*{_NUM})?)\s*\\?%?\.?\s*"
)


def input_shape(key: str | None) -> str | None:
    """What kind of box this item's answer fits in. `None` means any box.

    The same §4.1 argument as `choice_values`, and the same limit on it. This
    reads the key's *shape* and returns a constant, never a value: `13`, `-0.5`
    and `\\frac{1}{2}` all yield `NUMERIC`, so the box cannot be read backwards
    for the magnitude, the sign, or even whether the answer is a whole number.

    It is not free of information -- a numeric box does say "this answer is a
    number", which for a stem that could have wanted an expression is a nudge
    the learner did not earn. That is the trade being made, and it is made
    deliberately: an item marked failed because the learner typed `N` when they
    had already worked out `N = 13` is a false signal in the competence data,
    and false signals cost more than the nudge does.
    """
    text = normalize_answer(key)
    if not text:
        return None
    return NUMERIC if _as_number(text) is not None else None


def is_gradable(key: str | None) -> bool:
    """Whether an item can be graded deterministically at all.

    Used at ingest: items without a verifiable key are dropped rather than
    stored and skipped later.
    """
    return bool(normalize_answer(key))
