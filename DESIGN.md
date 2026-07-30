# DESIGN.md — Adaptive GRE Study Engine

**Status:** Draft v0.1 — pre-implementation
**Owner:** _(you)_
**Last updated:** 2026-07-30

---

## 1. Purpose

A local-first study system that treats *learning* as the unit of work rather than *scoring*.

Existing GRE tools optimize the wrong loop. They present an item, mark it right or wrong, and show an official explanation. Three things are wrong with that:

1. **Right/wrong is one bit of information.** It cannot distinguish "solved instantly" from "solved after four minutes of flailing," and the latter is what caps a score under timed conditions.
2. **Official explanations are not teaching artifacts.** They demonstrate a correct path without accounting for why a solver would *choose* that path. They typically open with the insight that cracks the problem — precisely the step the student could not generate. They answer "is this correct," when the student's question is "what should have made me think of that."
3. **Nothing models prerequisites.** Missing an overlapping-sets problem may indicate a gap in inclusion-exclusion, in fractions-of-fractions, or in parsing "of those who." Re-explaining overlapping sets addresses none of them.

This system exists to close those three gaps. An LLM is used **only as a tutor and diagnostician** — never as a grader, never as an item author.

### 1.1 Non-goals

| Not doing | Why |
|---|---|
| Generating practice questions | Synthetic GRE items are off-distribution, especially verbal. TC/SE depend on precise ETS conventions and a specific vocabulary band. Training on bad items teaches patterns that don't transfer. |
| Scoring / grading via LLM | Grading is deterministic against the answer key. Zero model involvement. |
| Score prediction | Unreliable, and it invites optimizing the predictor. |
| Redistributing copyrighted content | See §10. The repo ships an engine, not a corpus. |
| Hosting / accounts / sync | Local-first. SQLite file on disk. |
| API integration (v0–v1) | Deliberate. See §4. |

### 1.2 Success criteria

- **Primary:** the operator's score improves materially against a baseline of V155 / Q161.
- **Secondary:** another person can clone the repo, point it at content they already own, and run the loop without assistance.

---

## 2. Pedagogical theses

The system is an opinionated encoding of five claims. Each maps to a concrete mechanism. If a claim is wrong, the corresponding mechanism should be removed rather than quietly tolerated.

| # | Claim | Mechanism |
|---|---|---|
| T1 | The divergence point between a student's reasoning and correct reasoning is more informative than the outcome. | Pre-answer rationale capture (§6.2) |
| T2 | How much help was needed is a continuous mastery signal; correctness is a binary one. | Hint ladder + `min_hint_level` (§5) |
| T3 | Reconstructing a solution in one's own words is where encoding happens. | Mandatory explain-it-back gate (§6.4) |
| T4 | Knowledge gaps are a DAG; remediation must route downward, not sideways. | Prerequisite graph (§8) |
| T5 | Fluent-but-slow performance is the hidden score ceiling and is invisible to accuracy metrics. | Time × correctness quadrant analysis (§9.1) |

---

## 3. System overview

```
┌──────────────┐
│   content/   │  official items + explanations, gitignored, user-supplied
└──────┬───────┘
       │ ingest
       ▼
┌──────────────────────────────────────────────┐
│                 SQLite (study.db)            │
│  items · tags · tag_edges · attempts ·       │
│  exchanges · diagnoses · reviews             │
└──────┬─────────────────────────┬─────────────┘
       │                         │
   scheduler                  analytics
       │                         │
       ▼                         ▼
┌──────────────────────────────────────────────┐
│                    CLI                       │
│  drill · tutor · record · review · report    │
└──────┬─────────────────────────┬─────────────┘
       │ renders briefing        │ parses return block
       ▼                         ▲
  ┌─────────────────────────────────┐
  │  LLM chat client of user choice │   ← manual copy / paste
  └─────────────────────────────────┘
```

Everything except the tutoring conversation is local and deterministic.

---

## 4. The no-API exchange protocol

**Constraint:** no programmatic model access. The user pastes a rendered prompt into whatever chat client they prefer.

This constraint is treated as a feature for v0–v1:

- No API keys, no per-token cost, works with any provider
- Users leverage subscriptions they already pay for
- Radically lowers the barrier to someone else cloning and running this
- Forces the prompt contract to be explicit and inspectable rather than buried in code

### 4.1 Why one briefing, not one exchange per hint level

The naive design issues a separate copy/paste round trip per rung of the hint ladder. Rejected — six round trips per missed item is unusable friction, and friction on the diagnostic loop is fatal.

Instead: **a single briefing handoff.** The tool renders one prompt containing the item, the user's captured rationale, timing, recent history on the relevant tags, and instructions for the model to run the hint ladder conversationally. The tutoring conversation then happens naturally in the chat window, which is what a chat window is for. When finished, the user asks for the session record and pastes one structured block back.

The tool's responsibility narrows to **context-out, record-in.**

### 4.2 Explanation policy — differs by section

This follows from a known reliability asymmetry. On quant, model reasoning is dependable and can run free. On reading comprehension — especially inference and primary-purpose items — a model will construct a fluent justification for a wrong choice, or justify a right choice for a reason that isn't why it's right. Either outcome teaches false reasoning, which is worse than no tutoring.

| Section | Official explanation in briefing? | Model instruction |
|---|---|---|
| Quant (all types) | **Withheld** | Reason independently from the item and answer key |
| Verbal — TC / SE | Included | Defer to official text; unpack and paraphrase, do not derive |
| Verbal — RC | Included | Defer to official text; explicitly forbidden from constructing independent justifications |

Withholding on quant has a useful side effect: it removes the temptation to extract the solution immediately.

### 4.3 Accepted tradeoff: honesty enforcement

For verbal, the briefing contains the official explanation, so a user can trivially extract it and self-report `min_hint_level = 1`.

**This is accepted and not worth engineering against.** The user is both the subject and the sole beneficiary. Corrupting the log corrupts only one's own diagnostics, and the log is the entire value of the system. Documenting this plainly is the mitigation.

### 4.4 Return payload

The model is instructed to emit a single fenced JSON block on request. The tool parses it and rejects on `item_id` mismatch (guards against pasting a record against the wrong item).

```json
{
  "protocol_version": "0.1",
  "item_id": "ets-og3-quant-142",
  "min_hint_level": 3,
  "divergence_point": "Assumed x > 0 with no warrant from the stem; never tested a negative value.",
  "error_codes": ["edge_case_miss", "trigger_miss"],
  "prerequisite_gaps": ["qc-variable-sign-testing", "number-properties-negatives"],
  "explain_back_verdict": "incomplete",
  "explain_back_notes": "Reconstructed the algebra but omitted why the negative case flips the comparison.",
  "one_fix": "On every QC with an unconstrained variable, test -1, 0, 1/2 before anything else.",
  "diagnosis_confidence": "high"
}
```

**`one_fix` is singular by contract.** Models default to comprehensive lists; comprehensive lists are useless as action items. The prompt enforces exactly one.

---

## 5. Hint ladder

The tutor escalates only on request and reports the lowest rung that produced a solution.

| Level | Name | Model may say |
|---|---|---|
| **L0** | Nudge | "Something's off. Try again." Nothing more. |
| **L1** | Locate | Which *phase* failed — setup, execution, or reading. Not what the error is. |
| **L2** | Socratic | One pointed question aimed at the divergence. "You wrote that x is positive. What in the stem told you that?" |
| **L3** | Partial | Walk the solution **up to** the divergence point, then stop and hand back. |
| **L4** | Full | Complete worked solution. |
| **L5** | Prerequisite | Full solution plus instruction on the underlying concept. |

`min_hint_level_to_solve` is the headline mastery metric. "Solved at L1" and "needed L4" render identically in every other study tool — a green checkmark — and represent entirely different states of knowledge.

L5 firing repeatedly on one tag is the signal to traverse the prerequisite graph upward (§8).

---

## 6. The study loop

### 6.1 Item presentation — `gre drill`

Scheduler selects the item. Timer starts on render. Captured: `time_to_first_selection`, `time_total`.

### 6.2 Pre-answer capture (T1)

**Before** the answer is revealed, the tool opens `$EDITOR` and requires:

- **Confidence:** 1 (guess) / 2 (leaning) / 3 (certain)
- **Rationale:** two sentences, written blind. *"I picked C because the sentence pivots on 'nonetheless,' so the blank needs a contrast word."*

Non-optional. The rationale is the raw material for the entire diagnostic layer; without it the tutor is guessing at the divergence point.

### 6.3 Deterministic grading

Local comparison against the answer key. Result stored. **No model involvement.**

If correct *and* `time_total` is within the tag's target band → mark, schedule, done. No tutoring exchange.

If correct but **slow** → flag `fragile`. Optionally tutor with the goal of finding a faster route (see T5 / §9.1).

If wrong → proceed to tutoring.

### 6.4 Tutoring exchange

1. `gre tutor` renders the briefing and copies it to the clipboard.
2. User pastes into their chat client and works through the hint ladder.
3. In the same conversation, the user writes the **explain-it-back**: the solution path in their own words, three sentences, without looking. The model checks it against the correct path.
4. User requests the session record; model emits the JSON block.
5. `gre record` opens `$EDITOR` with two sections — paste the JSON, paste the raw explain-back text. Both are stored; the raw text is the best retrieval cue available a month later.

**The explain-back gate is mandatory.** The tool will not mark an item resolved without it. This is the highest-value ninety seconds in the loop, it is the step every other study tool omits because it is friction, and it is the reason this system exists.

---

## 7. Data model

SQLite. Append-only where practical. `schema.sql` ships in the repo; `content/` does not.

```sql
-- ─────────────── Content ───────────────

CREATE TABLE items (
    item_id           TEXT PRIMARY KEY,   -- content-addressed; see §10
    source            TEXT NOT NULL,      -- 'ets-og3', 'ets-quant-supp', 'khan', ...
    source_ref        TEXT,               -- page / problem number
    section           TEXT NOT NULL CHECK (section IN ('quant','verbal')),
    item_type         TEXT NOT NULL CHECK (item_type IN
                        ('qc','mc-one','mc-many','numeric','tc','se','rc')),
    difficulty        TEXT CHECK (difficulty IN ('easy','medium','hard')),
    stem              TEXT NOT NULL,
    choices_json      TEXT,
    answer_key        TEXT NOT NULL,
    official_expl     TEXT,
    passage_id        TEXT,               -- RC items sharing a passage
    created_at        TEXT NOT NULL
);

CREATE TABLE tags (
    tag_slug     TEXT PRIMARY KEY,        -- 'qc-variable-sign-testing'
    display_name TEXT NOT NULL,
    section      TEXT NOT NULL,
    notes        TEXT
);

CREATE TABLE item_tags (
    item_id  TEXT NOT NULL REFERENCES items(item_id),
    tag_slug TEXT NOT NULL REFERENCES tags(tag_slug),
    PRIMARY KEY (item_id, tag_slug)
);

-- Prerequisite DAG. child requires parent. Built incrementally from
-- observed diagnoses rather than authored upfront. See §8.
CREATE TABLE tag_edges (
    child_slug   TEXT NOT NULL REFERENCES tags(tag_slug),
    parent_slug  TEXT NOT NULL REFERENCES tags(tag_slug),
    confidence   REAL DEFAULT 0.5,        -- raised as evidence accumulates
    evidence_n   INTEGER DEFAULT 0,
    PRIMARY KEY (child_slug, parent_slug)
);

-- ─────────────── Activity ───────────────

CREATE TABLE sessions (
    session_id  TEXT PRIMARY KEY,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    mode        TEXT NOT NULL CHECK (mode IN
                  ('drill','timed-set','full-section','review')),
    notes       TEXT
);

CREATE TABLE attempts (
    attempt_id              TEXT PRIMARY KEY,
    session_id              TEXT NOT NULL REFERENCES sessions(session_id),
    item_id                 TEXT NOT NULL REFERENCES items(item_id),
    ts                      TEXT NOT NULL,
    time_to_first_selection REAL,
    time_total              REAL NOT NULL,
    selected                TEXT,
    is_correct              INTEGER NOT NULL,
    confidence              INTEGER CHECK (confidence BETWEEN 1 AND 3),
    pre_answer_rationale    TEXT NOT NULL,
    pass_number             INTEGER DEFAULT 1,   -- two-pass strategy drilling
    flagged                 INTEGER DEFAULT 0
);

CREATE TABLE exchanges (
    exchange_id       TEXT PRIMARY KEY,
    attempt_id        TEXT NOT NULL REFERENCES attempts(attempt_id),
    briefing_rendered TEXT NOT NULL,      -- exact prompt sent; reproducibility
    prompt_version    TEXT NOT NULL,
    model_reported    TEXT,               -- user-declared, e.g. 'claude-opus-5'
    raw_return_block  TEXT NOT NULL,
    recorded_at       TEXT NOT NULL
);

CREATE TABLE diagnoses (
    diagnosis_id        TEXT PRIMARY KEY,
    exchange_id         TEXT NOT NULL REFERENCES exchanges(exchange_id),
    min_hint_level      INTEGER NOT NULL CHECK (min_hint_level BETWEEN 0 AND 5),
    divergence_point    TEXT,
    explain_back_text   TEXT NOT NULL,    -- the user's own words (T3)
    explain_back_verdict TEXT CHECK (explain_back_verdict IN
                          ('accepted','incomplete','incorrect')),
    one_fix             TEXT NOT NULL,
    diagnosis_confidence TEXT
);

-- Fixed enum. Freeform text here makes the analytics unaggregatable.
CREATE TABLE diagnosis_errors (
    diagnosis_id TEXT NOT NULL REFERENCES diagnoses(diagnosis_id),
    error_code   TEXT NOT NULL REFERENCES error_codes(code),
    PRIMARY KEY (diagnosis_id, error_code)
);

CREATE TABLE error_codes (
    code        TEXT PRIMARY KEY,
    description TEXT NOT NULL
);

CREATE TABLE diagnosis_gaps (
    diagnosis_id TEXT NOT NULL REFERENCES diagnoses(diagnosis_id),
    tag_slug     TEXT NOT NULL REFERENCES tags(tag_slug),
    PRIMARY KEY (diagnosis_id, tag_slug)
);

-- ─────────────── Scheduling ───────────────

-- FSRS state, keyed on TAG not item. Resurfacing the same item tests
-- recall of that item; resurfacing the pattern tests the skill.
CREATE TABLE reviews (
    tag_slug      TEXT PRIMARY KEY REFERENCES tags(tag_slug),
    stability     REAL NOT NULL,
    difficulty    REAL NOT NULL,
    last_review   TEXT,
    due_at        TEXT NOT NULL,
    lapses        INTEGER DEFAULT 0,
    reps          INTEGER DEFAULT 0
);
```

### 7.1 Error taxonomy

Seeded into `error_codes`. Fixed enum — the model selects from this list and may not invent codes.

| Code | Description |
|---|---|
| `concept_gap` | Did not know the underlying rule |
| `trigger_miss` | Knew the rule; failed to recognize the cue to apply it |
| `setup_error` | Correct approach, wrong translation of the prompt |
| `execution_error` | Correct setup, arithmetic or algebraic slip |
| `misread` | Missed a word in the stem — *must* vs *could*, *integer*, *distinct*, *positive* |
| `edge_case_miss` | Failed to test negatives, zero, or fractions between 0 and 1 |
| `vocab_gap` | Unknown word (log the specific word in `diagnosis_gaps`) |
| `talked_myself_out` | Had the correct answer, changed it |
| `timeout` | Ran out of clock |

Additions require a schema migration and a note in this document. Ad-hoc growth of this list defeats its purpose.

---

## 8. Prerequisite graph

Edges are **learned, not authored.** Authoring a complete GRE prerequisite DAG upfront is a large project of dubious accuracy; deriving one from observed diagnoses produces a graph specific to the actual learner, which is more useful.

Mechanism: when a diagnosis reports `prerequisite_gaps` containing tag P for an item tagged C, upsert edge (C → P) and increment `evidence_n`. Confidence rises with repeated independent observation.

**Routing rule.** When a tag accumulates ≥3 failures at `min_hint_level >= 4`, the scheduler stops serving items for that tag and instead serves items for its highest-confidence parents. Re-serving the failing tag when the parent is broken is the sideways remediation this system exists to avoid.

Seed the graph with a thin hand-authored layer for obvious structure (e.g. `overlapping-sets → inclusion-exclusion → fraction-arithmetic`), then let observation thicken it.

---

## 9. Analytics

### 9.1 Time × correctness quadrant (T5)

Cross-tab per tag, with the time threshold set per item type:

|  | Correct | Wrong |
|---|---|---|
| **Fast** | Mastered | Careless / misread |
| **Slow** | **Fragile** | Concept gap |

**Fragile is the priority bucket.** These problems collapse under real time pressure but appear green in every accuracy metric, which is why untimed practice systematically overstates readiness. Report fragile-count by tag as a first-class number, not a footnote.

### 9.2 Hint-level trajectory

Per tag, `min_hint_level` over time. This is the actual learning curve. Accuracy is noisy at low n; hint level is a finer-grained and more stable signal.

### 9.3 Section-adaptive readiness

The GRE is **section-level adaptive** — performance on the first quant section determines the difficulty of the second. Hitting Q167+ requires surviving the hard second section, which is a materially different experience from mixed-difficulty practice.

Track and report the share of practice drawn from `difficulty = 'hard'` only, and support a `--hard-only` drill mode. Mixed-difficulty practice systematically under-prepares for the section that determines the ceiling.

### 9.4 Weekly report — `gre report`

Rendered locally from SQL. Deliberately not model-generated; these are deterministic rollups.

- Fragile count by tag, sorted descending
- Hint-level trajectory, flagging tags with no improvement over 3+ encounters
- Error-code distribution, current week vs trailing four
- Tags due for review, and tags blocked pending prerequisites
- Accumulated `one_fix` strings from the week — deduplicated, this is a personal errata list

---

## 10. Content and licensing boundary

**This matters for the goal of publishing the tool.** *Free* and *redistributable* are different properties. ETS, Magoosh, Manhattan, and Kaplan material is copyrighted regardless of price. A repository containing an ingested corpus of any of it is not shareable.

Architecture that survives this:

- **Ships in repo:** engine, schema, error taxonomy, hint ladder spec, prompt templates, seed tag list, importers, analytics.
- **Never in repo:** item stems, answer keys, official explanations, passages.
- `content/` and `*.db` are in `.gitignore` from the first commit.
- `item_id` is **content-addressed** — `sha256(source + source_ref + normalized_stem)[:16]`. This lets two users compare diagnostics on the same item without either transmitting the item.

Khan Academy material is CC BY-NC-SA, making it roughly the only large body of quant instruction that *can* be redistributed with attribution under non-commercial terms. If a bundled starter corpus is ever wanted, that is the only viable source — and it still requires care around the NC and SA terms.

**These are structural decisions that are cheap now and impossible to retrofit.** Do not put explanation text in version control "temporarily."

---

## 11. CLI surface (v0)

```
gre ingest <path> --source ets-og3     Import items into the local DB
gre drill [--tag T] [--hard-only] [--n 10]
                                        Serve items; capture timing,
                                        confidence, rationale; grade locally
gre tutor <attempt_id>                  Render briefing → clipboard
gre record <attempt_id>                 Open $EDITOR; paste JSON + explain-back
gre review                              Serve items for tags due per FSRS
gre report [--week]                     Weekly rollups
gre graph [--tag T]                     Inspect prerequisite edges
```

**Interface decision:** CLI with `$EDITOR` handoff for all multi-line capture — the `git commit` pattern. Pasting a multi-line JSON block directly into a terminal is genuinely unpleasant (bracketed-paste quirks, escaping); opening an editor sidesteps it entirely and costs almost nothing to build.

A local single-page web UI is the natural v1 upgrade — it makes side-by-side item rendering and paste-back materially nicer, and RC passages in particular want real layout. Deferred so it cannot become the project. See §13.

---

## 12. Repository layout

```
gre-tutor/
├── README.md
├── DESIGN.md                ← this file
├── LICENSE                  ← MIT or Apache-2.0
├── .gitignore               ← content/, *.db, .env
├── pyproject.toml
├── src/gretutor/
│   ├── cli.py
│   ├── db.py
│   ├── schema.sql
│   ├── grading.py           ← deterministic; no model
│   ├── scheduler.py         ← FSRS over tags
│   ├── analytics.py
│   ├── prompts/
│   │   ├── briefing_quant.md.j2
│   │   ├── briefing_verbal.md.j2
│   │   └── return_schema.json
│   ├── ingest/
│   │   ├── base.py
│   │   └── importers/
│   └── taxonomy/
│       ├── tags.yaml
│       ├── seed_edges.yaml
│       └── error_codes.yaml
├── content/                 ← GITIGNORED. user-supplied.
└── tests/
```

---

## 13. Milestones

### v0 — "runs the loop" *(target: one weekend)*

Hard scope limit. The failure mode for this project is sixty hours of building and ten hours of studying.

- [ ] `schema.sql`, SQLite init
- [ ] Manual/YAML item entry (importers can wait)
- [ ] `gre drill` — timing, confidence, rationale, deterministic grading
- [ ] `gre tutor` — briefing template rendered to clipboard
- [ ] `gre record` — JSON parse + explain-back capture
- [ ] One SQL query: the §9.1 quadrant

Anything past this competes directly with studying.

### v1 — "the diagnostics earn their keep"

- [ ] FSRS scheduler over tags
- [ ] Prerequisite edge learning + routing rule
- [ ] `gre report` weekly rollups
- [ ] Ingest importers for owned material
- [ ] `--hard-only` and timed-set modes

### v2 — "shippable to others"

- [ ] Local web UI
- [ ] Prompt template refinement pass (see §14)
- [ ] Setup docs for pointing at user-owned content
- [ ] Optional: API mode as an alternative to copy/paste

---

## 14. Open questions

1. **Prompt templates are stubbed.** §4 fixes the *contract* — what goes out, what comes back, what the model may and may not do. The wording is deliberately deferred; it should be tuned against real transcripts rather than guessed at. `prompt_version` is stored on every exchange so early data remains interpretable after revision.
2. **Time thresholds per item type** for the fragile/mastered split are unset. Derive from the operator's own distribution after ~200 attempts rather than importing generic targets.
3. **Fragile-item tutoring** — is a "find a faster route" exchange on a correct-but-slow item worth the friction? Test both ways in v1.
4. **AWA** is entirely out of scope. For most bioinformatics programs a 4.0–4.5 is sufficient and it is a poor use of hours. Revisit only if a target program signals otherwise.
5. **Two-pass strategy drilling.** The GRE permits movement within a section and imposes no wrong-answer penalty, so a skip-and-return pass is strictly correct strategy. `pass_number` exists in the schema; the drill mode that exercises it does not yet.

---

## 15. Design principles

1. **The model never grades.** Grading is deterministic, always.
2. **The model never authors items.** Official material only.
3. **Structured output or it didn't happen.** Fixed enums. Freeform diagnostics are unaggregatable.
4. **One fix, not five.** Enforced in the prompt contract.
5. **Blunt over encouraging.** A tutor that calls a wrong mental model "close" is worse than no tutor.
6. **Content never enters version control.**
7. **Friction on the diagnostic loop is fatal.** Every added step must justify itself against the risk of abandonment. The explain-back gate is the sole deliberate exception, and it is exempt because it *is* the mechanism.
