# DESIGN.md — Adaptive Exam Study Engine

**Status:** Draft v0.2 — pre-implementation
**Owner:** _(you)_
**Last updated:** 2026-07-30

**Changes from v0.1:** generalized from GRE-only to a multi-exam engine (§2–3);
added content acquisition and labeling pipeline (§11); licensing analysis revised
(§12) — the redistributability constraint inverts between exams.

---

## 1. Purpose

A local-first study system that treats *learning* as the unit of work rather than *scoring*.

Existing test-prep tools optimize the wrong loop. They present an item, mark it right or wrong, and show an official explanation. Three things are wrong with that:

1. **Right/wrong is one bit of information.** It cannot distinguish "solved instantly" from "solved after four minutes of flailing."
2. **Official explanations are not teaching artifacts.** They demonstrate a correct path without accounting for why a solver would *choose* that path. They typically open with the insight that cracks the problem — precisely the step the student could not generate. They answer "is this correct," when the student's question is "what should have made me think of that."
3. **Nothing models prerequisites.** Missing an overlapping-sets problem may indicate a gap in inclusion-exclusion, in fractions-of-fractions, or in parsing "of those who." Re-explaining overlapping sets addresses none of them.

An LLM is used **only as a tutor, diagnostician, and bulk labeler** — never as a grader, never as an item author.

### 1.1 Non-goals

| Not doing | Why |
|---|---|
| Generating practice questions | Synthetic items are off-distribution, especially verbal. Training on bad items teaches patterns that don't transfer. |
| Scoring / grading via LLM | Grading is deterministic against the answer key. Zero model involvement. |
| Score prediction | Unreliable, and it invites optimizing the predictor. |
| Redistributing copyrighted content | See §12. |
| Hosting / accounts / sync | Local-first. SQLite file on disk. |
| API integration (v0–v1) | Deliberate. See §5. |

### 1.2 Success criteria

- **Primary:** operator's GRE score improves materially against a baseline of V155 / Q161.
- **Secondary:** a second exam pack (TSIA2) runs on the same engine with zero changes to engine code.
- **Tertiary:** someone else clones the repo and runs the loop without assistance.

---

## 2. Multi-exam architecture

The engine encodes claims about *how people learn*. Those are exam-independent. Everything about a specific test — sections, item types, scoring, adaptivity, timing — is configuration.

### 2.1 The split

| Universal (engine code) | Per-exam (data / config) |
|---|---|
| Hint ladder L0–L5 | Section and item-type definitions |
| Explain-it-back gate | Tag taxonomy + seed prerequisite edges |
| Pre-answer rationale capture | Difficulty scale |
| Error taxonomy (core codes) | Error taxonomy **extensions** |
| FSRS scheduling over tags | Adaptivity model |
| Deterministic grading | Timing model + per-type time bands |
| Prerequisite DAG traversal | Explanation policy per section |
| Time × correctness analysis | Readiness criteria |
| Copy/paste exchange protocol | Briefing prompt overrides |

**The test of the abstraction:** adding a third exam should require writing YAML and ingesting content. If it requires touching `scheduler.py` or `analytics.py`, the abstraction leaked.

### 2.2 Why TSIA2 is the right second exam

It is not merely "a simpler GRE." It differs on exactly the axes that would otherwise get hardcoded, which makes it a good forcing function:

| | GRE | TSIA2 |
|---|---|---|
| Timed | Yes, tightly | **No** |
| Adaptivity | Section-level | **Item-level** |
| Content taxonomy | Must be invented | **Published by College Board** |
| Scoring | 130–170 scaled, per section | 910–990 CRC + diagnostic level 1–6 |
| Redistributable corpus available | No | **Yes** (see §12) |

Three of those five differences would have been baked into engine code if GRE were the only target.

> **Terminology note.** TSIA2 is a *placement* test — it determines whether you enroll in credit-bearing coursework or developmental education. It does not award college credit. If the actual goal is earning credit by examination, the relevant exam family is CLEP or DSST, which would be a third exam pack. The engine handles either; the distinction matters for choosing what to build.

### 2.3 Design consequences of the differences

**Untimed changes the analytics.** §10.1's "fragile" quadrant (slow + correct) is the score ceiling on a timed exam. On TSIA2 it does not gate the score at all. It remains a *diagnostic* signal — slow-but-correct still indicates shaky understanding — but it must not be weighted as a readiness blocker. Hence `timed: false` in the exam pack, and a `fragile_weight` config value rather than a hardcoded priority.

**Item-level adaptivity changes drill design.** GRE is section-adaptive, so the operator must be able to survive an entire hard section — hence `--hard-only` drill mode. TSIA2 climbs continuously, so what matters is the accuracy ceiling reached and stability near it. Different drill mode (`--ladder`, serving progressively harder items until failure), same engine.

**A published taxonomy is a gift.** TSIA2 math has four named domains — Algebraic Reasoning, Geometric and Spatial Reasoning, Probabilistic and Statistical Reasoning, Quantitative Reasoning — plus diagnostic levels 1–6. That is a prerequisite hierarchy handed over pre-built. Seed `tags.yaml` and `seed_edges.yaml` directly from it. GRE requires deriving the graph from observation (§9).

---

## 3. Exam packs

An exam pack is a directory of YAML. No code.

```
exams/
├── gre/
│   ├── exam.yaml
│   ├── tags.yaml
│   ├── seed_edges.yaml
│   ├── error_codes.yaml        # extensions only
│   └── prompts/                # optional briefing overrides
└── tsia2/
    ├── exam.yaml
    ├── tags.yaml
    ├── seed_edges.yaml
    └── error_codes.yaml
```

### 3.1 `exam.yaml` — GRE

```yaml
exam_id: gre
display_name: "Graduate Record Examination"
timed: true
adaptivity: section-level
fragile_weight: 1.0            # full weight: time is the ceiling

sections:
  - id: quant
    display_name: "Quantitative Reasoning"
    scale: {min: 130, max: 170}
    explanation_policy: withheld     # model reasons freely; see §5.2
    item_types:
      - {id: qc,      display_name: "Quantitative Comparison", time_band_sec: [60, 105]}
      - {id: mc-one,  display_name: "Multiple Choice",         time_band_sec: [60, 120]}
      - {id: mc-many, display_name: "Select All That Apply",   time_band_sec: [75, 135]}
      - {id: numeric, display_name: "Numeric Entry",           time_band_sec: [60, 120]}
  - id: verbal
    display_name: "Verbal Reasoning"
    scale: {min: 130, max: 170}
    explanation_policy: pinned       # defer to official text
    item_types:
      - {id: tc, display_name: "Text Completion",     time_band_sec: [45, 90]}
      - {id: se, display_name: "Sentence Equivalence", time_band_sec: [40, 75]}
      - {id: rc, display_name: "Reading Comprehension", time_band_sec: [60, 120],
         explanation_policy: pinned_strict}   # see §5.2

readiness:
  - {section: quant, target: 167}
  - {section: verbal, target: 161}

drill_modes: [mixed, hard-only, timed-set, full-section, two-pass]
```

### 3.2 `exam.yaml` — TSIA2

```yaml
exam_id: tsia2
display_name: "Texas Success Initiative Assessment 2.0"
timed: false
adaptivity: item-level
fragile_weight: 0.25           # diagnostic signal only, not a readiness gate

sections:
  - id: math
    display_name: "Mathematics"
    scale: {min: 910, max: 990}
    crc_item_count: 20
    explanation_policy: withheld
    item_types:
      - {id: mc-one, display_name: "Multiple Choice", time_band_sec: null}
    domains:                    # published College Board taxonomy
      - algebraic-reasoning
      - geometric-spatial-reasoning
      - probabilistic-statistical-reasoning
      - quantitative-reasoning
  - id: elar
    display_name: "English Language Arts and Reading"
    scale: {min: 910, max: 990}
    crc_item_count: 30
    explanation_policy: pinned
    item_types:
      - {id: mc-one, display_name: "Multiple Choice", time_band_sec: null}
  - id: essay
    display_name: "WritePlacer Essay"
    scale: {min: 1, max: 8}
    scoring: rubric             # out of scope for v0/v1; see §15
    item_types: []

# Institutional variation exists in the ELAR essay cut score. Verify against
# the specific institution before relying on these numbers.
readiness:
  - {section: math, rule: "crc >= 950 OR diagnostic_level == 6"}
  - {section: elar, rule: "crc >= 945 AND essay >= 5"}

diagnostic_levels: {min: 1, max: 6}

drill_modes: [mixed, ladder, domain-focus]
```

---

## 4. System overview

```
┌──────────────┐   ┌──────────────┐
│  exams/*/    │   │  content/    │  items + explanations
│  (in repo)   │   │  (gitignored │  — except redistributable
└──────┬───────┘   │   by default)│    packs; see §12
       │           └──────┬───────┘
       │ load pack        │ ingest → label (§11)
       ▼                  ▼
┌──────────────────────────────────────────────┐
│                SQLite (study.db)             │
│  exams · sections · items · tags · tag_edges │
│  attempts · exchanges · diagnoses · reviews  │
└──────┬─────────────────────────┬─────────────┘
       │                         │
   scheduler                  analytics
       │                         │
       ▼                         ▼
┌──────────────────────────────────────────────┐
│                     CLI                      │
│  drill · tutor · record · label · report     │
└──────┬─────────────────────────┬─────────────┘
       │ renders prompt          │ parses return block
       ▼                         ▲
  ┌─────────────────────────────────┐
  │  LLM chat client of user choice │   ← manual copy / paste
  └─────────────────────────────────┘
```

Everything except the tutoring conversation and bulk labeling is local and deterministic.

---

## 5. The no-API exchange protocol

**Constraint:** no programmatic model access. The user pastes a rendered prompt into whatever chat client they prefer.

Treated as a feature for v0–v1: no API keys, no per-token cost, works with any provider, users leverage subscriptions they already have, and the prompt contract stays explicit and inspectable rather than buried in code.

### 5.1 One briefing, not one exchange per rung

Six copy/pastes per missed item is unusable, and friction on the diagnostic loop is fatal. Instead: **a single briefing handoff.** The tool renders one prompt containing the item, the user's captured rationale, timing, and recent history on the relevant tags, plus instructions for the model to run the hint ladder conversationally. The tutoring happens in the chat window. When finished, the user requests the session record and pastes one structured block back.

The tool's responsibility is **context-out, record-in.**

### 5.2 Explanation policy

Model reasoning is dependable on quantitative items and unreliable on reading comprehension, where it will construct a fluent justification for a wrong choice, or justify a right choice for a reason that isn't why it's right. Either outcome teaches false reasoning — worse than no tutoring.

| Policy | Behavior |
|---|---|
| `withheld` | Official explanation not in the briefing. Model reasons independently from item + key. |
| `pinned` | Explanation included. Model defers to it; unpacks and paraphrases, does not derive. |
| `pinned_strict` | Explanation included. Model explicitly forbidden from constructing independent justifications for answer choices. |

Set per section, overridable per item type. Withholding also removes the temptation to extract the solution immediately.

### 5.3 Accepted tradeoff: honesty enforcement

Under `pinned`, the briefing contains the official explanation, so a user can extract it and self-report `min_hint_level = 1`.

**Accepted, not engineered against.** The user is both subject and sole beneficiary; corrupting the log corrupts only one's own diagnostics, and the log is the entire value of the system. Documenting it plainly is the mitigation.

### 5.4 Return payload

The model emits a single fenced JSON block on request. Parsed locally; rejected on `item_id` mismatch.

```json
{
  "protocol_version": "0.2",
  "exam_id": "gre",
  "item_id": "a3f19c02b7e41d55",
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

**`one_fix` is singular by contract.** Models default to comprehensive lists; comprehensive lists are useless as action items.

---

## 6. Pedagogical theses

Five claims, each mapped to a mechanism. If a claim is wrong, remove the mechanism rather than quietly tolerate it.

| # | Claim | Mechanism |
|---|---|---|
| T1 | The divergence point between a student's reasoning and correct reasoning is more informative than the outcome. | Pre-answer rationale capture (§7.2) |
| T2 | How much help was needed is a continuous mastery signal; correctness is binary. | Hint ladder + `min_hint_level` (§8) |
| T3 | Reconstructing a solution in one's own words is where encoding happens. | Mandatory explain-it-back gate (§7.4) |
| T4 | Knowledge gaps form a DAG; remediation must route downward, not sideways. | Prerequisite graph (§9) |
| T5 | Fluent-but-slow performance is the hidden ceiling **on timed exams** and is invisible to accuracy metrics. | Time × correctness quadrant, weighted by `fragile_weight` (§10.1) |

---

## 7. The study loop

### 7.1 `study drill --exam gre`
Scheduler selects the item. Timer starts on render. Captures `time_to_first_selection`, `time_total`. Timing is recorded even for untimed exams — it is a diagnostic signal regardless of whether it gates the score.

### 7.2 Pre-answer capture (T1)
**Before** the answer is revealed, `$EDITOR` opens and requires:
- **Confidence:** 1 (guess) / 2 (leaning) / 3 (certain)
- **Rationale:** two sentences, written blind. *"I picked C because the sentence pivots on 'nonetheless,' so the blank needs a contrast word."*

Non-optional. Without it the tutor is guessing at the divergence point.

### 7.3 Deterministic grading
Local comparison against the answer key. **No model involvement.**
- Correct and within the item type's `time_band_sec` → schedule, done. No exchange.
- Correct but slow → flag `fragile`. Weighted by `fragile_weight`.
- Wrong → proceed to tutoring.

### 7.4 Tutoring exchange
1. `study tutor <attempt_id>` renders the briefing to the clipboard.
2. User pastes into their chat client, works the hint ladder.
3. In the same conversation, the user writes the **explain-it-back**: the solution path in their own words, three sentences, without looking. The model checks it.
4. User requests the session record; model emits the JSON block.
5. `study record <attempt_id>` opens `$EDITOR` with two sections — paste the JSON, paste the raw explain-back text.

**The explain-back gate is mandatory.** The tool will not mark an item resolved without it. Highest-value ninety seconds in the loop; the step every other tool omits because it is friction; the reason this system exists.

---

## 8. Hint ladder

Exam-independent. The tutor escalates only on request and reports the lowest rung that produced a solution.

| Level | Name | Model may say |
|---|---|---|
| **L0** | Nudge | "Something's off. Try again." Nothing more. |
| **L1** | Locate | Which *phase* failed — setup, execution, or reading. Not what the error is. |
| **L2** | Socratic | One pointed question aimed at the divergence. |
| **L3** | Partial | Walk the solution **up to** the divergence point, then stop. |
| **L4** | Full | Complete worked solution. |
| **L5** | Prerequisite | Full solution plus instruction on the underlying concept. |

`min_hint_level_to_solve` is the headline mastery metric. "Solved at L1" and "needed L4" render identically in every other study tool — a green checkmark — and represent entirely different states of knowledge.

L5 firing repeatedly on one tag triggers upward traversal of the prerequisite graph (§9).

---

## 9. Prerequisite graph

Two population strategies, both supported:

**Seeded** — where the exam publishes a taxonomy. TSIA2 supplies four math domains and six diagnostic levels; encode directly in `seed_edges.yaml` with `confidence: 1.0`.

**Learned** — where it does not. When a diagnosis reports `prerequisite_gaps` containing tag P for an item tagged C, upsert edge (C → P) and increment `evidence_n`. Confidence rises with repeated independent observation. This is the GRE path, and it produces a graph specific to the actual learner, which is more useful than a generic one.

**Routing rule.** When a tag accumulates ≥3 failures at `min_hint_level >= 4`, the scheduler stops serving that tag and serves its highest-confidence parents instead. Re-serving a failing tag while its parent is broken is the sideways remediation this system exists to avoid.

---

## 10. Analytics

### 10.1 Time × correctness quadrant (T5)

|  | Correct | Wrong |
|---|---|---|
| **Fast** | Mastered | Careless / misread |
| **Slow** | **Fragile** | Concept gap |

On timed exams, fragile is the priority bucket: these collapse under real pressure but appear green in every accuracy metric, which is why untimed practice systematically overstates readiness. On untimed exams (`fragile_weight: 0.25`) it is reported as a comprehension signal but does not drive scheduling priority.

### 10.2 Hint-level trajectory
Per tag, `min_hint_level` over time. The actual learning curve. Accuracy is noisy at low n; hint level is finer-grained and more stable.

### 10.3 Adaptivity readiness
- **Section-level (GRE):** report share of practice drawn from `difficulty = hard`. Mixed-difficulty practice under-prepares for the section that determines the ceiling.
- **Item-level (TSIA2):** report the difficulty level at which accuracy crosses below 50% — the practical ceiling — and its stability across sessions.

### 10.4 `study report --exam <id> --week`
Deterministic SQL rollups, not model-generated.
- Fragile count by tag (weighted), descending
- Hint-level trajectory; flag tags with no improvement over 3+ encounters
- Error-code distribution, current week vs trailing four
- Tags due for review; tags blocked pending prerequisites
- Accumulated `one_fix` strings, deduplicated — a personal errata list

---

## 11. Content acquisition and labeling

New in v0.2. This is the subsystem that makes multi-exam actually work, and the one place where the no-API constraint genuinely hurts.

### 11.1 Pipeline

```
raw source → normalize → dedupe → label → review → items table
```

**Normalize.** Every importer emits the same intermediate JSON: `{source, source_ref, section, item_type, stem, choices, answer_key, official_expl?, difficulty?}`. Importers live in `ingest/importers/`, one per source. They are the only source-specific code in the system.

**Dedupe.** Content-addressed IDs — `sha256(exam_id + normalized_stem)[:16]` — catch the same item arriving from multiple study guides, which happens constantly.

**Label.** Assign `tag_slug`s from the exam's taxonomy, and `difficulty` if absent.

**Review.** Human confirmation before model-assigned labels are trusted for routing.

### 11.2 Batch labeling protocol

Tutoring is one item at a time, so copy/paste is fine. Labeling five hundred items one at a time is not.

**Mitigation: batch.** `study label --exam tsia2 --batch 25` renders 25 stems (no explanations, no rationale context — labeling needs far less context than tutoring) into one prompt along with the full permitted tag list. The model returns a JSON array. One paste in, one paste out, 25 items labeled.

Batch size is a tradeoff: larger batches mean fewer round trips but degrade per-item labeling quality. Start at 25 and tune against the gold set (§11.4).

### 11.3 Label provenance

**Mandatory, not optional.** Model-assigned tags feed the prerequisite graph, which drives remediation routing. Bad tags produce confidently wrong study plans — the worst possible failure mode, because it is invisible.

Every tag assignment carries:
- `label_source` — `official` | `human` | `model` | `imported`
- `label_confidence` — model self-reported, or 1.0 for human/official
- `reviewed_by_human` — boolean
- `labeled_at`, `label_prompt_version`

**Routing rule:** edges in the prerequisite graph may only be reinforced by diagnoses on items whose tags are `official`, `human`, or model-assigned **and** human-reviewed. Unreviewed model labels are good enough to serve items for practice; they are not good enough to restructure the study plan.

### 11.4 Gold set

Hand-label ~50 items per exam. Never sent to the model for labeling. Used to score every revision of the labeling prompt: run the prompt against the gold set, compute per-tag precision and recall.

Without this there is no way to tell whether a prompt change improved labeling or quietly degraded it. Cheap to build, and it is the only defense against slow taxonomy drift.

### 11.5 Cheap quality signal

Label each batch twice — two different sessions, ideally two different models. Disagreements go straight to the human review queue. Agreement is not proof of correctness, but disagreement is a reliable proof of *difficulty*, and it concentrates limited review attention where it pays.

---

## 12. Content licensing

**This inverts between exams, and the inversion drives release strategy.**

*Free* and *redistributable* are different properties. ETS, Magoosh, Manhattan, and Kaplan material is copyrighted regardless of price. A repository containing an ingested corpus of any of it is not shareable.

| Exam | Redistributable corpus | Notes |
|---|---|---|
| **GRE** | **No** | All quality material is ETS or commercial prep. Users must supply their own. |
| **TSIA2** | **Yes, largely** | OpenStax developmental math and college algebra are CC BY — redistributable, including commercially, with attribution. Coverage maps closely onto TSIA2 math domains. Khan Academy is CC BY-NC-SA: usable with attribution, non-commercial, share-alike. |

**Strategic consequence.** TSIA2 is the better *public* reference implementation even though GRE is the personal objective. A cloner can run the TSI pack end to end with bundled content and see the system work immediately; the GRE pack requires them to supply material first. If this is ever released as a portfolio artifact, the TSI pack is the demo.

### 12.1 Structural rules

- **Ships in repo:** engine, schema, exam packs, taxonomies, error codes, hint ladder spec, prompt templates, importers, analytics, gold sets *(gold set = tags only, never stems)*.
- **Never in repo:** copyrighted item stems, answer keys, official explanations, passages.
- `content/` and `*.db` in `.gitignore` from the first commit.
- Redistributable content lives in a separate `packs/` tree with per-file license attribution, and only material verified CC BY / CC BY-NC-SA / public domain goes there. Attribution is per-source, recorded in `items.source` and surfaced in a generated `NOTICE` file.
- Content-addressed `item_id` lets two users compare diagnostics on the same item without either transmitting the item.

**These decisions are cheap now and impossible to retrofit.** Do not put explanation text in version control "temporarily."

---

## 13. Data model

SQLite. Append-only where practical.

```sql
-- ─────────────── Exam configuration ───────────────

CREATE TABLE exams (
    exam_id        TEXT PRIMARY KEY,
    display_name   TEXT NOT NULL,
    timed          INTEGER NOT NULL,
    adaptivity     TEXT CHECK (adaptivity IN ('none','item-level','section-level')),
    fragile_weight REAL NOT NULL DEFAULT 1.0,
    pack_version   TEXT NOT NULL
);

CREATE TABLE sections (
    exam_id            TEXT NOT NULL REFERENCES exams(exam_id),
    section_id         TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    scale_min          REAL,
    scale_max          REAL,
    explanation_policy TEXT NOT NULL
        CHECK (explanation_policy IN ('withheld','pinned','pinned_strict')),
    PRIMARY KEY (exam_id, section_id)
);

CREATE TABLE item_types (
    exam_id            TEXT NOT NULL,
    section_id         TEXT NOT NULL,
    item_type_id       TEXT NOT NULL,
    display_name       TEXT NOT NULL,
    time_band_low_sec  REAL,          -- NULL for untimed exams
    time_band_high_sec REAL,
    explanation_policy TEXT,          -- NULL = inherit from section
    PRIMARY KEY (exam_id, section_id, item_type_id),
    FOREIGN KEY (exam_id, section_id) REFERENCES sections(exam_id, section_id)
);

-- ─────────────── Content ───────────────

CREATE TABLE items (
    item_id       TEXT PRIMARY KEY,   -- sha256(exam_id + normalized_stem)[:16]
    exam_id       TEXT NOT NULL REFERENCES exams(exam_id),
    section_id    TEXT NOT NULL,
    item_type_id  TEXT NOT NULL,
    source        TEXT NOT NULL,
    source_ref    TEXT,
    license       TEXT,               -- 'CC-BY-4.0', 'proprietary', ...
    redistributable INTEGER NOT NULL DEFAULT 0,
    difficulty    TEXT,               -- exam-defined scale
    stem          TEXT NOT NULL,
    choices_json  TEXT,
    answer_key    TEXT NOT NULL,
    official_expl TEXT,
    passage_id    TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY (exam_id, section_id, item_type_id)
        REFERENCES item_types(exam_id, section_id, item_type_id)
);

CREATE TABLE tags (
    exam_id      TEXT NOT NULL REFERENCES exams(exam_id),
    tag_slug     TEXT NOT NULL,
    display_name TEXT NOT NULL,
    section_id   TEXT,
    domain       TEXT,                -- e.g. 'algebraic-reasoning'
    level        INTEGER,             -- e.g. TSIA2 diagnostic level 1-6
    notes        TEXT,
    PRIMARY KEY (exam_id, tag_slug)
);

CREATE TABLE item_tags (
    item_id             TEXT NOT NULL REFERENCES items(item_id),
    exam_id             TEXT NOT NULL,
    tag_slug            TEXT NOT NULL,
    label_source        TEXT NOT NULL
        CHECK (label_source IN ('official','human','model','imported')),
    label_confidence    REAL NOT NULL DEFAULT 1.0,
    reviewed_by_human   INTEGER NOT NULL DEFAULT 0,
    label_prompt_version TEXT,
    labeled_at          TEXT NOT NULL,
    PRIMARY KEY (item_id, tag_slug),
    FOREIGN KEY (exam_id, tag_slug) REFERENCES tags(exam_id, tag_slug)
);

CREATE TABLE gold_labels (              -- §11.4; never sent to the model
    item_id  TEXT NOT NULL REFERENCES items(item_id),
    tag_slug TEXT NOT NULL,
    PRIMARY KEY (item_id, tag_slug)
);

CREATE TABLE tag_edges (
    exam_id     TEXT NOT NULL,
    child_slug  TEXT NOT NULL,
    parent_slug TEXT NOT NULL,
    confidence  REAL DEFAULT 0.5,     -- 1.0 when seeded from official taxonomy
    evidence_n  INTEGER DEFAULT 0,
    origin      TEXT CHECK (origin IN ('seeded','learned')),
    PRIMARY KEY (exam_id, child_slug, parent_slug)
);

-- ─────────────── Activity ───────────────

CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    exam_id    TEXT NOT NULL REFERENCES exams(exam_id),
    started_at TEXT NOT NULL,
    ended_at   TEXT,
    mode       TEXT NOT NULL,          -- validated against pack drill_modes
    notes      TEXT
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
    pass_number             INTEGER DEFAULT 1,
    flagged                 INTEGER DEFAULT 0
);

CREATE TABLE exchanges (
    exchange_id       TEXT PRIMARY KEY,
    attempt_id        TEXT NOT NULL REFERENCES attempts(attempt_id),
    briefing_rendered TEXT NOT NULL,   -- exact prompt sent; reproducibility
    prompt_version    TEXT NOT NULL,
    model_reported    TEXT,            -- user-declared
    raw_return_block  TEXT NOT NULL,
    recorded_at       TEXT NOT NULL
);

CREATE TABLE diagnoses (
    diagnosis_id         TEXT PRIMARY KEY,
    exchange_id          TEXT NOT NULL REFERENCES exchanges(exchange_id),
    min_hint_level       INTEGER NOT NULL CHECK (min_hint_level BETWEEN 0 AND 5),
    divergence_point     TEXT,
    explain_back_text    TEXT NOT NULL,
    explain_back_verdict TEXT CHECK (explain_back_verdict IN
                           ('accepted','incomplete','incorrect')),
    one_fix              TEXT NOT NULL,
    diagnosis_confidence TEXT
);

CREATE TABLE error_codes (
    exam_id     TEXT,                  -- NULL = universal core code
    code        TEXT NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY (COALESCE(exam_id, ''), code)
);

CREATE TABLE diagnosis_errors (
    diagnosis_id TEXT NOT NULL REFERENCES diagnoses(diagnosis_id),
    error_code   TEXT NOT NULL,
    PRIMARY KEY (diagnosis_id, error_code)
);

CREATE TABLE diagnosis_gaps (
    diagnosis_id TEXT NOT NULL REFERENCES diagnoses(diagnosis_id),
    tag_slug     TEXT NOT NULL,
    PRIMARY KEY (diagnosis_id, tag_slug)
);

-- ─────────────── Scheduling ───────────────

-- FSRS state keyed on TAG, not item. Resurfacing the same item tests recall
-- of that item; resurfacing the pattern tests the skill.
CREATE TABLE reviews (
    exam_id     TEXT NOT NULL,
    tag_slug    TEXT NOT NULL,
    stability   REAL NOT NULL,
    difficulty  REAL NOT NULL,
    last_review TEXT,
    due_at      TEXT NOT NULL,
    lapses      INTEGER DEFAULT 0,
    reps        INTEGER DEFAULT 0,
    PRIMARY KEY (exam_id, tag_slug)
);
```

### 13.1 Core error taxonomy

Universal codes (`exam_id IS NULL`). Packs may add codes; they may not remove these. The model selects from the list and may not invent codes — freeform text here makes the analytics unaggregatable.

| Code | Description |
|---|---|
| `concept_gap` | Did not know the underlying rule |
| `trigger_miss` | Knew the rule; failed to recognize the cue to apply it |
| `setup_error` | Correct approach, wrong translation of the prompt |
| `execution_error` | Correct setup, arithmetic or algebraic slip |
| `misread` | Missed a word in the stem — *must* vs *could*, *integer*, *distinct*, *positive* |
| `edge_case_miss` | Failed to test negatives, zero, or fractions between 0 and 1 |
| `vocab_gap` | Unknown word (logged in `diagnosis_gaps`) |
| `talked_myself_out` | Had the correct answer, changed it |
| `timeout` | Ran out of clock (timed exams only) |

Additions require a migration and a note in this document.

---

## 14. CLI surface

```
study exams                                 List installed packs
study init --exam <id>                      Load pack into DB

study ingest <path> --exam <id> --source <s>
study label --exam <id> --batch 25          Render labeling prompt → clipboard
study label-record --exam <id>              Paste JSON array back
study review-labels --exam <id>             Human review queue
study eval-labels --exam <id>               Score prompt against gold set

study drill --exam <id> [--tag T] [--mode M] [--n 10]
study tutor <attempt_id>                    Render briefing → clipboard
study record <attempt_id>                   $EDITOR: paste JSON + explain-back
study review --exam <id>                    Serve tags due per FSRS
study report --exam <id> [--week]
study graph --exam <id> [--tag T]
```

**Interface decision:** CLI with `$EDITOR` handoff for all multi-line capture — the `git commit` pattern. Pasting multi-line JSON directly into a terminal is unpleasant (bracketed-paste quirks, escaping); opening an editor sidesteps it and costs almost nothing.

A local single-page web UI is the natural v2 upgrade — better for side-by-side item rendering, and RC passages want real layout. Deferred so it cannot become the project.

---

## 15. Milestones

### v0 — "runs the loop" *(hard limit: one weekend)*

The failure mode for this project is sixty hours of building and ten hours of studying.

- [ ] `schema.sql`, SQLite init, exam pack loader
- [ ] **GRE pack only.** TSIA2 pack is a v1 deliverable.
- [ ] Manual/YAML item entry (importers wait)
- [ ] `study drill` — timing, confidence, rationale, deterministic grading
- [ ] `study tutor` — briefing rendered to clipboard
- [ ] `study record` — JSON parse + explain-back capture
- [ ] One SQL query: the §10.1 quadrant

**The multi-exam abstraction is in v0; the second exam is not.** Getting `exam_id` into the schema and taxonomy into YAML is an afternoon. Sourcing, ingesting, labeling, and reviewing a second corpus is weeks, and it competes directly with studying. Build the seams now, populate them later.

### v1 — "the diagnostics earn their keep"
- [ ] FSRS scheduler over tags
- [ ] Prerequisite edge learning + routing rule
- [ ] `study report` weekly rollups
- [ ] Ingest importers for owned GRE material
- [ ] Drill modes: `hard-only`, `timed-set`, `two-pass`
- [ ] **TSIA2 pack** — taxonomy from published domains, OpenStax ingest
- [ ] Batch labeling + gold set + review queue

### v2 — "shippable to others"
- [ ] Local web UI
- [ ] Prompt template refinement pass against real transcripts
- [ ] `packs/` tree with license attribution + generated `NOTICE`
- [ ] Setup docs
- [ ] Optional: API mode as an alternative to copy/paste
- [ ] Third exam pack as an abstraction test (CLEP/DSST if credit-by-exam is the goal)

---

## 16. Open questions

1. **Prompt templates are stubbed.** §5 fixes the *contract* — what goes out, what comes back, what the model may and may not do. Wording is deferred; it should be tuned against real transcripts. `prompt_version` and `label_prompt_version` are stored so early data stays interpretable after revision.
2. **Time bands** in the GRE pack are placeholders. Derive from the operator's own distribution after ~200 attempts rather than importing generic targets.
3. **Essay / WritePlacer scoring** is out of scope through v1. Rubric-based scoring is a different problem from item diagnosis and probably wants a separate loop. GRE AWA likewise: for most bioinformatics programs a 4.0–4.5 suffices and it is a poor use of hours.
4. **Fragile-item tutoring** — is a "find a faster route" exchange on a correct-but-slow item worth the friction? Test both ways in v1.
5. **Batch size for labeling** — 25 is a guess. Tune against the gold set.
6. **Cross-exam tag sharing.** TSIA2 algebra and GRE algebra overlap substantially. Should packs be able to import tags from each other, or is duplication cleaner? Duplication for now; revisit only if maintenance pain appears.
7. **Two-pass drilling.** The GRE permits movement within a section with no wrong-answer penalty, so skip-and-return is strictly correct strategy. `pass_number` exists; the drill mode does not.

---

## 17. Design principles

1. **The model never grades.** Grading is deterministic, always.
2. **The model never authors items.** Official or openly licensed material only.
3. **Structured output or it didn't happen.** Fixed enums. Freeform diagnostics are unaggregatable.
4. **One fix, not five.** Enforced in the prompt contract.
5. **Blunt over encouraging.** A tutor that calls a wrong mental model "close" is worse than no tutor.
6. **Label provenance is not optional.** Unreviewed model labels may serve practice; they may not restructure the study plan.
7. **Content never enters version control** unless its license explicitly permits redistribution.
8. **Exam-specific knowledge lives in YAML, not code.** If adding an exam requires touching the scheduler, the abstraction leaked.
9. **Friction on the diagnostic loop is fatal.** Every added step must justify itself. The explain-back gate is the sole deliberate exception, and it is exempt because it *is* the mechanism.
