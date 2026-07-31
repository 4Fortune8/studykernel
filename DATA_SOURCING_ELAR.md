# DATA_SOURCING_ELAR.md — Sourcing the TSIA2 ELAR Bank

**Status:** Draft v0.1
**Scope:** English Language Arts and Reading (ELAR) — reading, writing/revision,
and essay — for the `tsi-ready` product.
**Sibling to:** DATA_SOURCING_MATH.md. **Companion to:** DESIGN.md Part IV.

---

## 0. Why ELAR cannot reuse the math pipeline

Three structural differences break the math sourcing strategy:

1. **Items are passage-anchored.** A reading item is a passage plus a question
   set, not a standalone stem. Sourcing, storage, and serving all operate on the
   passage as the unit.
2. **The writing half has no public dataset.** There is no corpus of
   "which revision improves this sentence" MCQ items. Grammar-correction
   datasets (JFLEG, BEA) are the wrong shape — corrections, not choices.
3. **Available reading sources ship without explanations**, which collides with
   the `pinned` explanation policy that RC items require under DESIGN.md §5.2.

Consequently ELAR splits into three sub-problems with three different
solutions: **source** reading, **manufacture** writing/revision, and **author
prompts + rubric-loop** the essay.

Blueprint reminder (why the split matters): the 30-item ELAR CRC covers four
areas — literary text analysis, informational text analysis and synthesis,
essay revision and editing, and sentence revision/editing/completion — with
writing/revision occupying roughly **half** the items. Readiness additionally
requires the WritePlacer essay ≥ 5 *regardless* of CRC score (verify the exact
cut against the target institution; published values vary between 4 and 5).

---

## 1. ELAR tag taxonomy

```yaml
# products/tsi-ready/taxonomy/elar_tags.yaml
- literary-analysis            # theme, tone, characterization, figurative language
- informational-analysis       # main idea, inference, author's purpose, evidence
- synthesis-two-sources        # cross-passage synthesis items
- sentence-revision            # parent tag; children below
    - sv-agreement
    - verb-tense
    - comma-splice
    - fragment
    - run-on
    - pronoun-antecedent
    - apostrophe-possessive
    - dangling-modifier
    - parallel-structure
    - word-choice-homophone
- essay-revision               # rhetorical: transitions, ordering, unity
- essay-writing                # WritePlacer; scored by rubric loop, not key
```

The `sentence-revision` children are exactly the transformation library of the
injection generator (§3) — **the tag is the transformation**, which is what
makes generated items arrive labeled for free.

---

## 2. Reading — RACE

### 2.1 Role and fit

**RACE** (`race` on Hugging Face; configs `middle`, `high`, `all`) is the bulk
reading source: ~28K passages / ~98K four-option MCQ items collected from real
middle- and high-school English exams. Fit:

- **Difficulty band is right by construction.** RACE-M sits below the TSIA2
  readiness line, RACE-H at and above it — a ready-made two-band ladder for
  Elo priors, the same role the MATH `level` field played for math.
- **Passages arrive with question sets attached**, mapping directly onto the
  existing `passage_id` grouping. No schema change.
- Exam-style MCQ with answer keys → deterministic grading holds.

Honest caveats, recorded not hidden:

- **Research license** → `redistributable = 0`, `license = "race-research"`.
  Fine for the local project; auto-excluded from any shared pack.
- **Answer-key noise** comparable to MMLU. The `disputed_key` flag path from
  DATA_SOURCING_MATH.md §4 covers it (and §2.4 below strengthens detection).
- **Coverage skew:** strong on literal comprehension and inference; thin on the
  *synthesis-across-two-sources* item type. That tag is supplied by official
  samples only and will read as supply-constrained in the availability report.
  This is correct behavior, not a bug to paper over.

### 2.2 Field mapping

```python
from datasets import load_dataset
ds = load_dataset("race", "all")   # splits: train, validation, test
```

| RACE field | Maps to |
|---|---|
| `article` | passage text (stored once; see §2.3) |
| `question` | `stem` |
| `options` (list of 4) | `choices_json` |
| `answer` (`"A"`–`"D"`) | `answer_key` (already a letter; no conversion) |
| config (`middle`/`high`) | `difficulty_prior` band (low / mid-high) |

`item_type = mc-one`, `section = elar`, `official_expl = NULL` → items get
explanation policy `anchored` (§2.4).

**Volume control.** ~98K items would drown the bank. Ingest a stratified sample:
~400 passages (≈ 60% high, 40% middle), question sets intact — roughly
1,500–2,000 items. Passages are sampled, never split; serving a passage means
serving its full set (multipart-adjacent practice for free). Seed the sampler
for reproducibility; pull more later if bands run dry.

**Cleanup at ingest:** drop passages containing exam artifacts
(`_____` cloze markers referencing absent numbering, "according to the chart"
with no chart). Drop items whose options include "All of the above" less than
cleanly extracted. Expect low single-digit % loss.

### 2.3 Passage storage

Passages are stored once in a `passages` table (`passage_id`, `text`, `genre`,
`source`, `word_count`), items reference `passage_id`. The passage, not the
item, carries the genre tag; items inherit it. `passage_id =
sha256(normalized_passage_text)[:16]` — same content-addressing convention.

### 2.4 The `anchored` explanation policy (new; required by this source)

Existing policies fail here: `withheld` is banned for RC (fluent-fabrication
risk), `pinned` is impossible with no official explanation. The middle
position for explanation-less RC:

> **`anchored`** — the model may justify the keyed answer, but every claim in
> its reasoning must **quote the passage verbatim**, and it must state, for
> each rejected choice, the specific passage text that rules it out or note
> the absence of support. If the model cannot find textual support for the
> keyed answer, it must say so and emit `disputed_key`.

This does not eliminate fabrication risk — the model knows the key and will
rationalize toward it — but it converts *unfalsifiable fluent reasoning* into
*falsifiable quoted reasoning*. The user has the passage in front of them;
every anchor is checkable in seconds. The escape clause doubles as the
answer-key noise detector for this source.

Engineering surface: one new enum value in `sections.explanation_policy`
(`'anchored'`), one briefing template variant. The policy hierarchy is now:

| Policy | When | Model behavior |
|---|---|---|
| `withheld` | Quant | Reason freely from item + key |
| `pinned` | Verbal w/ official expl | Defer; unpack, don't derive |
| `pinned_strict` | RC w/ official expl | Defer; independent justification forbidden |
| `anchored` | RC w/o official expl | Justify only via verbatim passage quotes; flag unsupported keys |

### 2.5 Genre labeling — one cheap batch pass

The literary/informational split is genre classification, not judgment — a
model does it near-perfectly from the first paragraph. Batch protocol
(DESIGN.md lineage §11.2), **passages not items**: ~400 passages at batch 25 ≈
16 round trips, `label_source = model`. Items inherit the passage tag.
Spot-check 20; this label feeds serving balance, not DAG edges, so unreviewed
is acceptable indefinitely.

**Reading yield: ~1,500–2,000 items across two tags for one importer,
~16 labeling round trips, and a 20-passage spot check.**

---

## 3. Writing/revision — the error-injection generator

### 3.1 Why generation is allowed here

Design principle: *the model never authors items.* That principle exists
because model-authored items have (a) unverifiable answer keys and
(b) off-distribution style. **Rule-based error injection has neither problem**,
and no model is involved in authoring:

1. Take a clean, well-formed sentence from a controlled corpus.
2. Programmatically inject **one error from a closed transformation library** —
   deterministic code, one function per error type (~10–30 lines each).
3. Item: *"Which revision corrects the sentence?"* Key = the original sentence
   (or minimal fix). Distractors = **other** transformations applied to the
   same sentence.

The key is known-correct **by construction** — the generator made the error, so
the generator knows the fix. The transformation name is the tag, so items
arrive labeled at zero cost.

### 3.2 Sentence corpus

| Corpus | Register | License |
|---|---|---|
| Project Gutenberg (curated, post-1850 prose, 8–25 word sentences) | literary | Public domain |
| Simple English Wikipedia / OpenStax prose | informational | CC |

Sentence filter: complete, single clause or clean two-clause, no dialogue, no
archaic constructions, no proper-noun density. A few hundred base sentences is
plenty — each yields multiple items across transformations.

These items are `redistributable = 1` — the *only* fully shareable ELAR
supply, which matters for the future public pack.

### 3.3 Transformation library (initial ten)

`sv-agreement`, `verb-tense`, `comma-splice`, `fragment`, `run-on`,
`pronoun-antecedent`, `apostrophe-possessive`, `dangling-modifier`,
`parallel-structure`, `word-choice-homophone`.

Rules of the library:
- Each transformation is a pure function `sentence -> corrupted_sentence`,
  with a validity check (some sentences don't admit some errors; skip, don't
  force).
- **One error per item.** Compound-error items test error-spotting under
  ambiguity, which is a different (and off-blueprint) skill.
- Distractor rule: 3 distractors = the injected error itself (unchanged option)
  + two *different* transformations of the same sentence. All four options are
  therefore the same sentence in four states — exactly the TSIA2 revision
  format.
- Difficulty prior per transformation (homophones easy, dangling modifiers
  hard), refined by Elo like everything else.

### 3.4 The generator's unique property: supply on demand

Every sourced corpus is finite; the generator is not. When the allocator
reports the `comma-splice` tag needs work at a given band, `study generate
--tag comma-splice --band B --n 10` manufactures fresh supply at that band.
The `availability` term of the priority function can never starve for
mechanics tags. **This is the first component where the tool doesn't just
diagnose a weakness but manufactures targeted supply for it** — quietly the
most novel piece of the system, and worth calling out in any future writeup.

### 3.5 What injection cannot cover

Rhetorical revision — "which sentence best transitions between these
paragraphs?", ordering, unity, adding/deleting for purpose — is not
rule-generable; those judgments aren't deterministic transformations. The
`essay-revision` tag is supplied only by hand-entered official samples and
college-packet items. It will be supply-constrained; the availability report
says so honestly rather than the generator faking coverage it can't verify.

---

## 4. Essay — authored prompts + rubric loop

### 4.1 Prompts are authorable

The never-author rule protects answer keys; **an essay prompt has no key** —
there is nothing for a model to get silently wrong. Authoring ~40
WritePlacer-style persuasive prompts (single debatable issue, no research
required, accessible to any test-taker) is safe and sufficient. Store as items
with `item_type = essay`, `answer_key = NULL`.

### 4.2 The loop (v1 deliverable, promoted in priority)

```
prompt → timed draft (~30 min) → blind self-assessment → paste draft into
exchange → rubric-anchored critique → single-dimension revision → re-critique
```

- **Blind self-assessment first:** one paragraph scoring one's own draft
  against the rubric *before* seeing the critique — the essay analog of
  explain-it-back, same blind-then-compare mechanic, same encoding reason.
- **Critique prompt** is anchored to the WritePlacer dimensions:
  purpose/focus, organization/structure, development/support, sentence
  variety/style, mechanical conventions. It must return scores per dimension
  plus **one** dimension to fix — the essay-loop instance of *one fix, not
  five*.
- **Weighting:** the real scorer is automated and responds legibly to
  structural features — development/length, clear organization, sentence
  variety — so the critique weights structure over argumentative subtlety.
  This is not gaming; the scoring target is simply legible, and structure is
  where practice pays fastest.
- Drafts are stored as `attempts` against the prompt item; dimension scores go
  in the exchange return payload; the trajectory per dimension is the essay's
  hint-level-curve analog.

### 4.3 Validation caveat

Rubric-anchored model critique approximates but does not replicate the
automated scorer. Treat the loop as directional. If an official practice essay
with a real score can be obtained, calibrate against it once.

### 4.4 Scheduling note — the essay is a gate, not a polish step

ELAR readiness = CRC **and** essay. A learner can be excellent on
multiple-choice and fail ELAR on the essay alone. The essay loop enters the
rotation **early** (one full cycle per week from the start), not as a final
polish. The objective plugin's ELAR route already encodes this
(`crc_estimate >= 945 AND essay_score >= 5`); the schedule must respect it.

---

## 5. Supply summary

| ELAR tag | Source | Supply | Redistributable | Labeling cost |
|---|---|---|---|---|
| `literary-analysis` | RACE (literary) + official | Large | No | Genre batch pass (shared) |
| `informational-analysis` | RACE (informational) + official | Large | No | Same pass |
| `synthesis-two-sources` | Official samples only | **Thin** | No | Hand-entered |
| `sentence-revision/*` | Injection generator | **Unlimited, on demand** | **Yes** | Zero — tag = transformation |
| `essay-revision` | Official + packets | **Thin** | No | Hand-entered |
| `essay-writing` | Authored prompts | Unlimited | Yes | Zero |

**Strategic note for the operator's friend:** half the ELAR CRC is
writing/revision. The universal failure pattern is over-preparing reading
(because RC practice is abundant) and under-preparing revision (because it
isn't). The injection generator exists precisely to prevent that pattern —
trust the allocator when it keeps serving mechanics items.

---

## 6. Implementation checklist

- [ ] `passages` table + migration; `passage_id` content addressing
- [ ] `anchored` added to `explanation_policy` enum + briefing template variant
- [ ] `ingest/importers/hf_race.py` — stratified passage sampler, artifact
      cleanup, set-intact serving
- [ ] Genre batch-label pass (~16 round trips) + 20-passage spot check
- [ ] `generate/` module: sentence corpus curator + transformation library
      (start with 4: `sv-agreement`, `comma-splice`, `fragment`,
      `verb-tense`; grow incrementally)
- [ ] `study generate --tag T --band B --n N` CLI command
- [ ] ~40 authored essay prompts committed as a pack
- [ ] Essay loop: draft capture, blind self-assessment gate, rubric critique
      template, per-dimension score storage
- [ ] Hand-enter official ELAR samples (incl. synthesis + rhetorical-revision
      items) — the calibration anchor, same role as math §6

Estimated ELAR bank: **~1,500–2,000 reading items, unlimited mechanics items
on demand, 40 essay prompts** — for one importer, one small generator module,
~16 labeling round trips, and one hand-entry session of official samples.
