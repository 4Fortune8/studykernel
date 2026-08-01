"""Briefing generation -- the outbound half of the exchange protocol.

DESIGN.md §10: a single briefing handoff rendered to the clipboard, tutoring
happens in the user's chat client of choice, and one structured JSON block
returns. No API is required for the system to work.

The explanation policy is the part that matters most here and is per-section
product config, not a global. Model reasoning is dependable on quantitative
material and must be pinned to official text on reading comprehension, where
fluent-but-false justifications teach false reasoning. DATA_SOURCING_ELAR.md
§2.4 adds `anchored` for the common case of RC items with no official
explanation: justify only via verbatim quotes, and say so when the key cannot
be supported.

Prompt wording is deliberately stubbed (DESIGN.md §17 Q5) -- the *contracts*
are fixed, the wording is tuned against real transcripts. `prompt_version`
rides along on every exchange so early data stays interpretable across
revisions.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from kernel.pedagogy import errors, hints

PROMPT_VERSION = "v0.1-stub"

POLICY_INSTRUCTIONS: dict[str, str] = {
    "withheld": (
        "You may reason freely from the item and the answer key to explain the "
        "solution."
    ),
    "pinned": (
        "An official explanation is provided. Defer to it. Unpack and clarify it; "
        "do not derive an alternative justification."
    ),
    "pinned_strict": (
        "An official explanation is provided. Defer to it completely. Independent "
        "justification is forbidden -- if the official text does not support a "
        "point, do not make it."
    ),
    "anchored": (
        "No official explanation exists. You may justify the keyed answer ONLY by "
        "quoting the passage verbatim. For each rejected choice, quote the specific "
        "text that rules it out, or state that the passage offers no support. If you "
        "cannot find textual support for the keyed answer, say so plainly and set "
        '"disputed_key": true.'
    ),
}


@dataclass
class BriefingItem:
    item_id: str
    stem: str
    choices: list[str] | None
    answer_key: str | None
    official_expl: str | None
    passage: str | None
    section_slug: str
    explanation_policy: str
    tags: list[str]


@dataclass
class BriefingCapture:
    confidence: int | None
    rationale: str | None
    verification_method: str | None
    answer_given: str | None
    correct: bool
    min_hint_level: int
    time_total_ms: int | None = None
    time_median_ms: int | None = None


RETURN_SCHEMA: dict[str, Any] = {
    "item_id": "<echo the item_id exactly>",
    "error_code": "<one code from the list above>",
    "prerequisite_gaps": ["<tag slug>", "..."],
    "one_fix": "<a single actionable correction -- exactly one>",
    "trigger_miss": False,
    "explain_back_ok": None,
    "explain_back_feedback": "<what the learner's own explanation got wrong, if anything>",
    "disputed_key": False,
}


def render(
    item: BriefingItem,
    capture: BriefingCapture,
    product_error_codes: str | None = None,
) -> str:
    """Render the full briefing text for the clipboard."""
    policy = POLICY_INSTRUCTIONS.get(item.explanation_policy)
    if policy is None:
        raise ValueError(f"unknown explanation policy {item.explanation_policy!r}")

    parts: list[str] = []
    parts.append(
        "You are tutoring one item. Grading has already happened and is not your "
        "job -- do not re-grade, and do not dispute correctness.\n"
    )
    parts.append(f"EXPLANATION POLICY ({item.explanation_policy}): {policy}\n")

    if item.passage:
        parts.append(f"--- PASSAGE ---\n{item.passage}\n")

    parts.append(f"--- ITEM {item.item_id} ---")
    parts.append(item.stem)
    if item.choices:
        for letter, choice in zip("ABCDEFGH", item.choices, strict=False):
            parts.append(f"  {letter}. {choice}")
    parts.append(f"\nKEY: {item.answer_key}")
    if item.official_expl and item.explanation_policy in ("pinned", "pinned_strict"):
        parts.append(f"\nOFFICIAL EXPLANATION:\n{item.official_expl}")
    parts.append(f"TAGS: {', '.join(item.tags) or '(untagged)'}")

    parts.append("\n--- WHAT THE LEARNER DID (written before seeing the key) ---")
    parts.append(f"Confidence: {capture.confidence}")
    parts.append(f"Rationale: {capture.rationale}")
    if capture.verification_method:
        parts.append(f"Verification method: {capture.verification_method}")
    parts.append(f"Answer given: {capture.answer_given}  ->  " f"{'CORRECT' if capture.correct else 'WRONG'}")
    parts.append(
        f"Lowest hint level needed: L{capture.min_hint_level} "
        f"({hints.rung(capture.min_hint_level).name})"
    )
    if capture.time_total_ms and capture.time_median_ms:
        ratio = capture.time_total_ms / capture.time_median_ms
        parts.append(
            f"Time: {capture.time_total_ms / 1000:.0f}s vs own median "
            f"{capture.time_median_ms / 1000:.0f}s ({ratio:.1f}x)"
        )

    parts.append("\n--- YOUR TASK ---")
    parts.append(
        "1. Compare the learner's rationale to the actual solution and name where "
        "they diverge.\n"
        "2. Classify the error using exactly one code:\n"
        f"{product_error_codes or errors.describe()}\n"
        "3. Give ONE fix. Not five. The single highest-leverage correction.\n"
        "4. If the learner's explain-back is included below, say whether it is sound."
    )
    parts.append(
        "\nReturn ONLY this JSON block, fenced, with no prose after it:\n"
        "```json\n" + json.dumps(RETURN_SCHEMA, indent=2) + "\n```"
    )

    return "\n".join(parts)
