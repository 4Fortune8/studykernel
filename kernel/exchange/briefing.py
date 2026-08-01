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

# Bumped when the return contract changes, not when wording is tuned. v0.2
# added `divergence`; a reply parsed under v0.1 has none, and this is what
# lets a later reader tell "the model declined to locate it" from "the prompt
# never asked".
# v0.3 added `explanation`: the worked solution itself, which the protocol was
# asking a reader to produce in prose and then throwing away with the prose.
PROMPT_VERSION = "v0.3-stub"

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
    # The learner declared they had no method rather than writing one. Passed
    # through because it changes the reader's job from correcting a path to
    # teaching one, and because a reader told this will not invent a
    # divergence out of a rationale that was a guess.
    stuck: bool = False


# What the reader must say when the learner's own work is too thin to locate a
# divergence in. Stored and displayed verbatim, so the learner is told what to
# do differently rather than being handed an empty field: the fix for "no steps
# shown" is showing steps, and this is the only place the system can say so at
# the moment it actually matters.
NO_WORK_SHOWN = (
    "You didn't show enough of your steps to find where this went wrong. "
    "Write out what you actually did next time -- with the steps, this can "
    "point at the exact line that broke."
)

RETURN_SCHEMA: dict[str, Any] = {
    "item_id": "<echo the item_id exactly>",
    "error_code": "<one code from the list above>",
    "divergence": (
        "<quote the first step in the learner's OWN work that is wrong, then "
        "what it should have been -- or the exact NO WORK SHOWN sentence>"
    ),
    "prerequisite_gaps": ["<tag slug>", "..."],
    "one_fix": "<a single actionable correction -- exactly one>",
    "explanation": (
        "<what was flawed in their thinking, then the bridge from what they did "
        "to what they should have done, ending at the key -- complete enough "
        "that no follow-up question is needed>"
    ),
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

    if capture.stuck:
        parts.append(
            "\nThe learner stated they did not know where to start. There is no "
            "solution path of theirs to correct. Teach this item from the "
            "beginning -- first move first, and why that move -- and set "
            '"divergence" to the NO WORK SHOWN sentence below.'
        )

    parts.append("\n--- YOUR TASK ---")
    parts.append(
        "1. Find the DIVERGENCE: the first step in the learner's own work -- their "
        "rationale above, and their explain-back below -- that is actually wrong. "
        "Quote their words, then give the corrected step. Point at the line that "
        "broke, not at the topic. One or two sentences: this locates the break, "
        "item 4 walks the route out of it, and item 3 says what habit to change "
        "-- write each of the three once, in its own field.\n"
        "   If they showed no steps to locate it in -- a bare answer, a restatement, "
        "or a declaration that they did not know where to start -- do not guess at "
        "reasoning they did not write, and do not use a quote to stand in for one. "
        'Return this sentence verbatim as "divergence":\n'
        f"   {NO_WORK_SHOWN}\n"
        "2. Classify the error using exactly one code:\n"
        f"{product_error_codes or errors.describe()}\n"
        "3. Give ONE fix. Not five. The single highest-leverage correction.\n"
        "4. Write the EXPLANATION, addressed to the learner and built on the "
        "reasoning they actually gave -- not a solution written as though they "
        "had said nothing. Two things, in one pass:\n"
        "   (a) What is flawed in their thinking. Name the belief or the move "
        "that is wrong, not the topic it belongs to. If they were right for the "
        "wrong reason, that is the flaw and it is worth more than the mark.\n"
        "   (b) The bridge from what they did to what they should have done: the "
        "steps that carry their starting point to the keyed answer, in order, "
        "each one named. Where 3 above says what habit to change, this says how "
        "the item is actually done.\n"
        "   Write it so that nothing is left for a follow-up question. If the "
        "reader would have to ask 'but why?' at any step, that step is not "
        "finished. Do not stop at the correct answer -- stop when the route to "
        "it is walkable.\n"
        "   Where the learner gave no usable reasoning to bridge from, teach the "
        "item from the first move instead, and say why that move is first.\n"
        "   This is bounded by the EXPLANATION POLICY at the top -- it is the "
        "reasoning that policy permits, said to the learner, not a licence to go "
        "past it. Under pinned or pinned_strict, unpack the official text and do "
        "not derive your own. Under anchored, every step is a verbatim quote or "
        "it does not appear.\n"
        "5. If the learner's explain-back is included below, say whether it is sound."
    )
    parts.append(
        "\nReturn ONLY this JSON block, fenced, with no prose after it:\n"
        "```json\n" + json.dumps(RETURN_SCHEMA, indent=2) + "\n```"
    )

    return "\n".join(parts)
