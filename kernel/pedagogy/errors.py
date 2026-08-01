"""The core error taxonomy. DESIGN.md §5.

The kernel defines the codes; **the product supplies the weights.** v0.2 got
this wrong by treating weights as kernel constants. The same
`execution_error` is noise-tier on a long timed exam and catastrophic on an
item-adaptive test with twenty items of runway, where one careless miss costs
the item *plus* downward routing.

Codes are a fixed enum because freeform diagnostics are unaggregatable
(principle 6). Products may add codes and override weights; they may not
remove a core code, since analytics compare across products.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorCode:
    code: str
    description: str


CORE_CODES: tuple[ErrorCode, ...] = (
    ErrorCode("knowledge_gap", "The required rule or concept was not known."),
    ErrorCode("trigger_miss", "The rule was known but its cue was not recognized."),
    ErrorCode("execution_error", "Correct approach, botched arithmetic or algebra."),
    ErrorCode("misread", "Misread the stem, a qualifier, or the question asked."),
    ErrorCode("distractor_trap", "Chose a deliberately attractive wrong option."),
    ErrorCode("incomplete", "Stopped before answering what was actually asked."),
    ErrorCode("prerequisite_gap", "Failed on an upstream skill, not this tag's skill."),
    ErrorCode("time_pressure", "Knew it, ran out of time or rushed."),
    ErrorCode("guess", "No usable reasoning; selected at random."),
    ErrorCode("none", "Correct, no error to diagnose."),
)

CORE_CODE_NAMES = frozenset(c.code for c in CORE_CODES)

# Neutral default. A product that does not override these is telling the
# allocator every error type costs the same, which is almost never true.
DEFAULT_WEIGHT = 1.0


def validate_code(code: str, product_codes: frozenset[str] = frozenset()) -> None:
    if code not in CORE_CODE_NAMES and code not in product_codes:
        raise ValueError(
            f"unknown error code {code!r}; core codes are {sorted(CORE_CODE_NAMES)}"
        )


def weight(code: str, product_weights: dict[str, float]) -> float:
    return product_weights.get(code, DEFAULT_WEIGHT)


def describe() -> str:
    """The enum, rendered for a briefing prompt."""
    return "\n".join(f"  {c.code}: {c.description}" for c in CORE_CODES)
