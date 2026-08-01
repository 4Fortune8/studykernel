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

# Every field the kernel knows how to capture. Products choose a subset.
KNOWN_FIELDS: dict[str, str] = {
    "confidence": "How sure are you, 1 (guess) to 3 (certain)?",
    "rationale": "In two sentences, why is your answer right?",
    "verification_method": (
        "Before submitting: how did you check this? "
        "(back-substitution / magnitude estimate / re-read the qualifier / unit check)"
    ),
    "predicted_difficulty": "Harder or easier than your average item on this tag?",
}

REQUIRED_ALWAYS = ("confidence", "rationale")


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
        if not capture.verification_method or not capture.verification_method.strip():
            raise CaptureError(
                "verification_method is enabled for this product: state how you "
                "checked the answer before submitting"
            )
