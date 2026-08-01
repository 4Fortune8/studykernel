"""The explain-it-back gate. DESIGN.md §9.

Solution path in the learner's own words, checked, stored. **Mandatory** -- an
item is not resolved without it.

DESIGN.md calls this the single highest-value minute in the loop and the step
every other tool omits because it is friction. Principle 10 says friction on
the diagnostic loop is fatal and names this as the sole exception, because
here the friction *is* the mechanism: articulating the path is what encodes
it. So the gate does not get a skip flag, and this module deliberately offers
no way to add one.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_WORDS = 15


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str | None = None


def check(explanation: str | None) -> GateResult:
    """Local well-formedness check before the explanation goes out for review.

    This catches empty and one-word submissions cheaply. Whether the
    explanation is *correct* is judged in the exchange, by a reader who has the
    item -- not here, and never by the kernel.
    """
    if not explanation or not explanation.strip():
        return GateResult(False, "no explanation given -- the item stays unresolved")
    if len(explanation.split()) < MIN_WORDS:
        return GateResult(
            False,
            f"explanation is under {MIN_WORDS} words; state the path, not the answer",
        )
    return GateResult(True)


def resolves(explanation: str | None, reviewer_accepted: bool | None) -> bool:
    """An attempt is resolved only when the gate passes and a reader accepts it.

    `reviewer_accepted` arrives in the exchange return payload. `None` means
    the exchange has not happened yet, which is not the same as rejection --
    the attempt simply stays open.
    """
    return check(explanation).passed and reviewer_accepted is True
