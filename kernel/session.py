"""The drill loop as a headless service. WEB_UI.md §2.

`cmd_drill` used to hold this sequence -- pick a tag, pick an item, capture,
grade, gate, brief, persist -- interleaved with `input()` and `print()`. Any
second front end needs the identical sequence, and a copy of it would drift
silently, because both copies would keep looking like they work.

So the sequence lives here and both front ends are adapters over it. This
module does no I/O beyond the database: it never reads stdin, never prints,
never writes a file. A caller that needs the briefing on disk writes it.

**The pedagogy invariants are enforced here, not by caller control flow**,
which is the whole point of the extraction (WEB_UI.md §8 risk 2):

- The pre-answer view is `Served`, which has no `answer_key` field at all.
  The key cannot be handed over early by a template that asks for the wrong
  variable, because there is nothing to ask for (§4.1).
- Phases advance in one order. Grading before capture raises `PhaseError`
  rather than quietly recording an unusable attempt. An empty answer raises
  `BlankAnswer` for the same reason: it is a slip, not a response.
- The explain-back gate has no skip path, and nothing is persisted until it
  passes. This module deliberately offers no flag to bypass it, exactly as
  `pedagogy/explain_back` offers none.
- `min_hint_level` is the *highest rung actually served*, floored by whatever
  the learner reports. A front end that serves rungs one at a time gets a
  measurement instead of a self-report (§4.2).

Three deviations from the interface sketched in WEB_UI.md §2, each because
the sketch does not survive contact with both callers:

1. `start()` takes no learner or product -- they are constructor arguments, so
   that a web request builds one session object and a CLI invocation builds
   one, instead of threading the pair through every call.
2. `briefing()` returns a `Briefing`, not a bare string. The caller needs the
   `attempt_id` to tell the learner what to record, and the `prompt_version`
   to display (§4.5).
3. `record()` is keyed by `attempt_id`, not by token. In the CLI the exchange
   happens in a *later process*, so no in-memory token survives to name it.
   The attempt id is the durable handle, and it is what the briefing hands
   back for that reason.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import dataclass
from dataclasses import field as dataclasses_field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from kernel import allocator
from kernel.exchange import briefing as briefing_mod
from kernel.exchange import record as record_mod
from kernel.objectives import base as objective_base
from kernel.pedagogy import capture as capture_mod
from kernel.pedagogy import explain_back, grading, hints
from kernel.storage import db

RANK_LIMIT = 25


class Phase(str, Enum):
    """One order, enforced. Every transition is a method on `DrillSession`."""

    PRESENTED = "presented"
    CAPTURED = "captured"
    ANSWERED = "answered"
    EXPLAINED = "explained"
    BRIEFED = "briefed"


class PhaseError(RuntimeError):
    """Raised when a step is taken out of order."""


class BlankAnswer(ValueError):
    """Raised when an answer is submitted empty.

    A blank answer used to grade as wrong, which is defensible in the abstract
    -- "I don't know" is a real answer -- but it is not what an empty box
    means in practice. It means a slipped return key, and it costs the learner
    an item plus a poisoned data point: an attempt marked wrong at whatever
    hint level, and a `rationale` that no longer corresponds to anything
    submitted. Deliberately declining to answer is not a case worth
    representing at the cost of that one.

    Enforced here rather than by a `required` attribute in a form, because
    this module is where the invariants live (see this module's docstring):
    a front end that forgets the attribute would otherwise still record it.
    """


class UnknownDrill(LookupError):
    """Raised when a token names no live drill -- expired, or a stale tab."""


# ------------------------------------------------------------------ results


@dataclass(frozen=True)
class Satisfied:
    """The objective is met with margin. DESIGN.md principle 9: stop studying.

    A distinct type rather than an empty `Served`, so a front end cannot
    render a start button by forgetting to check a boolean.
    """

    message: str = "Objective satisfied with margin. Stop studying."


@dataclass(frozen=True)
class Starved:
    """Nothing servable. The corpus, not the learner, is the blocker."""

    reason: str
    tag_slug: str | None = None
    # Set when the emptiness is an artefact of a subject filter rather than of
    # the corpus, so a front end can offer to widen the scope instead of
    # implying there is nothing to study anywhere.
    section: str | None = None


@dataclass(frozen=True)
class Recommendation:
    """What to work on, and why -- with no item reserved yet.

    The three multiplicands are carried separately rather than pre-summarised
    because they fail differently and the difference is the whole diagnosis: a
    low `gradient` means the objective does not care, low `learnability` means
    it is the wrong difficulty, and low `availability` means the corpus cannot
    serve it. Collapsing them to `priority` alone would hide which.
    """

    tag_slug: str
    section: str | None
    routed_from: str | None
    priority: float
    gradient: float
    learnability: float
    availability: float
    predicted_p_correct: float
    target_band: tuple[float, float]
    reasons: list[str] = dataclasses_field(default_factory=list)


@dataclass(frozen=True)
class Served:
    """Everything the learner may see *before* answering.

    There is no `answer_key` field and there must never be one. This type is
    what a template renders in phase 1, so the key is absent from the page by
    construction rather than by remembering to leave it out (WEB_UI.md §4.1).
    """

    token: str
    item_id: str
    tag_slug: str
    routed_from: str | None
    stem: str
    choices: list[str] | None
    passage: str | None
    section_slug: str
    target_band: tuple[float, float]
    capture_fields: list[str]
    max_hint_level: int = hints.MAX_LEVEL
    # Parallel to `choices`: what selecting each one submits. `None` when the
    # item is not answerable by selection at all, which is every numeric item
    # and any multiple-choice item whose key matches neither a letter nor an
    # option. See `grading.choice_values` for why this is computed here and
    # not in a template -- and for why it is not a key leak.
    choice_values: list[str] | None = None
    # For the items `choice_values` leaves to a text box: which box. `None` is
    # a plain one; `grading.NUMERIC` is one that will not take letters. A
    # constant per shape, not per key -- see `grading.input_shape`.
    input_shape: str | None = None


@dataclass(frozen=True)
class Accepted:
    """A capture that passed validation and is now fixed for this attempt."""

    token: str
    fields: list[str]


@dataclass(frozen=True)
class Verdict:
    """The first moment the key is allowed out."""

    correct: bool
    answer_key: str
    answer_given: str
    min_hint_level: int
    competence: float
    # Parallel to `Served.choices`: which option was the key, which was chosen,
    # or both. "Key: C -- you answered B" is unreadable once the answer form is
    # gone and C and B are just letters, and the moment a learner most wants to
    # see the two options side by side is the moment they got it wrong.
    choice_marks: list[str | None] = dataclasses_field(default_factory=list)


@dataclass(frozen=True)
class DrillView:
    """A drill in progress, re-derived from server state.

    Exists so a browser refresh is harmless: the phase lives on the server, so
    reloading re-renders where you actually are rather than restarting or
    double-recording. `verdict` is `None` until the answer is in, which is the
    same §4.1 guarantee as `Served` -- the key cannot reach a template early
    because there is nothing holding it.
    """

    phase: Phase
    served: Served
    verdict: Verdict | None = None
    rungs_shown: list[hints.Rung] = dataclasses_field(default_factory=list)
    explanation: str | None = None
    # The blind rationale, handed back so the gate can show the learner what
    # they already wrote instead of asking for it twice. Carrying it on the
    # view rather than re-reading the capture keeps the template's only source
    # of item data the one type that is guaranteed key-free.
    rationale: str | None = None
    stuck: bool = False
    attempt_id: int | None = None
    diagnosis: record_mod.Diagnosis | None = None


@dataclass(frozen=True)
class Briefing:
    text: str
    attempt_id: int
    prompt_version: str
    item_id: str


# ------------------------------------------------------- in-progress state


@dataclass
class _Drill:
    """Server-side state for one item in flight. Never leaves this module."""

    token: str
    item: sqlite3.Row
    tag_slug: str
    routed_from: str | None
    section: dict[str, Any]
    choices: list[str] | None
    passage: str | None
    capture_fields: list[str]
    started_at: str
    target_band: tuple[float, float]
    phase: Phase = Phase.PRESENTED
    highest_rung_served: int = 0
    # Every rung handed over, in the order asked for. `highest_rung_served` is
    # the metric; this is what a front end redraws after a refresh, and the
    # two are not the same list -- a learner may ask for L3 and then L1.
    rungs_shown: list[int] = dataclasses_field(default_factory=list)
    capture: capture_mod.Capture | None = None
    answer_given: str | None = None
    correct: bool | None = None
    min_hint_level: int = 0
    explanation: str | None = None
    # The learner declared they had no method, rather than writing one. Kept
    # apart from `explanation` because the text is a canned string and a later
    # reader must not have to pattern-match on it to know what happened.
    stuck: bool = False
    attempt_id: int | None = None
    briefing_text: str | None = None
    diagnosis: record_mod.Diagnosis | None = None
    # Monotonic, not wall clock: expiry must not move when the system clock
    # does. `started_at` stays wall clock because it is written to the log.
    touched_at: float = dataclasses_field(default_factory=time.monotonic)


# How long an untouched drill stays in the store. Long enough that a genuine
# item -- read a passage, work it, write the explain-back -- never expires
# under someone, short enough that a browser tab left open overnight does not
# hold an item and its passage forever.
DRILL_TTL_SECONDS = 6 * 60 * 60


class DrillStore:
    """In-memory drill state, keyed by token. WEB_UI.md §9 question 1.

    In-memory for now: an abandoned drill is lost on restart, which costs an
    ungraded capture and nothing else. If abandonment turns out to be worth
    measuring -- and it is diagnostic -- this grows a table behind the same
    three methods.

    Abandonment is the normal case, not the exception: every drill closed by
    closing a tab stays here otherwise, holding an item row and whatever
    passage it hangs off. So entries expire. Sweeping on `put` means the
    cost is paid by whoever starts the next drill and never by a background
    thread this process does not otherwise need.
    """

    def __init__(self, ttl_seconds: float = DRILL_TTL_SECONDS) -> None:
        self._drills: dict[str, _Drill] = {}
        self.ttl_seconds = ttl_seconds

    def put(self, drill: _Drill) -> None:
        self.sweep()
        self._drills[drill.token] = drill

    def get(self, token: str) -> _Drill:
        drill = self._drills.get(token)
        if drill is None or self._expired(drill):
            self._drills.pop(token, None)
            raise UnknownDrill(
                f"no drill in progress for token {token!r} -- it expired, or the "
                "server restarted. Nothing was recorded."
            )
        drill.touched_at = time.monotonic()
        return drill

    def drop(self, token: str) -> None:
        self._drills.pop(token, None)

    def sweep(self) -> int:
        """Discard expired drills. Returns how many went."""
        stale = [t for t, d in self._drills.items() if self._expired(d)]
        for token in stale:
            del self._drills[token]
        return len(stale)

    def _expired(self, drill: _Drill) -> bool:
        return (time.monotonic() - drill.touched_at) > self.ttl_seconds

    def __len__(self) -> int:
        return len(self._drills)


DEFAULT_STORE = DrillStore()


# ------------------------------------------------------------- the service


class DrillSession:
    """One learner, one product, the drill loop. Front ends are adapters."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        product: dict[str, Any],
        learner: str,
        store: DrillStore | None = None,
    ) -> None:
        self.conn = conn
        self.product = product
        self.learner = learner
        self.store = store if store is not None else DEFAULT_STORE

    # -- phase 0 -----------------------------------------------------------

    def position(self) -> objective_base.ObjectiveReport:
        """Where the learner stands against the objective, per route.

        Read-only and side-effect free -- unlike `study report`, this does not
        snapshot to `objective_state`. A page that renders on every visit must
        not write a position sample every time it is refreshed, or the
        trajectory chart ends up measuring how often someone opened a tab.
        """
        state = db.load_state(self.conn, self.learner, self.product["product_id"], self.product)
        return objective_base.build(self.product["objective"]).report(state)

    def _ranked(self, section: str | None):
        """(state, objective, servable allocations). Shared by the callers below."""
        state = db.load_state(self.conn, self.learner, self.product["product_id"], self.product)
        objective = objective_base.build(self.product["objective"])
        edges = db.load_edges(self.conn, self.product["product_id"])
        ranked = [
            a
            for a in allocator.rank(state, objective, edges, limit=RANK_LIMIT)
            if a.priority > 0
        ]
        if section is not None:
            known = set(self.product.get("sections") or {})
            if section not in known:
                raise ValueError(f"unknown section {section!r}; product has {sorted(known)}")
        return state, objective, ranked

    @staticmethod
    def _in_section(alloc, state, section: str | None) -> bool:
        tag = state.tags.get(alloc.tag_slug)
        return section is None or (tag is not None and tag.section == section)

    def servable_by_section(self) -> dict[str, int]:
        """How many tags each section can currently serve.

        So a subject picker can show what is actually behind each option
        instead of offering a dead click. A section absent from the result has
        nothing servable right now.
        """
        state, _, ranked = self._ranked(None)
        counts: dict[str, int] = {}
        for alloc in ranked:
            tag = state.tags.get(alloc.tag_slug)
            if tag is not None:
                counts[tag.section] = counts.get(tag.section, 0) + 1
        return counts

    def recommend(
        self, tag: str | None = None, section: str | None = None
    ) -> Recommendation | Satisfied | Starved:
        """Decide what to work on, without reserving an item for it.

        Split out of `start()` because a home page has to ask *what should I
        study* without also answering *give me an item now* -- the second is a
        commitment, and asking it on every page load would churn item
        selection and mint tokens nobody uses. `start()` is this plus the
        commitment.

        `section` is the domain-focus drill mode (DESIGN.md §13.4), and it is
        a narrower thing than letting the learner pick the item. Choosing to
        spend the next half hour on one subject is a scheduling decision only
        the learner can make; choosing *which tag within it* is the
        allocator's whole job, and it still does that. WEB_UI.md §9 Q3 argues
        against offering a menu of tags for exactly that reason, and this is
        not one.

        A satisfied objective ignores the filter entirely: focusing on a
        subject must never be a way to keep the tool from saying stop.
        """
        state, objective, ranked = self._ranked(section)
        if objective.satisfied(state):
            return Satisfied()

        in_scope = [a for a in ranked if self._in_section(a, state, section)]
        if tag:
            in_scope = [a for a in in_scope if a.tag_slug == tag]

        if not in_scope:
            if section and ranked:
                elsewhere = sorted(
                    {
                        state.tags[a.tag_slug].section
                        for a in ranked
                        if a.tag_slug in state.tags
                    }
                )
                return Starved(
                    f"Nothing servable in {section}. Other subjects still have work: "
                    + ", ".join(elsewhere),
                    tag_slug=tag,
                    section=section,
                )
            return Starved(
                "Nothing servable. The report shows what the corpus is missing.",
                tag_slug=tag,
                section=section,
            )

        alloc = in_scope[0]
        return Recommendation(
            section=state.tags[alloc.tag_slug].section
            if alloc.tag_slug in state.tags
            else None,
            tag_slug=alloc.tag_slug,
            routed_from=alloc.routed_from,
            priority=alloc.priority,
            gradient=alloc.gradient,
            learnability=alloc.learnability,
            availability=alloc.availability,
            predicted_p_correct=alloc.predicted_p_correct,
            target_band=alloc.target_band,
            reasons=list(alloc.reasons),
        )

    def start(
        self, tag: str | None = None, section: str | None = None
    ) -> Served | Satisfied | Starved:
        """Pick what to work on and serve it, key withheld."""
        choice = self.recommend(tag, section)
        if not isinstance(choice, Recommendation):
            return choice

        alloc = choice
        lo, hi = alloc.target_band
        item = db.pick_item(self.conn, self.product["product_id"], alloc.tag_slug, lo, hi)
        if item is None:
            return Starved(
                f"no item for {alloc.tag_slug} in band {lo:.0f}-{hi:.0f}",
                tag_slug=alloc.tag_slug,
            )

        drill = _Drill(
            token=secrets.token_urlsafe(16),
            item=item,
            tag_slug=alloc.tag_slug,
            routed_from=alloc.routed_from,
            section=self.product["sections"][item["section_slug"]],
            choices=json.loads(item["choices_json"]) if item["choices_json"] else None,
            passage=db.load_passage(self.conn, item["passage_id"]),
            capture_fields=capture_mod.active_fields(self.product),
            started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            target_band=alloc.target_band,
        )
        self.store.put(drill)

        return self._serve(drill, alloc.target_band)

    def view(self, token: str) -> DrillView:
        """Where this drill is, so a refresh redraws instead of restarting."""
        drill = self.store.get(token)
        verdict = None
        if drill.phase in (Phase.ANSWERED, Phase.EXPLAINED, Phase.BRIEFED):
            verdict = self._verdict(drill)
        return DrillView(
            phase=drill.phase,
            served=self._serve(drill, drill.target_band),
            verdict=verdict,
            rungs_shown=[hints.rung(level) for level in drill.rungs_shown],
            explanation=drill.explanation,
            rationale=drill.capture.rationale if drill.capture else None,
            stuck=drill.stuck,
            attempt_id=drill.attempt_id,
            diagnosis=drill.diagnosis,
        )

    @staticmethod
    def _serve(drill: _Drill, target_band: tuple[float, float]) -> Served:
        return Served(
            token=drill.token,
            item_id=drill.item["item_id"],
            tag_slug=drill.tag_slug,
            routed_from=drill.routed_from,
            stem=drill.item["stem"],
            choices=drill.choices,
            passage=drill.passage,
            section_slug=drill.item["section_slug"],
            target_band=target_band,
            capture_fields=drill.capture_fields,
            choice_values=grading.choice_values(
                drill.choices, drill.item["answer_key"]
            ),
            input_shape=grading.input_shape(drill.item["answer_key"]),
        )

    # -- phase 1 -----------------------------------------------------------

    def request_hint(self, token: str, level: int) -> hints.Rung:
        """Serve one rung and remember that it was served.

        Only before the verdict: once the key is out, a rung measures nothing.
        The ladder does not unroll -- one call, one rung (pedagogy/hints).
        """
        drill = self.store.get(token)
        self._require(drill, Phase.PRESENTED, Phase.CAPTURED)
        rung = hints.rung(level)  # validates the range
        drill.highest_rung_served = max(drill.highest_rung_served, level)
        if level not in drill.rungs_shown:
            drill.rungs_shown.append(level)
        return rung

    def submit_capture(self, token: str, capture: capture_mod.Capture) -> Accepted:
        """Fix the blind capture. Raises `CaptureError`; the key stays withheld."""
        drill = self.store.get(token)
        self._require(drill, Phase.PRESENTED)
        capture_mod.validate(capture, drill.capture_fields)
        drill.capture = capture
        drill.phase = Phase.CAPTURED
        return Accepted(token=token, fields=list(drill.capture_fields))

    # -- phase 2 -----------------------------------------------------------

    def submit_answer(
        self, token: str, answer: str, reported_hint_level: int | None = None
    ) -> Verdict:
        """Grade deterministically, server-side. The model never grades.

        `reported_hint_level` is the CLI's after-the-fact self-report. It acts
        as a floor, never a ceiling: a front end that served rungs one at a
        time already knows better, and taking the max means a self-report can
        only ever be more honest than the observation, not less. That is not
        DRM (§4.3) -- it declines to *un-observe* something already served.
        """
        drill = self.store.get(token)
        self._require(drill, Phase.CAPTURED)

        if not (answer or "").strip():
            raise BlankAnswer("no answer given -- nothing to grade")

        reported = reported_hint_level or 0
        if not 0 <= reported <= hints.MAX_LEVEL:
            raise ValueError(f"hint level must be 0-{hints.MAX_LEVEL}, got {reported}")

        drill.answer_given = answer
        drill.correct = grading.grade(answer, drill.item["answer_key"])
        drill.min_hint_level = max(drill.highest_rung_served, reported)
        drill.phase = Phase.ANSWERED

        return self._verdict(drill)

    def _verdict(self, drill: _Drill) -> Verdict:
        """The verdict for an already-graded drill.

        One builder for both callers -- `submit_answer` returns it once and
        `view` rebuilds it on every refresh. Two copies drifted apart is how a
        redrawn page ends up disagreeing with the page it redrew.
        """
        key = drill.item["answer_key"]
        return Verdict(
            correct=bool(drill.correct),
            answer_key=key,
            answer_given=drill.answer_given or "",
            min_hint_level=drill.min_hint_level,
            competence=hints.competence_score(
                drill.min_hint_level, bool(drill.correct)
            ),
            choice_marks=grading.mark_choices(
                grading.choice_values(drill.choices, key), key, drill.answer_given
            ),
        )

    def submit_explain_back(
        self, token: str, text: str, stuck: bool = False
    ) -> explain_back.GateResult:
        """The gate. Nothing is persisted until it passes, and it has no skip.

        A failed gate leaves the drill in `ANSWERED`, so the caller loops. The
        only way past is a well-formed explanation, which is the mechanism
        rather than an obstacle in front of it (DESIGN.md principle 10).

        `stuck` is the learner declaring they do not know where to start. It is
        not the skip this method refuses to grow: the attempt is recorded, the
        declaration is stored where the path would go, and the briefing asks
        the reader to teach rather than to diagnose. See `pedagogy/explain_back`
        for why that is a stronger signal than a path the learner invented to
        satisfy a word count.
        """
        drill = self.store.get(token)
        self._require(drill, Phase.ANSWERED)

        gate = explain_back.check(text, bool(drill.correct), stuck)
        if not gate.passed:
            return gate

        drill.stuck = stuck
        drill.explanation = (
            explain_back.STUCK_DECLARATION if stuck else text.strip()
        )
        cap_row = drill.capture.as_row()  # type: ignore[union-attr]
        cap_row["answer_given"] = drill.answer_given
        cap_row["stuck"] = stuck
        drill.attempt_id = db.record_attempt(
            self.conn,
            self.learner,
            self.product["product_id"],
            drill.item,
            drill.tag_slug,
            cap_row,
            bool(drill.correct),
            drill.min_hint_level,
            drill.started_at,
        )
        drill.phase = Phase.EXPLAINED
        return gate

    # -- phase 3 -----------------------------------------------------------

    def briefing(self, token: str) -> Briefing:
        """Render the outbound half of the exchange and log it."""
        drill = self.store.get(token)
        self._require(drill, Phase.EXPLAINED, Phase.BRIEFED)
        if drill.briefing_text is not None:
            return Briefing(
                text=drill.briefing_text,
                attempt_id=int(drill.attempt_id),  # type: ignore[arg-type]
                prompt_version=briefing_mod.PROMPT_VERSION,
                item_id=drill.item["item_id"],
            )

        item = drill.item
        capture = drill.capture
        b_item = briefing_mod.BriefingItem(
            item_id=item["item_id"],
            stem=item["stem"],
            choices=drill.choices,
            answer_key=item["answer_key"],
            official_expl=item["official_expl"],
            passage=drill.passage,
            section_slug=item["section_slug"],
            explanation_policy=drill.section.get("explanation_policy", "withheld"),
            tags=[drill.tag_slug],
        )
        b_cap = briefing_mod.BriefingCapture(
            confidence=capture.confidence,  # type: ignore[union-attr]
            rationale=capture.rationale,  # type: ignore[union-attr]
            verification_method=capture.verification_method,  # type: ignore[union-attr]
            answer_given=drill.answer_given,
            correct=bool(drill.correct),
            min_hint_level=drill.min_hint_level,
            stuck=drill.stuck,
        )
        text = briefing_mod.render(b_item, b_cap)
        text += f"\n\n--- LEARNER'S EXPLAIN-BACK ---\n{drill.explanation}\n"

        db.record_exchange(
            self.conn, int(drill.attempt_id), briefing_mod.PROMPT_VERSION, text, None  # type: ignore[arg-type]
        )
        drill.briefing_text = text
        drill.phase = Phase.BRIEFED

        return Briefing(
            text=text,
            attempt_id=int(drill.attempt_id),  # type: ignore[arg-type]
            prompt_version=briefing_mod.PROMPT_VERSION,
            item_id=item["item_id"],
        )

    def record_for(self, token: str, pasted_text: str) -> record_mod.Diagnosis:
        """`record()` for a drill still in hand, keyed by token.

        The token-holding caller should use this rather than `record()`: the
        attempt id comes from the drill, so a stale form cannot aim a
        diagnosis at some other attempt by supplying its own. It also refuses
        to record twice, which a browser refresh would otherwise do.
        """
        drill = self.store.get(token)
        self._require(drill, Phase.EXPLAINED, Phase.BRIEFED)
        if drill.diagnosis is not None:
            raise record_mod.RecordError(
                f"attempt {drill.attempt_id} already has a diagnosis recorded"
            )
        diagnosis = self.record(int(drill.attempt_id), pasted_text)  # type: ignore[arg-type]
        drill.diagnosis = diagnosis
        return diagnosis

    def finish(self, token: str) -> None:
        """Release in-flight state. The attempt is already durable.

        Not called after recording: a browser that refreshes the page it is
        already on should see the drill it just completed, not a message
        claiming nothing was written. Expiry reclaims it instead.
        """
        self.store.drop(token)

    # -- the inbound half, later and possibly in another process ----------

    def record(self, attempt_id: int, pasted_text: str) -> record_mod.Diagnosis:
        """Parse a returned payload and log it against `attempt_id`.

        Rejects on `item_id` mismatch inside `exchange/record` -- the check
        that makes the copy-paste protocol safe without an API. The item id
        comes from the attempt row, never from the caller, so a stale tab
        cannot land a diagnosis on the wrong item by supplying both halves.
        """
        row = self.conn.execute(
            "SELECT item_id FROM attempts WHERE attempt_id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise record_mod.RecordError(f"no attempt {attempt_id}")

        product_codes = frozenset(self.product.get("error_code_weights", {}) or {})
        diagnosis = record_mod.parse(pasted_text, row["item_id"], product_codes)

        db.record_diagnosis(self.conn, attempt_id, diagnosis)
        self.conn.execute(
            "UPDATE exchanges SET payload_json = ? WHERE attempt_id = ?",
            (json.dumps(diagnosis.raw), attempt_id),
        )
        self.conn.commit()
        return diagnosis

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _require(drill: _Drill, *allowed: Phase) -> None:
        if drill.phase not in allowed:
            expected = " or ".join(p.value for p in allowed)
            raise PhaseError(
                f"drill is at phase {drill.phase.value!r}, this step needs {expected}"
            )


# ------------------------------------------------------------------ helpers


def build_capture(values: dict[str, Any], fields: list[str]) -> capture_mod.Capture:
    """Coerce raw front-end strings into a `Capture`.

    Both callers receive text -- a terminal prompt and a form post -- and both
    need `confidence` as an int or `None`. Coercing it in one place stops the
    two front ends disagreeing about what `"3 "` or `"three"` means.
    """
    cap = capture_mod.Capture()
    for field_name in fields:
        raw = values.get(field_name)
        if field_name == "confidence":
            text = str(raw).strip() if raw is not None else ""
            cap.confidence = int(text) if text.isdigit() else None
        else:
            setattr(cap, field_name, str(raw).strip() if raw is not None else None)
    return cap
