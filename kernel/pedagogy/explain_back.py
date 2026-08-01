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

# Two floors, because the gate asks two different questions.
#
# After a miss it asks for the solution path, which is a reconstruction and has
# a length: what you did, in order, and where it went wrong. 12 words is the
# floor for that. It was 15, which rejected honest two-sentence paths
# ("factored it, set each factor to zero, kept the positive root" is 12) and
# taught the learner to pad instead.
#
# After a hit it asks why the answer holds, and the true answer to that is
# often short -- "both sides are divisible by three" is six words and is a
# complete reason. Holding a *justification* to a word count measures typing,
# and worse, it is the case where padding is most tempting, because the learner
# already knows they were right and the box is the only thing between them and
# the next item. 5 words still stops "I just knew it" and "it's obviously C".
#
# Neither floor is a quality judgement. Whether an explanation is any good is
# settled in the exchange, by a reader who has the item.
MIN_WORDS = 12
MIN_WORDS_CORRECT = 5


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str | None = None


def floor_for(correct: bool) -> int:
    """The word floor that applies to an attempt with this verdict."""
    return MIN_WORDS_CORRECT if correct else MIN_WORDS


def check(explanation: str | None, correct: bool = False) -> GateResult:
    """Local well-formedness check before the explanation goes out for review.

    This catches empty and one-word submissions cheaply. Whether the
    explanation is *correct* is judged in the exchange, by a reader who has the
    item -- not here, and never by the kernel.

    `correct` is the verdict on the *answer*, and it selects the floor rather
    than waiving it. Defaulting it to `False` means the strict floor applies to
    a caller that has not thought about it, which is the right way round.
    """
    if not explanation or not explanation.strip():
        return GateResult(False, "no explanation given -- the item stays unresolved")

    minimum = floor_for(correct)
    if len(explanation.split()) < minimum:
        wanted = (
            "say why it holds, in a sentence"
            if correct
            else "state the path, not the answer"
        )
        return GateResult(False, f"explanation is under {minimum} words; {wanted}")
    return GateResult(True)


def resolves(
    explanation: str | None, reviewer_accepted: bool | None, correct: bool = False
) -> bool:
    """An attempt is resolved only when the gate passes and a reader accepts it.

    `correct` must be the same verdict `check` was called with at submission
    time; passing the gate and then failing to resolve on a stricter floor
    would leave an attempt permanently open for a reason nothing displays.

    `reviewer_accepted` arrives in the exchange return payload. `None` means
    the exchange has not happened yet, which is not the same as rejection --
    the attempt simply stays open.
    """
    return check(explanation, correct).passed and reviewer_accepted is True
