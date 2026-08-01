"""The explain-it-back gate. DESIGN.md §9.

Solution path in the learner's own words, checked, stored. **Mandatory** -- an
item is not resolved without it.

DESIGN.md calls this the single highest-value minute in the loop and the step
every other tool omits because it is friction. Principle 10 says friction on
the diagnostic loop is fatal and names this as the sole exception, because
here the friction *is* the mechanism: articulating the path is what encodes
it. So the gate does not get a skip flag, and this module deliberately offers
no way to add one.

It does offer a way to say "I don't know where to start", which is the
opposite of a skip: it is an answer to the question, it is recorded, and it
makes the exchange teach the item from zero instead of diagnosing a divergence.
The mechanism only works on a learner who *had* a method. Demanding a path from
one who had none does not produce articulation, it produces fiction -- and
fiction reaches the reader indistinguishable from data. See `check`.
"""

from __future__ import annotations

from dataclasses import dataclass

# One floor, and a low one. It was 12 words after a miss and 5 after a hit, on
# the theory that a reconstructed solution path has a length. It does -- but a
# word count cannot tell a real path from a padded one, and the learner who
# pads is precisely the learner the count was aimed at. What the floor actually
# bought was the rejection of empty and one-word submissions, and 3 words buys
# that. Everything above 3 was friction on the diagnostic loop, which principle
# 10 says is fatal: an explanation box that feels like an obstacle produces
# either abandonment or filler, and filler is worse than abandonment because it
# reaches the exchange looking like data.
#
# The floor is not a quality judgement and never was. Whether an explanation is
# any good is settled in the exchange, by a reader who has the item.
MIN_WORDS = 3

# What the gate records when the learner declares they cannot start. This is
# not a skip -- see `check`. It is stored verbatim so the briefing, the
# analytics, and any later reader all see the same declaration rather than an
# empty string that could equally mean "abandoned".
STUCK_DECLARATION = "I don't know where to start."


@dataclass(frozen=True)
class GateResult:
    passed: bool
    reason: str | None = None


def check(
    explanation: str | None, correct: bool = False, stuck: bool = False
) -> GateResult:
    """Local well-formedness check before the explanation goes out for review.

    This catches empty and one-word submissions cheaply. Whether the
    explanation is *correct* is judged in the exchange, by a reader who has the
    item -- not here, and never by the kernel.

    `stuck` is the learner declaring they do not know where to start. It is not
    a skip flag and the distinction is the whole point: a skip removes the
    attempt from the diagnostic loop, whereas this *escalates* it. "I don't
    know where to start" is the single most informative thing a learner can say
    about a miss -- it separates a wrong method from no method, which is the
    difference between a correction and a lesson, and nothing else in the
    capture can express it. The alternative is not a better explanation; it is
    an invented one, and an invented path is worse than silence because the
    briefing reads it as the learner's actual reasoning and diagnoses a
    divergence that never happened.

    So the declaration passes the gate, is recorded as a declaration, and makes
    the exchange teach from zero. What it does not do is let the attempt
    through unmarked.

    `correct` is the verdict on the *answer*. It no longer selects a floor --
    there is only one -- but it still selects what to ask for when the floor is
    missed.
    """
    if stuck:
        return GateResult(True)

    if not explanation or not explanation.strip():
        return GateResult(False, "no explanation given -- the item stays unresolved")

    if len(explanation.split()) < MIN_WORDS:
        wanted = (
            "say why it holds, in a few words"
            if correct
            else "state the path, or say you don't know where to start"
        )
        return GateResult(False, f"explanation is under {MIN_WORDS} words; {wanted}")
    return GateResult(True)


def is_stuck(explanation: str | None) -> bool:
    """Whether a stored explanation is the stuck declaration rather than a path."""
    return (explanation or "").strip() == STUCK_DECLARATION


def resolves(
    explanation: str | None, reviewer_accepted: bool | None, correct: bool = False
) -> bool:
    """An attempt is resolved only when the gate passes and a reader accepts it.

    A stuck declaration resolves on the same terms as any other explanation:
    the learner did their half honestly, and whether the *exchange* closed the
    gap is the reader's call, exactly as it is when a path was written.

    `reviewer_accepted` arrives in the exchange return payload. `None` means
    the exchange has not happened yet, which is not the same as rejection --
    the attempt simply stays open.
    """
    stuck = is_stuck(explanation)
    return check(explanation, correct, stuck).passed and reviewer_accepted is True
