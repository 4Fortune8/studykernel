# DATA_SOURCING_MATH.md — HF Dataset Ingestion for the TSIA2 Math Bank

**Status:** Draft v0.1
**Scope:** Three Hugging Face datasets → `items` table for the `tsi-ready` math bank.
**Companion to:** DESIGN.md §Part IV (tsi-ready), §11 (content pipeline in the v0.2 lineage).

Sources covered:

| Source | HF path | Rows | License | `redistributable` |
|---|---|---|---|---|
| GSM8K | `openai/gsm8k` (config `main`) | 7,473 train + 1,319 test | MIT | 1 |
| MATH | `dim/competition_math` | 7,500 (train only) | Mirror; original is MIT-licensed code over scraped competition content | 0 |
| MMLU | `cais/mmlu` (3 subsets) | ~860 usable | MIT | 1 |

Note on `dim/competition_math`: it is a community mirror of the Hendrycks MATH
dataset (the canonical repo has had availability problems). Field layout verified
2026-07-30: `problem` (LaTeX string), `level` ("Level 1"…"Level 5" + junk class),
`type` (7 subjects), `solution` (worked solution, final answer in `\boxed{}`).
Underlying content is scraped from math competitions (AoPS et al.), so it is
marked `redistributable = 0` regardless of the mirror's presentation. GSM8K and
MMLU are MIT and safe to bundle.

---

## 1. Target format

Every importer emits the standard intermediate record (one JSONL line per item):

```json
{
  "source":        "gsm8k",
  "source_ref":    "train/4211",
  "section":       "math",
  "item_type":     "numeric",
  "stem":          "...",
  "choices":       null,
  "answer_key":    "45",
  "official_expl": "...",
  "difficulty_prior": 1200,
  "tags":          [{"slug": "quantitative-reasoning", "label_source": "imported"}],
  "license":       "MIT",
  "redistributable": 1,
  "flags":         []
}
```

`item_id = sha256("tsia2" + normalized_stem)[:16]` is computed at DB insert, after
normalization (§5), which is what makes cross-source dedupe work.

---

## 2. GSM8K — `openai/gsm8k`

**Role:** bulk supply for **Quantitative Reasoning** — multi-step arithmetic word
problems (rates, money, measurement, proportional scaling). This is the closest
style match to TSIA2 quantitative items of any public dataset.

```python
from datasets import load_dataset
ds = load_dataset("openai/gsm8k", "main")   # splits: train, test
```

| Field | Maps to |
|---|---|
| `question` | `stem` |
| `answer` (reasoning + `#### N`) | `official_expl` = text before `####`; `answer_key` = text after `####` |

**Rules:**
- `answer_key`: split on `####`, strip whitespace and thousands-separator commas
  (`"1,200"` → `"1200"`). Final answers are integers throughout, so grading is a
  clean string/numeric compare — no flags needed.
- `item_type = numeric`. All items → tag `quantitative-reasoning`,
  `label_source = imported` (the dataset is single-domain by construction; no
  labeling pass needed).
- `difficulty_prior`: one band, low-middle (grade-school ceiling ≈ TSIA2
  diagnostic levels 2–4). Elo will spread them out; the prior just seeds.
- **Cap the ingest.** ~8.8K items would swamp the bank and skew the allocator's
  `availability` term toward one domain. Ingest the full test split (1,319 —
  slightly cleaner) plus a random 1,500 from train, seeded for reproducibility.
  More can be pulled later if the band runs dry.

**Yield: ~2,800 quantitative-reasoning items, zero manual work.**

---

## 3. MATH — `dim/competition_math`

**Role:** the only source that covers **all four domains** with per-item subject
and difficulty labels already attached. Also the top-band supply (§3.4).

```python
ds = load_dataset("dim/competition_math")   # split: train only
```

### 3.1 Subject → domain mapping

| `type` value | Disposition |
|---|---|
| `Prealgebra` | → `quantitative-reasoning` |
| `Algebra` | → `algebraic-reasoning` |
| `Geometry` | → `geometric-spatial-reasoning` |
| `Counting & Probability` | → `probabilistic-statistical-reasoning` |
| `Intermediate Algebra` | → `algebraic-reasoning`, top band only (§3.4) |
| `Number Theory` | **drop** — not on the TSIA2 blueprint |
| `Precalculus` | **drop** — past the exam ceiling |

`label_source = imported` (the subject label is part of the dataset).

### 3.2 Level filter

Competition "Level 1" ≈ TSIA2 mid-band; the scale climbs fast.

| `level` | Disposition | `difficulty_prior` |
|---|---|---|
| Level 1 | in | mid |
| Level 2 | in | mid-high |
| Level 3 | in | high |
| Level 4 | Algebra + Intermediate Algebra only, `flags += ["top_band"]` | top |
| Level 5 | **drop** | — |
| anything else (the field has a junk class) | **drop** | — |

Level 3 is genuinely on-blueprint — spot checks show rationalizing denominators,
arithmetic sequences, quadratic manipulation, completing the square — i.e. the
college-algebra end where the 950 threshold and diagnostic level 6 live. Level 5
is unambiguously past it.

### 3.3 The `[asy]` filter — the big one

A substantial share of MATH problems (Geometry especially) embed **Asymptote
diagram code** — `[asy] ... [/asy]` blocks — and the problem is unsolvable
without the rendered figure. The pipeline does not render Asymptote.

**Rule: drop any item whose `problem` or `solution` contains `[asy]`.** Do not
flag-and-review; the review cost exceeds the item value when other sources exist.

Consequence: this guts the Geometry subset (figure-heavy by nature) — expect to
keep well under half of it. Accepted; see §7 (gaps).

### 3.4 Answer extraction from `\boxed{}`

`answer_key` = contents of the **last** `\boxed{...}` in `solution`, extracted
with a brace-matching scan (regex fails on nested braces — `\boxed{\frac{2}{3}}`
is common).

Keep-vs-flag rule, applied to the extracted answer:

| Extracted answer looks like | Disposition |
|---|---|
| Integer / decimal (`17`, `6.125`, `-80`) | keep, `item_type = numeric` |
| Simple fraction (`\frac{20}{3}`) | keep; normalize to `20/3` |
| Ordered pair, interval, list, expression, units-in-answer (`(3,-1)`, `2, 3, and 4`, `x = 2`, `5-12i`) | `flags += ["nonstd_answer"]` — held out of serving until reviewed or dropped |
| No `\boxed{}` found | `flags += ["no_answer"]` — drop |

Deterministic grading is the system's spine; anything that would need fuzzy
answer matching is not worth it at this corpus size. Expect roughly 60–70% of
surviving items to have clean numeric/fraction answers.

### 3.5 Provenance and licensing

`redistributable = 0`, `license = "scraped-competition"`. Fine for the local
project; excluded automatically from any future shared pack by the existing
`redistributable` query. This is the one source of the three that can never ship.

**Yield after all filters: roughly 1,500–2,200 items** — order-of-magnitude
estimate; the pipeline should print actual per-domain counts at ingest.
Approximate shape: Algebra ~600–800, Prealgebra ~400–500, C&P ~300–400,
Geometry ~150–300 (asy-decimated), Intermediate Algebra top band ~100–200.

---

## 4. MMLU — `cais/mmlu`

**Role:** the only **multiple-choice** source of the three — matching the actual
TSIA2 item format — plus direct coverage of statistics.

```python
for subset in ["elementary_mathematics", "high_school_mathematics",
               "high_school_statistics"]:
    ds = load_dataset("cais/mmlu", subset)   # splits: test, validation, dev
```

Use `test` + `validation` (dev is 5 items/subset, skip). Roughly:
`elementary_mathematics` ~420, `high_school_mathematics` ~300,
`high_school_statistics` ~240.

| Field | Maps to |
|---|---|
| `question` | `stem` |
| `choices` (list of 4) | `choices_json` |
| `answer` (int 0–3) | `answer_key` = `"A"|"B"|"C"|"D"` |

**Tagging:**
- `high_school_statistics` → `probabilistic-statistical-reasoning`,
  `label_source = imported`. Single-domain; no pass needed.
- `elementary_mathematics` and `high_school_mathematics` are **mixed-domain**
  (arithmetic, algebra, geometry, probability interleaved). These two subsets are
  the one place in this pipeline that needs the **batch-label protocol**
  (DESIGN.md lineage §11.2): ~30 round trips at batch 25,
  `label_source = model`, unreviewed until spot-checked. They still serve
  practice immediately under the provenance rule — model labels serve, they just
  don't reinforce DAG edges.
- Interim shortcut if the labeling pass is deferred: tag the whole of
  `elementary_mathematics` as `quantitative-reasoning` with
  `flags += ["coarse_tag"]`. Wrong for maybe a third of items, but it makes them
  servable on day one and the flag marks them for the real pass.

**Difficulty priors:** elementary → low; high_school_math → mid-high;
high_school_statistics → mid.

**Known hazards, accepted:**
- MMLU has a documented low-single-digit answer-key error rate. Detection path
  already exists: a graded "miss" where the tutoring exchange concludes the key
  is wrong → `flags += ["disputed_key"]` via a field in the return payload.
  Cheap to add, worth it for this source.
- A few items reference figures or "the following" content that isn't present.
  Drop any stem matching `figure|graph below|shown below` at ingest.

**Yield: ~860 MCQ items, ~30 paste round-trips of labeling for two subsets.**

---

## 5. Normalization (shared, order matters)

Applied to every stem before hashing and storage:

1. **Whitespace:** collapse runs, strip ends, normalize line endings.
2. **LaTeX is stored raw.** MATH stems are LaTeX-dense; do not strip it — it is
   the ground truth and a future web UI renders it (KaTeX). The CLI displays raw
   LaTeX for now; readable enough for algebra, and a known cosmetic cost.
3. **Answer normalization (grading side, not storage side):** strip `$`,
   surrounding whitespace, thousands commas; `\frac{a}{b}` → `a/b`; `\dfrac`
   likewise. Grading compares normalized-to-normalized. GSM8K needs almost none
   of this; MATH needs all of it.
4. **Hash for `item_id`:** lowercase, all whitespace removed, computed after
   step 1–2. This catches the classic word-problem reuse across sources
   (identical items circulate between question banks with whitespace variance).
5. **Dedupe rule:** identical `item_id` from two sources → keep the first-loaded,
   log the collision. Expect a nonzero GSM8K/MMLU-elementary overlap.

---

## 6. Ingest order and Elo calibration

Load order is not arbitrary — priors calibrate against each other:

1. **Official College Board samples** (hand-entered, ~40 items) — the anchor.
   These are the only items whose difficulty placement is known-authentic.
2. **GSM8K** — establishes the low-mid band.
3. **MMLU** — brackets it (elementary below, hs-math above).
4. **MATH** — mid through top band.

All priors are just starting positions; a few hundred attempts of Glicko-2
updates will dominate them. The reason to seed bands at all is the allocator's
first week — before attempt data exists, band targeting runs entirely on priors.

---

## 7. Coverage after this pipeline — and what it does not solve

| TSIA2 domain | Supply | Verdict |
|---|---|---|
| Quantitative Reasoning | GSM8K (~2,800) + MATH Prealgebra + MMLU elementary | **Oversupplied** |
| Algebraic Reasoning | MATH Algebra + Int. Algebra + MMLU hs-math slice | **Good, incl. top band** |
| Probabilistic & Statistical | MATH C&P + MMLU hs-statistics | **Adequate** |
| Geometric & Spatial | MATH Geometry post-asy + MMLU slices | **Thin, and text-only** |

Three honest gaps, none solved by these datasets:

1. **Geometry with figures.** Everything surviving the pipeline is text-only,
   while real TSIA2 geometry items lean on figures. Text-only practice covers
   formulas and coordinate geometry but not figure reading. Mitigation is the
   already-planned non-HF sources: OpenStax geometry sections and the official
   sample PDF (which includes figure items — hand-enter those). Do not try to
   fix this by rendering Asymptote; that is a rabbit hole with a weekend-sized
   floor.
2. **Multipart items.** TSIA2 uses connected question sets (one scenario, parts
   A/B/C building on each other). No public dataset has this structure. The
   `passage_id` field already in the schema supports grouping if items are ever
   authored or hand-entered for it; out of scope for ingestion.
3. **ELAR.** Entirely untouched by this document — these are math datasets.
   ELAR sourcing (official samples + college packets) is its own effort.

Also worth saying plainly: MATH items have a competition *flavor* (trick-aware,
proof-adjacent phrasing) that TSIA2 does not. At Levels 1–3 this mostly washes
out, and the skills transfer; but the official samples and college packets stay
in the mix precisely so the learner's calibration to *exam style* comes from
exam-style items. The HF corpus builds skill; the authentic corpus builds
familiarity. Both are needed and neither substitutes for the other.

---

## 8. Implementation checklist

- [ ] `ingest/importers/hf_gsm8k.py` — split/cap/seed logic, `####` parse
- [ ] `ingest/importers/hf_math.py` — subject map, level filter, asy drop,
      brace-matching `\boxed{}` extractor, answer classifier (keep/flag)
- [ ] `ingest/importers/hf_mmlu.py` — subset loop, index→letter, figure-ref drop
- [ ] Shared: normalizer, hasher, dedupe logger, JSONL writer
- [ ] Ingest report: per-source × per-domain × per-band counts, drop/flag tallies
- [ ] `disputed_key` field added to the tutoring return payload
- [ ] Batch-label run over the two mixed MMLU subsets (~30 round trips)
- [ ] Spot-check: 20 random MATH extractions eyeballed against solutions
      (the `\boxed{}` extractor is the likeliest silent-failure point)

Estimated bank after this document is executed: **~4,000–5,500 math items across
all four domains with priors**, at the cost of three importer scripts, ~30
labeling round trips, and one 20-item spot check.
