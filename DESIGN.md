# DESIGN.md — Learning Kernel + Study Products

**Status:** Draft v0.3 — pre-implementation, restructured
**Owner:** _(you)_
**Last updated:** 2026-07-30

**Supersedes v0.2.** Restructured from "one engine, multiple exam packs" to
"one learning kernel, multiple products." First product: TSIA2. The optimization
framework is generalized; objective functions become pluggable. This document is
written from first principles and is intended to move into the repo root.

---

## Part I — First principles

### 1. The problem, stated without reference to any exam

A student has a **goal** — pass a threshold, maximize a score, retain a body of
knowledge, reach fluency in a skill. They have finite time and attention. They
have a current state of knowledge that is only partially observable, even to
themselves. The question every study session must answer is:

> **Given my goal and my current state, what is the single best thing to work on
> right now?**

Every existing study tool answers this badly, in one of two ways:

- **The binary curriculum.** Chapter 1, then Chapter 2. The ordering encodes an
  author's guess about a median student who does not exist. It cannot see that
  *this* student's algebra is solid and their geometry is hollow. It optimizes
  coverage, not the goal.
- **Naive adaptivity.** "Review your weakest topic." Weakest-first is only
  correct if the goal rewards uniform strength. Under a threshold goal, your
  weakest topic may be irrelevant; under a coverage-weighted goal, a moderate
  weakness on a high-frequency topic beats a severe weakness on a rare one.

Both fail for the same root cause: **no explicit objective function.** A
curriculum is what you build when you have no objective — the only ordering
left is someone else's chapter sequence. Once the objective is explicit, the
ordering falls out of it, personalized for free.

### 2. The claim this system is built on

> Studying is a resource-allocation problem. Model the learner's state, define
> the goal as an objective over that state, and allocate the next unit of effort
> where the objective's gradient is steepest.

Everything in the kernel is in service of the three components of that sentence:

1. **State estimation** — what does the learner know, how reliably, and with
   what uncertainty?
2. **Objective definition** — what does "done" mean, expressed as a function
   of that state?
3. **Effort allocation** — which action moves the objective most per minute?

The TSIA2 analysis (v0.2 discussion) produced a threshold-crossing objective.
The generalization insight is that **the machinery around it is goal-agnostic**;
only the objective itself is goal-specific. Hence: the optimizer is kernel,
objectives are plugins.

### 3. A stated position on the Goodhart problem

Goal-directed optimization against an assessment has a known failure mode: it
produces brittle, test-shaped knowledge. Optimize purely for P(pass) and the
system will happily teach you to pattern-match item formats rather than
understand mathematics.

Position taken:

- The objective determines **priority** — what to work on next and when to stop.
- The pedagogy determines **method** — how any item is studied: rationale
  capture, hint ladder, explain-it-back, prerequisite routing. These are
  understanding-forcing mechanisms and are **invariant across all objectives.**
- Reliability lower bounds (not point accuracy) are the mastery bar everywhere,
  which structurally resists the shallow-pattern shortcut: pattern-matching is
  exactly the strategy that produces high-variance performance, and variance is
  what the mastery bar punishes.

In short: *what* to study is goal-optimized; *how* to study is never
compromised by the goal. This line is load-bearing and any future feature that
blurs it should be rejected.

---

## Part II — Architecture

### 4. Shape of the system

```
┌─────────────────────────────────────────────────────────────┐
│                       PRODUCT LAYER                         │
│                                                             │
│   ┌─────────────────┐    ┌─────────────────┐    ┌────────┐  │
│   │  tsi-ready       │    │  gre-forge      │    │ future │  │
│   │  (Product 1)     │    │  (Product 2)    │    │ ...    │  │
│   │                  │    │                 │    │        │  │
│   │ domain pack      │    │ domain pack     │    │        │  │
│   │ objective config │    │ objective config│    │        │  │
│   │ content packs    │    │ importers       │    │        │  │
│   │ product prompts  │    │ product prompts │    │        │  │
│   └────────┬─────────┘    └────────┬────────┘    └───┬────┘  │
└────────────┼───────────────────────┼─────────────────┼───────┘
             │      depends on       │                 │
             ▼                       ▼                 ▼
┌─────────────────────────────────────────────────────────────┐
│                   LEARNING KERNEL (core pkg)                │
│                                                             │
│  state estimation   │  objectives (plugin API)  │ pedagogy  │
│  ─ Elo/theta        │  ─ threshold              │ ─ hints   │
│  ─ reliability CIs  │  ─ maximization           │ ─ explain │
│  ─ prerequisite DAG │  ─ mastery/retention      │   back    │
│                     │  ─ deadline-aware wrapper │ ─ capture │
│  ──────────────────────────────────────────────────────────  │
│  allocator (priority function over objectives)               │
│  exchange protocol (briefing out / record in, no API)        │
│  storage (SQLite, append-only activity log)                  │
└─────────────────────────────────────────────────────────────┘
```

**Repo reality vs product fiction.** One monorepo now:
`kernel/` (installable package, zero exam knowledge), `products/tsi-ready/`,
`products/gre-forge/`. The products are configuration-heavy and code-light —
if a product needs kernel changes, the kernel was wrong. Physical separation
into distinct repos happens when a second human uses one of them, not before.
Splitting today buys release-engineering overhead and nothing else.

### 5. What is kernel, what is product

The sorting rule: **kernel code may not contain the name of any exam.** If a
concept only makes sense given a particular test's structure, it is product
configuration.

| Kernel (invariant) | Product (configuration) |
|---|---|
| Learner state model (rating, reliability, variance) | Domain taxonomy + seed prerequisite edges |
| Objective plugin API + allocator | Which objective, with what parameters |
| Hint ladder L0–L5 | Section/item-type definitions, blueprint weights |
| Explain-it-back gate | Explanation policy per section |
| Pre-answer capture (fields configurable) | Which capture fields are active |
| Core error taxonomy + per-code weights | Error-code weight overrides, added codes |
| Prerequisite routing rule | Time bands (or their absence) |
| Exchange protocol + return schema | Briefing prompt overrides |
| FSRS retention scheduling | Content packs and importers |
| Analytics primitives | Report composition |

Two entries deserve emphasis because v0.2 got them wrong:

- **Error-code weights are per-product data, not kernel constants.** The same
  `execution_error` is noise-tier on GRE and catastrophic on an item-adaptive
  CAT with 20 items. The kernel defines the codes and consumes the weights; the
  product supplies the numbers.
- **Capture fields are configurable.** `verification_method` (how did you check
  your answer before submitting?) exists because untimed exams make checking
  free and therefore trainable. It is meaningless under time pressure. The
  kernel supports the field; the TSI product turns it on.

---

## Part III — The kernel

### 6. Learner state estimation

The state is per-(learner, domain-tag) and has three components. All three are
required; any one alone gives a wrong picture.

**6.1 Ability — Elo-family rating.**
Learner and items live on a shared scale, updated after every attempt exactly
like chess ratings. Item difficulty therefore **calibrates itself from attempt
data** — removing the need to trust hand-labeled or model-labeled difficulty,
which was the weakest link in the v0.2 labeling pipeline. With
content-addressed item IDs, calibration can eventually pool across users
without transmitting content.

- `learner_state(exam, tag)`: `rating`, `rating_deviation`, `n_attempts`
- `items`: `rating`, `rating_deviation`, `n_attempts`
- New items start at the pack's prior with high deviation; provisional-period
  K-factor handling as in standard Elo practice.

**6.2 Reliability — the proficiency measure.**
Competence is "can solve it at all" (captured by correctness and
`min_hint_level`). **Proficiency is "solves it reliably and recognizes when it
applies."** Operationalized as a binomial confidence interval over recent
attempts in a difficulty band. A tag's mastery bar is on the **lower bound** of
that interval — two-in-a-row means nothing, and the CI makes that structural
rather than a matter of discipline. `trigger_miss` rate is tracked as the second
proficiency signal: knowing a rule but failing to recognize its cue is the
competence/proficiency gap made measurable.

**6.3 Variance — the consistency measure.**
Rolling variance of rating updates. Motivated by adaptive tests — which
effectively estimate the difficulty at which P(correct) crosses ~50%, and read
erratic performance as lower ability — but retained as a kernel-level signal
for every goal type, because high variance at a level is the empirical
signature of shallow pattern-knowledge (see §3).

### 7. Objective plugins

An objective is a small interface:

```
Objective:
  progress(state)        -> [0, 1]         # where am I?
  gradient(state, tag)   -> float          # marginal value of improving tag
  satisfied(state)       -> bool           # stopping rule
  report(state)          -> summary        # human-readable position
```

**7.1 `threshold` — the TSIA2 case, generalized.**
Outcome is a step function: cross the line or don't. 990 equals 951; 949 equals
910. Therefore optimize **P(crossing)**, not expected score.

- `gradient` concentrates near the boundary: when the estimated position is
  near the cut, small reliability gains on high-frequency tags dominate
  everything; far above it, all gradients go to zero — *further study of a
  satisfied objective is worth exactly nothing*, and the plugin says so.
- **Multiple routes** are first-class: a threshold objective is a boolean
  expression over sub-conditions (e.g. `CRC ≥ 950 OR diagnostic_level = 6`;
  `CRC ≥ 945 AND essay ≥ 5`). The plugin evaluates P(success) per route and
  routes effort toward the steepest — a learner with deep algebra and hollow
  geometry is closer via the no-domain-holes route than via raw ability, and
  the allocator should know it.
- `satisfied` requires clearing the cut **plus a margin sized to measurement
  uncertainty.** Aiming at exactly the line is a coin flip on test day.
  Threshold objectives are the only family with a naturally crisp stopping
  rule; that is their gift, and the margin is the part people skip.

**7.2 `maximize` — the GRE case.**
Every point has value; diminishing returns per tag; gradient follows expected
score gain weighted by blueprint frequency. No natural stopping rule — `satisfied`
is deadline- or target-driven. Fragility (slow-but-correct) enters the gradient
here because time is scored; see §7.5.

**7.3 `mastery` — no exam at all.**
Retention of a body of knowledge: language vocabulary, anatomy, a certification
domain, interview prep. Gradient follows FSRS urgency × coverage weight;
`satisfied` is "all tags above reliability bar with stable retention." This
plugin is the existence proof that the kernel is a *studying* system, not a
*test-prep* system.

**7.4 `deadline(inner)` — a wrapper, not a peer.**
Takes any objective plus a date and reshapes its gradient: as time runs out,
consolidation of near-threshold tags beats opening new fronts, and
spaced-review scheduling compresses. Composition (`deadline(threshold(...))`)
keeps calendar pressure out of every individual objective's logic.

**7.5 Answer to the design question: does threshold scoring generalize?**
**The framework generalizes; the objective does not, and should not.** What
TSIA2 forced into existence — position estimation, gradient-based allocation,
route evaluation, stopping rules with margins — is universal machinery that
every goal type needs. But collapsing every goal into threshold form would be
false: a threshold objective *correctly* abandons satisfied domains, which is
precisely wrong under mastery (retention decays) and wrong under maximization
(points remain valuable). Verdict: **one allocator, plural objectives.** The
scoring method's *skeleton* is centralized; its *shape* stays per-goal.

### 8. The allocator

Priority of working on tag T under objective O:

```
priority(T) = O.gradient(state, T)          # goal leverage
            × learnability(T)               # position on the learning curve
            × availability(T)               # items exist in the right band
```

**`learnability`** peaks where predicted P(correct) ≈ 0.6–0.8 — hard enough to
teach, solvable enough to consolidate. Below ~0.3, prerequisites are missing and
the DAG routes to parents instead (serving the tag itself would be the sideways
remediation this system exists to avoid). Above ~0.85, time is being spent
confirming the known.

**`availability`** is the honest term: the Elo band query must find items. If a
high-priority tag has no items in the target band, that is a *content
acquisition* signal surfaced in the report, not silently skipped.

The allocator is the entire replacement for a curriculum. Its output *is* the
personalized syllabus, recomputed after every attempt.

### 9. Pedagogy layer — invariant across objectives

Unchanged from v0.1/v0.2 in substance; restated as kernel invariants:

- **Pre-answer capture.** Confidence (1–3) + two-sentence rationale, written
  blind, before the answer is revealed. The rationale is the raw material for
  divergence diagnosis; without it the tutor is guessing.
- **Deterministic grading.** The model never grades. Ever.
- **Hint ladder L0–L5.** Escalation only on request; `min_hint_level_to_solve`
  is the headline competence metric — continuous where correctness is binary.
- **Explain-it-back gate.** Solution path in the student's own words, checked,
  stored. Mandatory; an item is not resolved without it. The single
  highest-value minute in the loop and the step every other tool omits because
  it is friction. Mandatory after a *correct* answer too — that is the case it
  exists for, since correctness cannot distinguish L1 from L4. What changes on
  a hit is only the question asked — the blind rationale is handed back and the
  gate asks whether it *held*, rather than asking for it a second time. On a
  miss, "I don't know where to start" is an accepted answer: it is recorded as
  a declaration, it sets `attempts.stuck`, and it routes the exchange to a
  lesson instead of a correction. Not a skip — a demanded path from a learner
  who had no method is a fabrication, and a fabrication reaches the reader
  looking like data. See WEB_UI.md §4.3.
- **Divergence, not just a fix.** The exchange returns `divergence` alongside
  `one_fix`: the first step in the learner's *own* work that is wrong, quoted,
  with the corrected step. The fix is the habit to carry forward and
  generalises past the item; the divergence is the line they actually wrote,
  and it is what they came back to find out. When they showed no steps to
  locate it in, the reader returns a fixed sentence saying so — the only point
  in the loop where "show your work" can be said at the moment it would change
  the next attempt.
- **The explanation is a bridge, not a worked solution.** `explanation` returns
  two things in one pass: what was flawed in the reasoning the learner actually
  gave, and the route from what they did to what they should have done. A
  detached solution — the item worked as though the learner had said nothing —
  leaves them to map it onto their own thinking, which is exactly the step a
  second "explain this" prompt was being sent to get. The measure is that no
  follow-up question is left; three fields, said once each: `divergence` locates
  the break, `one_fix` names the habit, `explanation` walks the route.
  **Bounded by the item's explanation policy** — under `pinned_strict` it
  unpacks the official text, under `anchored` every step is a verbatim quote. An
  "explain step by step" instruction that did not say so would silently license
  the derived reasoning §10 forbids on reading comprehension, and would do it
  while reading as the more specific rule.
- **Prerequisite DAG.** Seeded where an official taxonomy exists; learned from
  diagnosis `prerequisite_gaps` where it does not. Routing rule: ≥3 failures at
  L4+ stops the tag and serves its highest-confidence parents.
- **One fix, not five.** Singular by prompt contract.

### 10. Exchange protocol (no-API)

Carried forward from v0.2 unchanged in mechanism: single briefing handoff
rendered to clipboard; tutoring happens in the user's chat client of choice;
one structured JSON block returns; `study record` parses and rejects on
`item_id` mismatch. Explanation policy (`withheld` / `pinned` /
`pinned_strict`) is per-section product config — model reasoning is dependable
on quantitative material and must be pinned to official text on reading
comprehension, where fluent-but-false justifications teach false reasoning.

Batch labeling, label provenance (`official`/`human`/`model` + review flags),
the gold set, and the rule that **unreviewed model labels may serve practice
but may not reinforce prerequisite edges** all carry forward as kernel
features. Provenance is not optional: bad labels feed the DAG, the DAG drives
routing, and a confidently wrong study plan is the worst failure mode because
it is invisible.

The honesty tradeoff also carries forward: under `pinned`, a user can extract
the explanation and self-report L1. Accepted, not engineered against — the
user is subject and sole beneficiary, and corrupting the log corrupts only
their own diagnostics.

---

## Part IV — Product 1: `tsi-ready`

### 11. Why TSIA2 first

- **Scoped.** Two sections + essay, published four-domain math taxonomy with
  diagnostic levels 1–6 — the prerequisite DAG arrives pre-seeded with
  confidence 1.0, removing the cold-start problem the GRE product has.
- **Redistributable content exists.** OpenStax developmental math and college
  algebra are CC BY and map closely onto the math domains; Khan Academy is
  CC BY-NC-SA. `tsi-ready` can ship with working content and run end-to-end on
  clone. The GRE product structurally cannot (all quality material is ETS or
  commercial). **The TSI product is therefore the public demo even though GRE
  is the personal objective.**
- **It exercises the objective plugin hardest.** Threshold + multiple routes +
  step-function payoff is the most structured objective; building the plugin
  API against the most demanding case first prevents a lazy API.
- Placement (not credit) exam, untimed, item-level adaptive, ~20 math CRC items
  — every one of these properties stresses a kernel seam that GRE would have
  let stay hardcoded.

### 12. Objective configuration

```yaml
objective:
  type: threshold
  margin_policy: rating_deviation      # cut + current measurement uncertainty
  routes:
    math:
      - "crc_estimate >= 950"
      - "all_domains(diagnostic_level >= 6)"
    elar:
      - "crc_estimate >= 945 AND essay_score >= 5"
```

Verify the essay cut against the target institution — published values vary
(4 vs 5), and some institutions use finer-grained placement bands beyond the
binary, which turns the objective into a small ordered set of thresholds.
The plugin handles either; the config must match the actual school.

### 13. TSIA2-specific consequences (from the v0.2 → v0.3 analysis)

**13.1 Untimed inverts error economics.** Checking is free, so careless errors
are *preventable*, so they are treated as unacceptable rather than noise:

- `execution_error` and `misread` carry **elevated weights** in this product —
  counterintuitively higher than on the harder GRE, because on an item-adaptive
  CAT an error costs the item *plus* downward routing, with ~20 items of runway.
- `verification_method` capture field is **on**: before submitting, state how
  the answer was confirmed (back-substitution, magnitude estimate, re-read the
  qualifier, unit check). Directly trainable; attacks the dominant error mode.
- Time deltas are diagnostic only, measured against the learner's own median at
  that difficulty: well-below-median + wrong → didn't engage (the most fixable
  failure on an untimed test); `time_to_first_selection ≈ time_total` → not
  checking at all, which on an untimed exam should never be true by design.

**13.2 Consistency is a first-class target.** The CAT reads variance as lower
ability. Mastery bars sit on reliability lower bounds; the report surfaces
per-domain variance trend, and high-variance tags outrank low-rated ones near
the threshold.

**13.3 Breadth dominates depth.** ~5 math items per domain means no narrow
topic repays deep specialization — blueprint `coverage` weights are flat across
the four domains, the opposite of GRE quant tuning.

**13.4 Drill modes.** `--ladder` (climb difficulty until failure; measures the
ceiling and its session-to-session stability), `--domain-focus`, and
`--full-sitting` — 20 math + 30 ELAR + essay in one block, because the real
constraint on an untimed test is stamina and attention decay, not the clock.

**13.5 The essay is in scope for this product.** ELAR gates on essay ≥ 5
regardless of CRC performance, and it is machine-scored — meaning the scoring
target is *legible*: development, organization, sentence variety, vocabulary
range respond predictably. A small structured practice loop (prompt → draft →
rubric-anchored critique via the exchange protocol → revision) buys a
disproportionate amount. v1 for `tsi-ready`, permanently out of scope for the
GRE product (AWA 4.0–4.5 suffices for the target programs and is a poor use of
hours).

---

## Part V — Product 2 sketch: `gre-forge`

Deferred, but specified enough to prove the kernel seams:

- Objective: `deadline(maximize)` with per-section targets (V161 / Q167 against
  a V155 / Q161 baseline).
- Fragility (slow-but-correct) enters the gradient at full weight — on a timed,
  section-adaptive exam it *is* the score ceiling and is invisible to accuracy
  metrics; untimed practice systematically overstates readiness.
- `--hard-only` drill mode: surviving an entire hard second section is a
  different experience from mixed difficulty, and it is where 167+ lives.
- Two-pass strategy drilling (`pass_number`): no wrong-answer penalty and free
  movement within sections make skip-and-return strictly correct.
- Prerequisite DAG is **learned**, not seeded — no official taxonomy exists.
- User-supplied content only; importers, no bundled packs.

If building `gre-forge` requires kernel changes beyond a new objective config
and product pack, the kernel failed its design test.

---

## Part VI — Delivery

### 14. Repository layout

```
studykernel/                      # monorepo
├── DESIGN.md                     # this file
├── LICENSE                       # MIT or Apache-2.0
├── kernel/                       # installable package; contains no exam names
│   ├── state/        (elo.py, reliability.py, variance.py)
│   ├── objectives/   (base.py, threshold.py, maximize.py, mastery.py, deadline.py)
│   ├── allocator.py
│   ├── pedagogy/     (hints.py, capture.py, explain_back.py, dag.py)
│   ├── exchange/     (briefing.py, record.py, labeling.py, prompts/)
│   ├── storage/      (schema.sql, db.py)
│   └── analytics/
├── products/
│   ├── tsi-ready/
│   │   ├── product.yaml          # sections, item types, capture fields, weights
│   │   ├── objective.yaml
│   │   ├── taxonomy/             # 4 domains × levels 1–6, seeded edges conf=1.0
│   │   ├── packs/                # CC-licensed content, per-file attribution, NOTICE
│   │   └── prompts/
│   └── gre-forge/
│       ├── product.yaml
│       ├── objective.yaml
│       ├── taxonomy/             # seed thin; DAG learns
│       └── importers/
├── content/                      # user-supplied, gitignored
└── tests/
    └── kernel_purity_test.py     # greps kernel/ for exam names; CI-enforced
```

The purity test is not a joke. It is the cheapest possible enforcement of the
architecture's one rule, and it will catch the drift the moment it happens.

### 15. Schema deltas from v0.2

- `learner_state (exam_id, tag_slug, rating, rating_deviation, n_attempts,
  reliability_lo, variance_rolling)`
- `items` + `rating, rating_deviation, n_attempts` (self-calibrating difficulty;
  hand labels demoted to priors)
- `attempts` + `verification_method TEXT NULL` (product-gated capture field)
- `objective_state (exam_id, route_id, p_success, position_estimate, margin,
  satisfied, computed_at)` — position over time is itself a progress artifact
- `error_code_weights (exam_id, code, weight)` — product data, not constants
- Everything else (attempts, exchanges, diagnoses, provenance-carrying
  item_tags, gold_labels, tag_edges, reviews) carries forward unchanged.

### 16. Milestones

**v0 — kernel core + TSI loop** *(hard limit: one weekend of build time)*
- [ ] Schema + product/objective pack loaders
- [ ] Elo state (learner + item), reliability CI
- [ ] `threshold` objective with multi-route evaluation
- [ ] Allocator v1 (gradient × learnability × availability)
- [ ] Drill loop: capture (incl. `verification_method`) → grade → tutor
      briefing → record
- [ ] Seed taxonomy from the published domains; ingest one OpenStax slice
- [ ] Report: position estimate, per-route P(success), domain reliability table

The failure mode remains sixty hours of building and ten of studying. Elo and
the CI are ~50 lines combined; the threshold plugin is an expression evaluator.
None of this is licensed to become a research project.

**v1 — the diagnostics earn their keep**
- [ ] FSRS retention over tags; `deadline` wrapper
- [ ] DAG learned-edge accrual + routing rule
- [ ] Batch labeling + gold set + review queue
- [ ] `--ladder` and `--full-sitting` modes; variance trend in report
- [ ] Essay loop (rubric-anchored, via exchange protocol)
- [ ] Stopping rule live: satisfied-with-margin, and the tool *says you are done*

**v2 — second product + shippable**
- [ ] `gre-forge` on the unchanged kernel (the abstraction test)
- [ ] `maximize` objective; fragility weighting; hard-only + two-pass modes
- [ ] Local web UI; prompt refinement pass against real transcripts
- [ ] Packaging, NOTICE generation, setup docs
- [ ] Optional API mode alongside copy/paste

### 17. Open questions

1. **Elo variant.** Plain Elo vs Glicko-2. Glicko's explicit rating deviation
   feeds the margin policy directly and handles sparse per-tag data better;
   costs modest complexity. Leaning Glicko-2; decide at implementation.
2. **Route P(success) estimation.** Analytic approximation over per-domain
   reliabilities vs Monte Carlo simulation of a 20-item CAT sitting. Simulation
   is ~30 lines and more honest about adaptivity; likely v1.
3. **Cross-product tag identity.** TSIA2 algebra ⊂ GRE algebra. Shared tag
   namespace would let `tsi-ready` progress warm-start `gre-forge` — attractive
   for exactly one user (this one) and a coupling hazard for everyone else.
   Duplicate now; revisit with evidence of real maintenance pain.
4. **Essay scoring fidelity.** Rubric-anchored model critique approximates but
   does not replicate the automated scorer. Position the loop as directional
   (structure, development, variety) and validate against at least one official
   practice essay score if obtainable.
5. **Prompt wording** remains deliberately stubbed; contracts are fixed
   (§10), wording is tuned against real transcripts. `prompt_version` on every
   exchange keeps early data interpretable across revisions.
6. **How much does `mastery` defer?** It is the proof the kernel is a studying
   system rather than a test-prep system, but it has no user yet. Ship the
   plugin interface in v0, the implementation when a real use case exists
   (post-exam retention of TSI math is the natural first one).

### 18. Design principles

1. **What to study is goal-optimized; how to study never is.** (§3 — load-bearing.)
2. **No kernel code may name an exam.** CI-enforced.
3. **One allocator, plural objectives.** Threshold logic never leaks into the
   scheduler; scheduler logic never leaks into an objective.
4. **The model never grades; the model never authors items.**
5. **Mastery lives on reliability lower bounds, not point estimates.**
6. **Structured output or it didn't happen.** Fixed enums; freeform diagnostics
   are unaggregatable.
7. **Label provenance gates the DAG.** Unreviewed model labels serve practice;
   they never restructure the study plan.
8. **Content enters version control only with a redistribution license.**
9. **A satisfied objective reads zero.** The tool must be able to say "stop
   studying" — a study tool that cannot say this is an engagement product.
10. **Friction on the diagnostic loop is fatal.** Every step justifies itself;
    the explain-back gate is the sole exception because it *is* the mechanism.
