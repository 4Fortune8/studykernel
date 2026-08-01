"""The hint ladder, L0-L5. DESIGN.md §9.

`min_hint_level_to_solve` is the headline competence metric -- continuous
where correctness is binary. A learner who solves at L1 and one who solves at
L4 both score "correct", and only this number tells them apart.

Escalation happens **only on request**. The ladder is not a tutorial that
unrolls; each rung is a floor the learner chose to stand on, which is what
makes the number mean anything.
"""

from __future__ import annotations

from dataclasses import dataclass

MAX_LEVEL = 5


@dataclass(frozen=True)
class Rung:
    level: int
    name: str
    instruction: str


LADDER: tuple[Rung, ...] = (
    Rung(0, "unaided", "No hint. Solve from the stem alone."),
    Rung(
        1,
        "orient",
        "Name the topic and the one concept the item is testing. Reveal nothing "
        "about the solution path.",
    ),
    Rung(
        2,
        "recall",
        "State the relevant rule, formula, or definition in general form. Do not "
        "apply it to this item.",
    ),
    Rung(
        3,
        "first-step",
        "Give only the first move -- what to set up, substitute, or read for -- "
        "and stop there.",
    ),
    Rung(
        4,
        "scaffold",
        "Give the full solution path as ordered steps with the arithmetic or "
        "reasoning at each step left to the learner.",
    ),
    Rung(
        5,
        "worked",
        "Give the complete worked solution. Record that the item was not solved "
        "independently.",
    ),
)


def rung(level: int) -> Rung:
    if not 0 <= level <= MAX_LEVEL:
        raise ValueError(f"hint level must be 0-{MAX_LEVEL}, got {level}")
    return LADDER[level]


def solved_independently(min_hint_level: int) -> bool:
    """L5 means the solution was handed over; nothing was demonstrated."""
    return min_hint_level < MAX_LEVEL


def competence_score(min_hint_level: int, correct: bool) -> float:
    """A [0, 1] competence reading that correctness alone cannot express."""
    if not correct:
        return 0.0
    return max(0.0, (MAX_LEVEL - min_hint_level) / MAX_LEVEL)
