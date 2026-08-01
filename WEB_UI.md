# WEB_UI.md — Local study interface

**Status:** Draft v0.1 — plan, pre-implementation
**Scope:** A local single-user web front end over the existing kernel.
**Companion to:** DESIGN.md §16 (v2: "Local web UI"), DATA_SOURCING_ELAR.md §4 (essay loop).

Decisions taken: **local single-user** (localhost, no auth, no multi-tenancy),
**copy/paste exchange retained** with better ergonomics, **FastAPI + HTMX +
KaTeX**, server-rendered, no JS build step.

---

## 1. Why a web UI at all

The CLI already runs the loop. A second front end has to justify itself or it
is exactly the "sixty hours of building and ten of studying" failure DESIGN.md
keeps naming. Four things the terminal genuinely cannot do:

1. **Render mathematics.** DATA_SOURCING_MATH.md §5.2 stores LaTeX raw and
   calls the CLI's raw display "a known cosmetic cost… a future web UI renders
   it (KaTeX)". ~3,000 imported MATH items are LaTeX-dense and near-unreadable
   as source. This alone pays for the project.
2. **Passage-anchored reading.** RACE items are a passage plus a question set.
   The terminal prints a wall of text; a browser puts passage and item side by
   side and keeps the passage pinned across the set.
3. **The essay loop.** A 30-minute timed draft with a blind self-assessment
   gate is not something `input()` can host at all. It is currently
   unimplementable, not merely unpleasant.
4. **Position as a trajectory.** `objective_state` already snapshots per-route
   P(success) over time. That is a line chart, and it is invisible in a table.

Everything else the UI does is the CLI with fewer keystrokes, which is worth
something (principle 10) but is not the reason to build it.

**Anti-goal, stated first because it is the one that will drift:** this is not
an engagement product. See §6.

---

## 2. The refactor this requires first

`cmd_drill` in `kernel/cli.py` is **126 lines of orchestration** — pick a tag,
pick an item, capture, grade, gate, render briefing, persist. The web UI needs
the identical sequence. Copying it guarantees the two front ends drift, and
the drift will be silent because both will look like they work.

**Extract `kernel/session.py`: a headless drill session service.**

```python
class DrillSession:
    start(learner, product) -> Served | Satisfied | Starved
    submit_capture(token, capture) -> Accepted | CaptureError
    submit_answer(token, answer) -> Verdict          # server grades
    request_hint(token, level) -> Rung               # records level reached
    submit_explain_back(token, text) -> GateResult
    briefing(token) -> str
    record(token, pasted_text) -> Diagnosis
```

Both `cli.py` and `web/` become thin adapters over it. This is the same test
products get: if the web layer needs kernel changes beyond calling this
service, the seam was drawn wrong.

The existing 76 tests must pass unchanged through the refactor — they are the
proof it was behavior-preserving.

---

## 3. Architecture

```
web/
├── app.py              FastAPI, single learner, no sessions/auth
├── deps.py             db connection, product pack, objective (per request)
├── routes/
│   ├── now.py          home: what to work on, or STOP
│   ├── drill.py        the three-phase loop
│   ├── report.py       position, routes, reliability, backlog
│   ├── corpus.py       browse, provenance, review queue
│   └── essay.py        the essay loop
├── templates/          Jinja2; HTMX partials under partials/
└── static/
    ├── katex/          bundled, not CDN — must work offline
    └── app.css         one file, no framework
```

`kernel/` is untouched apart from §2's extraction. The web layer **must not**
reimplement allocation, grading, or state maths in JavaScript. The browser
renders and posts; every decision is server-side.

Add `web` as an optional dependency group (`fastapi`, `uvicorn`, `jinja2`)
so the study loop keeps working with no web install.

---

## 4. The drill flow, and the invariants it must re-earn

Every pedagogy invariant is currently enforced by the CLI's control flow.
A browser makes each one easy to break, so each has to be re-earned
deliberately. **This section is the most important one in the file.**

### 4.1 Blind capture — the key is never sent

DESIGN.md §9: confidence and rationale are written **blind**, before the
answer is revealed.

> The answer key must not be in the page. Not hidden with CSS, not disabled,
> not `display:none` — **not sent to the browser at all.**

Phase 1 renders stem, choices and passage only; the key is excluded from the
template context. It arrives in the phase-2 response, after capture is
persisted. Anything less and the log silently becomes worthless, and the
learner cannot tell because they will not remember whether they peeked.

A view-source test belongs in the suite: fetch phase 1, assert the key string
is absent from the body.

### 4.2 Hint ladder — observed, not self-reported

The CLI asks "lowest hint level you needed" *after the fact*. The browser can
do better: each rung is a separate request, and the server records the highest
rung actually served. `min_hint_level` stops being a self-report and becomes a
measurement.

This is a genuine data-quality upgrade over the CLI, and the headline
competence metric (DESIGN.md §9) is the thing it upgrades. Rungs are fetched
on request, one at a time — never all six shipped and revealed client-side.

### 4.3 The explain-back gate — no skip, no exceptions

DESIGN.md principle 10 names this the sole justified friction, because here
the friction *is* the mechanism. Therefore:

- no skip button, no "remind me later", no keyboard escape
- navigating away leaves the attempt `resolved = 0`, and the home page says so
- the gate is server-side; a client that never renders it changes nothing

The one thing the UI must *not* do is add DRM. DESIGN.md §10's honesty
tradeoff stands: a determined user can self-report L1 after reading the
explanation, and that is accepted, because the user is subject and sole
beneficiary and corrupting the log corrupts only their own diagnostics. Not
handing over the key early (§4.1) is a different thing from policing what the
learner does with it.

### 4.4 Grading is server-side, always

`kernel.pedagogy.grading` runs on the server. The browser cannot grade because
it does not have the key until after grading has happened. This falls out of
§4.1 for free, which is a sign §4.1 is the right constraint.

### 4.5 The exchange, with the ergonomics fixed

Protocol unchanged (DESIGN.md §10): briefing out, one structured JSON block
back, rejected on `item_id` mismatch. What improves is everything around it:

- briefing rendered in a panel with a one-click copy
- paste box validates **inline and immediately** — `RecordError` surfaces next
  to the box instead of after a Ctrl-D
- the `item_id` mismatch message names both ids, so the cause (a stale tab) is
  obvious rather than mysterious
- `prompt_version` is displayed, so a transcript can be placed later

---

## 5. Screens

### 5.1 `Now` — the home page

Answers exactly one question: *what is the single best thing to work on right
now?* Top allocation, the reason (gradient / learnability / availability), and
a start button. Position readout underneath.

Three states, and the third is the one that matters:

| State | Page |
|---|---|
| servable | the recommendation + start |
| starved | "the corpus cannot serve what you need" + the acquisition backlog |
| **satisfied** | **STOP. No start button. See §6.** |

### 5.2 `Drill` — three phases, one page

HTMX swaps a single panel; no client-side routing, no state machine in JS.

1. **Present + capture.** Passage (if any) pinned left, item right. KaTeX on
   stem and choices. Capture fields come from `capture.active_fields(product)`
   — `verification_method` appears for products that enable it and is absent
   for those that do not, with no UI branch of its own.
2. **Answer + verdict.** Answer, then server grades, then verdict. Hint rungs
   requestable throughout phase 1–2. Then the explain-back gate.
3. **Exchange.** Briefing panel, copy, paste, inline validation, one fix shown
   on success.

### 5.3 `Report`

The CLI report, plus the two things a terminal cannot show:

- **position trajectory** — per-route P(success) over time from
  `objective_state`, which is already being written on every `report` run
- **reliability table** sorted by lower bound, with CI width drawn, so "wide
  interval" reads as a shape rather than a number
- variance trend per domain (DESIGN.md §13.2 wants this surfaced)
- the content-acquisition backlog, ordered by the objective

### 5.4 `Corpus` — provenance and the review queue

The schema carries `source`, `license`, `redistributable`, `label_source`,
`reviewed` and `flags_json` on every item, and nothing currently reads them
back. This screen is where that becomes useful:

- browse and search items; provenance visible per item
- **review queue** filtered on `flags_json`: `disputed_key` (answer-key errors
  from MMLU and RACE), `coarse_tag` (the ~2,160 items awaiting the batch
  labeling pass), `nonstd_answer` (552 held-out MATH items)
- promoting an item to the gold set (`gold_labels`) is one click here

This screen is the natural host for the batch-labeling and gold-set work that
DESIGN.md §16 v1 schedules, so building it early is not scope creep — it is
the tool that makes v1's labeling milestone tractable.

### 5.5 Anchored-policy quote verification — the one novel idea here

DATA_SOURCING_ELAR.md §2.4 defines `anchored`: with no official explanation,
the model may justify the keyed answer **only by quoting the passage
verbatim**, and must emit `disputed_key` if it cannot find support. The stated
value is that "the user has the passage in front of them; every anchor is
checkable in seconds."

A browser can check them **mechanically**. Extend the return payload with

```json
"evidence": [{"choice": "B", "quote": "..."}]
```

and then, for each quote, string-search the passage:

- **found** → highlight the span in the passage, click-to-scroll from the claim
- **not found** → flag it in red as an unverifiable anchor

That converts the anchored policy from an instruction the model is asked to
follow into a constraint that is *checked*. A fabricated quote becomes visible
immediately rather than plausible forever. It is cheap — one substring search
per claim — and it is the single highest-value thing this UI can do that the
CLI cannot.

It does not eliminate fabrication: the model still knows the key and can
rationalize toward it with real quotes. It eliminates *invented* quotes, which
is the failure mode the policy was written against.

### 5.6 `Essay` — v1 of the loop

DATA_SOURCING_ELAR.md §4.2, which is unimplementable in the CLI:

```
prompt → timed draft (~30 min) → blind self-assessment → paste draft into
exchange → rubric-anchored critique → single-dimension revision → re-critique
```

- draft area with a visible timer; autosave to `attempts`
- **blind self-assessment gate**: score your own draft against the rubric
  *before* the critique is revealed — same blind-then-compare mechanic as
  explain-back, same encoding reason, so it gets the same no-skip treatment
- critique returns per-dimension scores plus **one** dimension to fix
- per-dimension trajectory chart — the essay's analog of the hint-level curve

Scheduling note the UI must respect: the essay is a **gate, not a polish
step** (§4.4). ELAR readiness needs CRC *and* essay, so the essay enters the
rotation early and the `Now` page must be willing to recommend it — not park
it in a corner tab.

---

## 6. What this UI must never grow

DESIGN.md principle 9: *a satisfied objective reads zero. The tool must be
able to say "stop studying" — a study tool that cannot say this is an
engagement product.*

The satisfied state is therefore a **full-page stop**, not a banner. The start
button is removed, not disabled.

Explicitly forbidden, because every one of them is a default in this genre and
each directly contradicts a stated principle:

| Forbidden | Why |
|---|---|
| streaks, daily goals, "don't break the chain" | optimizes attendance, not the objective |
| notifications / reminders to come back | the tool decides when you are done, not when you return |
| XP, levels, badges, leaderboards | a second objective competing with the real one |
| "you're on fire", congratulation on volume | volume is not the goal and rewarding it is Goodhart |
| infinite-scroll "more practice" past satisfied | the gradient is zero; serving anyway is a lie |

The honest version of engagement here is that the tool tells you to stop, and
you trust it because it has never inflated anything else.

One consequence worth accepting up front: this UI will feel *less* compelling
than a commercial study app, and that is the design working, not failing.

---

## 7. Milestones

**Priority call (2026-08-01):** the essay is **deferred**. Math and the ELAR
multiple-choice items are the focus, so phase 3 moves behind phase 2 and the
phase-2 reading work is the next thing after phase 1.

It does, however, need a product-config change first. With `essay_score = 0`,
the conjunct `elar_crc_estimate >= 945 AND essay_score >= 5` zeroes the
gradient on every ELAR tag, so the allocator serves **no English items at
all** despite 1,359 of them sitting in band. Splitting the essay into its own
route group leaves P(pass) identical and restores ELAR allocation. See
HANDOFF.md §6.

**Phase 1 — parity plus the reason to exist.**
- [x] Extract `kernel/session.py`; all 76 existing tests pass unchanged
- [x] FastAPI skeleton, `deps.py` — bundled KaTeX still outstanding
- [x] `Now` page with all three states, satisfied state included from day one
- [x] Profile switching (not in the original plan; see HANDOFF.md)
- [x] Drill flow, three phases, with the §4.1 key-absence test in the suite
- [x] Observed `min_hint_level` (§4.2)
- [x] Exchange panel with inline validation
- [x] Bundled KaTeX (0.16.22, woff2 only, 604 KB, no network at runtime)
- [ ] `Report` page, CLI parity

**Phase 2 — the reading half.**
- [ ] Passage pane, pinned across a question set
- [ ] `evidence` field in the return schema; verbatim quote verification (§5.5)
- [ ] `Corpus` browser + review queue; `disputed_key` triage
- [ ] Gold-set promotion

**Phase 3 — the essay. Deferred; see the priority call above.**
- [ ] Draft surface, timer, autosave
- [ ] Blind self-assessment gate
- [ ] Rubric critique, per-dimension storage and trajectory

**Phase 4 — trajectory and polish.**
- [ ] Position-over-time chart from `objective_state`
- [ ] Variance trend per domain
- [ ] CI-width visualization on the reliability table

Phases 1 and 3 are the ones that unlock studying that cannot happen today —
but phase 3 is deferred by the priority call above, which leaves phase 2 as
the next thing that matters. Phase 4 stays pull-based: build it when the
report actually feels blind, not before.

---

## 8. Risks

1. **The web UI is more fun to build than studying is.** Identical in shape to
   the corpus-engineering trap Datasourcing.md §7 names. Phase 1 has a
   defensible end; stop there until real use demands more.
2. **The pedagogy invariants are enforced by convention once there are two
   front ends.** Mitigation: they move into `kernel/session.py` (§2) where
   both inherit them, and §4.1 gets an explicit test.
3. **Scope drift into engagement features**, which will arrive as reasonable
   individual requests. §6 is the standing answer.
4. **Local-only assumptions leaking into the schema.** They should not — the
   schema is already per-learner. But `redistributable = 0` on ~4,400 of the
   8,200 imported items (all of MATH and RACE) means a hosted deployment is a
   licensing question, not a devops one. Recorded here so the decision is not
   made accidentally later.

---

## 9. Open questions

1. **Where does in-progress drill state live?** In-memory keyed by token is
   simplest and loses an ungraded capture on restart. A `drill_sessions` table
   survives restarts and makes abandonment visible — and abandonment is itself
   diagnostic. Leaning in-memory for phase 1, table if abandonment turns out
   to be worth measuring.
2. **Does `evidence` (§5.5) belong in the kernel's return schema or in a
   policy-specific extension?** It is only meaningful under `anchored`. The
   kernel defines the codes and consumes them elsewhere (errors, §5), so the
   precedent points to kernel-defined and optional.
3. **Should `Now` ever offer a choice?** Presenting the top three would feel
   respectful and would quietly reintroduce the learner's own priors, which is
   the thing the allocator exists to replace. Leaning: one recommendation, with
   the ranking visible on `Report` for anyone who wants to audit it.
