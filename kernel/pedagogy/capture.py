"""Pre-answer capture. DESIGN.md §9.

Confidence (1-3) plus a two-sentence rationale, written **blind**, before the
answer is revealed. The rationale is the raw material for divergence
diagnosis; without it the tutor is guessing at why the learner went wrong
instead of reading it.

Which fields are active is product configuration, not a kernel constant
(DESIGN.md §5). `verification_method` exists because an untimed exam makes
checking free and therefore trainable -- and is meaningless under time
pressure. The kernel supports the field; a product turns it on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class VerificationMethod:
    """One way of checking an answer, named so it can be counted.

    `detail` is not decoration. A closed set the learner cannot read is a set
    they tick at random, which is worse than the free-text box it replaced --
    random ticks look like data. The detail says what the method actually
    involves, so the tick means the learner did *that*.
    """

    slug: str
    label: str
    detail: str


# The methods a learner can claim. Closed on purpose: this field's whole
# value is being countable. Free text produced "math", "wowowowowow" and
# "Not sure what to put here" -- none of which can be read as a habit, and
# all of which cost the learner a decision at the friction-sensitive moment
# right before submitting (DESIGN.md principle 10).
#
# `NO_CHECK` is the point of the exercise, not an escape hatch: an untimed
# exam makes checking free, so "didn't check" paired with a wrong answer
# coded `execution_error` or `misread` is the preventable loss the product
# exists to attack (DESIGN.md §13.1). It has to be as easy to say as the
# others or it gets papered over with a plausible-sounding lie.
VERIFICATION_METHODS: tuple[VerificationMethod, ...] = (
    VerificationMethod(
        "back_substitution",
        "Back-substitution",
        "Put the answer back into the original equation or condition and "
        "confirmed it actually holds.",
    ),
    VerificationMethod(
        "recomputed",
        "Worked it again",
        "Redid the computation -- ideally by a different route -- and landed "
        "on the same result.",
    ),
    VerificationMethod(
        "magnitude_estimate",
        "Size check",
        "Asked whether the answer is even the right size and sign. A ballpark "
        "estimate, not an exact redo.",
    ),
    VerificationMethod(
        "unit_check",
        "Units and form",
        "Confirmed the answer is in the form asked for: right units, percent "
        "vs decimal, area vs perimeter.",
    ),
    VerificationMethod(
        "reread_qualifier",
        "Re-read the question",
        "Went back to the stem for the word that sets the task -- not, least, "
        "except, approximately -- and confirmed I answered that question.",
    ),
    VerificationMethod(
        "checked_source",
        "Checked the text",
        "Went back to the passage and found the specific line that supports "
        "the answer.",
    ),
    VerificationMethod(
        "none",
        "I didn't check",
        "Submitted without checking it. Say so -- on an untimed exam this is "
        "the most useful thing this field can record about you.",
    ),
)

NO_CHECK = "none"

VERIFICATION_BY_SLUG: dict[str, VerificationMethod] = {
    m.slug: m for m in VERIFICATION_METHODS
}

# Every field the kernel knows how to capture. Products choose a subset.
KNOWN_FIELDS: dict[str, str] = {
    "confidence": "How sure are you, 1 (guess) to 3 (certain)?",
    "rationale": "In two sentences, why is your answer right?",
    "verification_method": (
        "Before submitting: how did you check this? Name one or more, "
        "comma-separated -- "
        + ", ".join(m.slug for m in VERIFICATION_METHODS)
    ),
    "predicted_difficulty": "Harder or easier than your average item on this tag?",
}

REQUIRED_ALWAYS = ("confidence", "rationale")


def parse_verification(raw: Any) -> str | None:
    """Normalise whatever a front end submitted into canonical slugs.

    Takes a list (checkbox group), a comma-separated string (the CLI, and the
    round trip through the database) or `None`. Returns the slugs joined in
    `VERIFICATION_METHODS` order so that two learners who ticked the same
    boxes produce the same string and the values can be grouped by equality.

    Unknown tokens are passed through rather than dropped, because `validate`
    is where a bad value gets rejected -- silently discarding it here would
    turn a typo into an empty field and blame the learner for a blank.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        tokens = [t.strip() for t in raw.split(",")]
    else:
        tokens = [str(t).strip() for t in raw]

    seen = [t for t in tokens if t]
    if not seen:
        return None

    order = {m.slug: i for i, m in enumerate(VERIFICATION_METHODS)}
    known = sorted({t for t in seen if t in order}, key=lambda t: order[t])
    unknown = [t for t in seen if t not in order]
    return ",".join(known + unknown)


def was_checked(value: str | None) -> bool | None:
    """Did this attempt get checked? `None` when the row cannot say.

    Three outcomes, not two. Rows written before the field closed hold free
    text -- "math", "non", "Not sure what to put here" -- and none of that can
    be read as either a check or the absence of one. Counting them as checked
    would flatter the number the report exists to show; counting them as
    unchecked would invent a failure the learner may not have had. They are
    excluded and said out loud instead.
    """
    if not value or not value.strip():
        return None
    slugs = [s.strip() for s in value.split(",") if s.strip()]
    if any(s not in VERIFICATION_BY_SLUG for s in slugs):
        return None
    return slugs != [NO_CHECK]


def describe_verification(value: str | None) -> str | None:
    """Render a stored value as prose, for a human or a tutor to read.

    Falls back to the raw string for anything not in the set, which is what
    rows written before the field closed look like.
    """
    if not value:
        return None
    labels = [
        VERIFICATION_BY_SLUG[slug].label if slug in VERIFICATION_BY_SLUG else slug
        for slug in (v.strip() for v in value.split(","))
        if slug
    ]
    return ", ".join(labels)


@dataclass
class Capture:
    confidence: int | None = None
    rationale: str | None = None
    verification_method: str | None = None
    predicted_difficulty: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "confidence": self.confidence,
            "rationale": self.rationale,
            "verification_method": self.verification_method,
        }


class CaptureError(ValueError):
    """Raised when a capture is incomplete or malformed."""


def active_fields(product_config: dict) -> list[str]:
    """Capture fields this product turns on, always including the required two."""
    configured = list((product_config.get("capture") or {}).get("fields", []))
    unknown = [f for f in configured if f not in KNOWN_FIELDS]
    if unknown:
        raise CaptureError(f"product enables unknown capture field(s): {unknown}")
    ordered = list(REQUIRED_ALWAYS) + [f for f in configured if f not in REQUIRED_ALWAYS]
    return ordered


def validate(capture: Capture, fields: list[str]) -> None:
    """Reject a capture that would produce an undiagnosable attempt.

    Strict on purpose. DESIGN.md principle 10 says friction on the diagnostic
    loop is fatal, and this is friction -- but a blank rationale makes the
    entire downstream diagnosis worthless, so the field is cheap to fill and
    expensive to skip.
    """
    if "confidence" in fields:
        if capture.confidence not in (1, 2, 3):
            raise CaptureError("confidence must be 1, 2, or 3")
    if "rationale" in fields:
        if not capture.rationale or len(capture.rationale.strip()) < 10:
            raise CaptureError(
                "rationale must be a real sentence -- it is what the diagnosis reads"
            )
    if "verification_method" in fields:
        raw = (capture.verification_method or "").strip()
        if not raw:
            raise CaptureError(
                "verification_method is enabled for this product: say how you "
                "checked the answer before submitting, or that you didn't"
            )
        slugs = [s.strip() for s in raw.split(",") if s.strip()]
        unknown = [s for s in slugs if s not in VERIFICATION_BY_SLUG]
        if unknown:
            raise CaptureError(
                f"unknown verification method(s): {', '.join(unknown)} -- "
                f"choose from {', '.join(m.slug for m in VERIFICATION_METHODS)}"
            )
        # "I didn't check" and "here is how I checked" cannot both be true, and
        # a row claiming both is a row the didn't-check count has to guess at.
        if NO_CHECK in slugs and len(slugs) > 1:
            raise CaptureError(
                "you cannot both check the answer and not check it -- pick the "
                "methods you used, or 'I didn't check' on its own"
            )
