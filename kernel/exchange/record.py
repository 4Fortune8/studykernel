"""Recording the inbound half of the exchange. DESIGN.md §10.

One structured JSON block returns; `study record` parses it and **rejects on
`item_id` mismatch**. That check is the whole reason the protocol is safe
without an API: it is what stops a diagnosis for one item landing on another
after a tab switch.

Structured output or it didn't happen (principle 6). Freeform diagnostics are
unaggregatable, so an unparseable return is an error rather than something to
salvage with a regex.

The honesty tradeoff carries forward from DESIGN.md §10: under `pinned` a user
can read the explanation and self-report L1. Accepted, not engineered against
-- the user is subject and sole beneficiary, and corrupting the log corrupts
only their own diagnostics.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from kernel.pedagogy import errors

_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


class RecordError(ValueError):
    """Raised when a returned payload is unusable."""


@dataclass
class Diagnosis:
    item_id: str
    error_code: str
    one_fix: str
    # Where *this* attempt broke, in the learner's own words, or the standard
    # "you didn't show your steps" sentence. Distinct from `one_fix`, which is
    # the habit to change: a fix generalises and is what the learner carries to
    # the next item, while this is the only output in the whole exchange that
    # is about the line they actually wrote. Optional because the field is
    # newer than the schema and old payloads must still record.
    divergence: str | None = None
    # The worked solution. Named against `explain_back` on the same record,
    # which is the learner's: this one is the reader's. Bounded by the item's
    # explanation policy, so on a `pinned_strict` or `anchored` item it is a
    # restatement of official or quoted text rather than derived reasoning.
    explanation: str | None = None
    prerequisite_gaps: list[str] = field(default_factory=list)
    trigger_miss: bool = False
    explain_back_ok: bool | None = None
    explain_back_feedback: str | None = None
    disputed_key: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


def extract_json(text: str) -> dict[str, Any]:
    """Pull the JSON block out of a pasted chat reply.

    Tolerant about *surroundings* (fences, prose before it) and strict about
    *content*. Chat clients add wrappers; that is not the user's fault. A
    malformed schema is a different problem and is not smoothed over.
    """
    match = _FENCE.search(text)
    candidate = match.group(1) if match else None

    if candidate is None:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise RecordError(
                "no JSON object found in the pasted reply -- copy the whole fenced block"
            )
        candidate = text[start : end + 1]

    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError as exc:
        raise RecordError(f"the returned block is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise RecordError("the returned JSON must be an object")
    return payload


def parse(
    text: str,
    expected_item_id: str,
    product_codes: frozenset[str] = frozenset(),
) -> Diagnosis:
    """Parse and validate a returned payload against the item it belongs to."""
    payload = extract_json(text)

    returned_id = str(payload.get("item_id", "")).strip()
    if returned_id != expected_item_id:
        # Hard reject. Recording a diagnosis against the wrong item silently
        # poisons the DAG and the tag's reliability history at once.
        raise RecordError(
            f"item_id mismatch: briefing was for {expected_item_id!r}, reply "
            f"returned {returned_id!r}. Nothing recorded."
        )

    code = str(payload.get("error_code", "")).strip()
    if not code:
        raise RecordError("payload is missing error_code")
    errors.validate_code(code, product_codes)

    one_fix = str(payload.get("one_fix", "")).strip()
    if not one_fix:
        raise RecordError("payload is missing one_fix")
    if _looks_like_a_list(one_fix):
        # One fix, not five -- singular by prompt contract (DESIGN.md §9).
        raise RecordError(
            "one_fix contains multiple fixes. The contract is singular; re-run the "
            "exchange rather than recording a list."
        )

    gaps = payload.get("prerequisite_gaps") or []
    if not isinstance(gaps, list):
        raise RecordError("prerequisite_gaps must be a list of tag slugs")

    # Not required: a payload from an older prompt version has no such key, and
    # rejecting it would make a prompt revision retroactively invalidate
    # replies the learner already has in a chat window.
    divergence = str(payload.get("divergence") or "").strip() or None
    explanation = str(payload.get("explanation") or "").strip() or None

    return Diagnosis(
        item_id=returned_id,
        error_code=code,
        one_fix=one_fix,
        divergence=divergence,
        explanation=explanation,
        prerequisite_gaps=[str(g) for g in gaps],
        trigger_miss=bool(payload.get("trigger_miss", False)),
        explain_back_ok=_tri_state(payload.get("explain_back_ok")),
        explain_back_feedback=payload.get("explain_back_feedback"),
        disputed_key=bool(payload.get("disputed_key", False)),
        raw=payload,
    )


def _tri_state(value: Any) -> bool | None:
    """None means "not judged yet", which is not the same as rejected."""
    if value is None:
        return None
    return bool(value)


def _looks_like_a_list(text: str) -> bool:
    numbered = len(re.findall(r"(?m)^\s*\d[.)]\s", text)) >= 2
    bulleted = len(re.findall(r"(?m)^\s*[-*]\s", text)) >= 2
    return numbered or bulleted
