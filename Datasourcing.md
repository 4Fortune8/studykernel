# CONTENT.md — TSIA2 Corpus Acquisition & Organization

**Status:** Draft v0.1 — companion to DESIGN.md v0.3
**Scope:** `products/tsi-ready` content pipeline. GRE content strategy is
different (user-supplied only) and documented separately when needed.

---

## 1. The reframe that makes this tractable

TSIA2 has no leaked item bank, no Kaggle dataset, no ETS-style official guide
with hundreds of items. This looks like a data-scarcity problem. It is not.

TSIA2 is a **skills placement test** over completely standard content:
developmental math through college algebra, and college-level reading and
editing. It does not test TSIA2-specific knowledge; it tests whether you can
factor a quadratic, read a passage, and fix a comma splice. Therefore:

> **The corpus is the skill, not the exam.** Any well-formed item that
> exercises a blueprint skill at a known difficulty is training signal. Only a
> thin layer of official items is needed — to anchor format, style, and the
> difficulty scale.

This resolves into a two-role corpus, and the role distinction is load-bearing:

| Role | Purpose | Size | Source constraint |
|---|---|---|---|
| **`anchor`** | Format/style calibration, gold-set labeling, full-sitting simulation, Elo scale anchoring | Small (~60–120 items) | Official or state-released only |
| **`pool`** | Volume training — what the allocator serves daily | Large (target §5) | Open license, skill-aligned |

Anchors are never diluted with proxies; pools are never mistaken for the real
exam's voice. `items.role` is a schema field, and `--full-sitting` mode draws
from `role = anchor` (plus `style = native` pool items, §6) only.

---

## 2. Source tiers

### Tier 0 — Official (anchor role; local only, never in repo)

| Source | What | License posture |
|---|---|---|
| College Board TSIA2 sample question PDFs (math + ELAR) | The definitive style reference | Copyrighted; personal use; **gitignored** |
| ACCUPLACER practice app / study materials | TSIA2 runs on the ACCUPLACER platform; nearest official cousin | Same |
| WritePlacer sample essays + rubric annotations | The essay loop's ground truth | Same |

Small — likely 40–80 usable items total. That is enough for what anchors do:
gold-set seeds, style fidelity checks, and pinning the Elo scale to reality.

### Tier 1 — State-released assessment items (anchor-adjacent; the alignment gift)

**TEA's released STAAR items.** Texas accepts STAAR English III and Algebra II
EOC performance as TSIA2 *exemptions* — meaning the state itself certifies the
alignment between these instruments. TEA publicly releases retired STAAR test
forms with keys, and released state assessment items sit in the most
permissive practical category short of true open license (verify TEA's terms
of use before any *redistribution*; local use for study is unambiguous).

- Algebra I / Algebra II EOC released items → math domains
- English II / III EOC released items → ELAR reading + editing

**NAEP released items (the gem).** Federal government work → **public
domain**, full stop. Grade 12 mathematics and reading items from the NAEP
Questions Center come with **national percent-correct statistics per item** —
free, empirically grounded difficulty priors. Ingest performance data into
`items.rating` as an informed prior instead of cold-starting at the pack
default. No other source offers calibrated difficulty for free.

Also public domain, similar structure: released items from PISA and TIMSS
(check per-release terms; most are freely reusable with attribution).

### Tier 2 — Open-licensed skill corpora (pool role; bulk)

| Source | Coverage | License | Notes |
|---|---|---|---|
| **OpenStax** — Prealgebra, Elementary & Intermediate Algebra, College Algebra, Introductory Statistics | All four math domains | **CC BY** | End-of-section exercises with answers to odd-numbered problems. The backbone. |
| **WeBWorK Open Problem Library (OPL)** | Enormous college-math problem set, topic-tagged | Open (mostly GPL-ish/CC) | Machine-readable problem source (PG format); randomized parameters; an importer here yields thousands of items. Highest engineering ROI in this table. |
| **MyOpenMath** | Developmental math aligned to OER texts | Open | Overlaps OPL; useful for developmental levels 1–3 |
| **LibreTexts** math + composition | Broad | Mixed CC (per-page) | Usable but license must be checked **per page**; importer records it per item |
| **Khan Academy** | All math domains + grammar | CC BY-NC-SA | NC + SA constrains any future commercial packaging; fine for personal use and NC distribution |
| **Public-domain literature + nonfiction** (Gutenberg, federal publications) | ELAR passage bank | PD | Passages only — items must come from elsewhere or be human-authored (§4) |

### Tier 3 — Aggregator repos and prep-site scrapes: **rejected**

The reference repo examined (Samkarya/online-exam-questions) illustrates why
this tier is excluded regardless of convenience:

1. **License.** Custom non-commercial license with commercial rights reserved
   to a single company — incompatible with this project's packs.
2. **No provenance.** Nothing records where items came from or under what
   right they are reproduced. Ingesting provenance-free content imports
   unbounded legal and quality risk with no way to triage it later.
3. **AI-generated content, unmarked.** Its README instructs contributors to
   LLM-generate questions into the bank with no flag distinguishing authored
   from authentic. This violates a core principle of this project (the model
   never authors items) *and* is undetectable after the fact — the worst kind
   of corpus poisoning.

Its one useful contribution is negative: its paper-oriented organization
("NIMCET 2023" as a sequential file) is the binary curriculum in file format.
This project stores a **skill-indexed pool**; papers exist only as an
`anchor`-role construct for simulation.

---

## 3. Pipeline

```
acquire → normalize → dedupe → label → review → calibrate → serve
```

**Acquire.** One importer per source under `products/tsi-ready/importers/`.
Importers are the only source-specific code. Each emits the standard
intermediate JSON and stamps: `source`, `source_ref`, `license`,
`redistributable`, `role`, `style` (§6), and — where the source provides it —
`difficulty_prior` (NAEP percent-correct, STAAR item statistics).

**Normalize.** Math → LaTeX in stems (the reference repo's KaTeX-with-escaped-
backslashes convention is the right call and is adopted); passages →
`passages` table keyed by `passage_id`, items reference them. Items without a
verifiable answer key are **dropped at ingest** — grading is deterministic or
the item does not exist. (Concretely: OpenStax even-numbered exercises without
published answers do not enter the pool.)

**Dedupe.** Content-addressed IDs, as in DESIGN.md. Skill corpora overlap
heavily; the same factoring problem arrives from three sources.

**Label.** Batch protocol (25/prompt) against the taxonomy: domain × diagnostic
level × skill tag. Dual-label (two sessions or two models); disagreements go to
human review. Provenance rules from DESIGN.md apply unchanged: unreviewed model
labels serve practice, never reinforce DAG edges.

**Review.** Gold set of 50 hand-labeled items — seeded from Tier 0/1 anchors,
since official items are the ones whose "true" classification is least
arguable. Every labeling-prompt revision is scored against it.

**Calibrate.** Elo does the real difficulty work from attempt data. Priors:
NAEP/STAAR statistics where available; taxonomy diagnostic level maps to a
coarse prior band otherwise; high rating-deviation on everything until
attempts accumulate. Hand difficulty labels are demoted to priors everywhere.

**Serve.** The allocator's `availability` term now has teeth: when a
high-priority (domain, level) cell has no items in the target Elo band, the
report says so — and that message is this pipeline's backlog, ordered by the
objective. Content acquisition itself becomes goal-directed.

---

## 4. The ELAR gap, stated honestly

Math has an embarrassment of open riches. ELAR does not:

- **Reading comprehension items** under open license are scarce. NAEP reading
  and released STAAR English are the mainstays; OPL/OpenStax offer nothing
  here. Public-domain passages are unlimited, but passages are not items.
- **Editing/revision items** (the TSIA2 ELAR style — fix the sentence, choose
  the best revision) are scarcer still.

Mitigations, in order of preference:

1. Weight Tier 1 heavily for ELAR — released STAAR English is the volume play.
2. **Human-authored items over public-domain passages**, written by the
   operator, marked `label_source = human`, reviewed against anchor style.
   Slow, but every item is clean.
3. **Not** LLM authorship. The principle holds even here, where the temptation
   is strongest. One narrow, explicitly-marked carve-out is permitted: the
   model may *transform* (reformat, OCR-correct, convert notation) existing
   items — recorded as `model_transformed = 1` — but may not originate stem,
   options, or key. Transformation preserves provenance; authorship destroys it.

If ELAR pool depth remains thin, that is acceptable: the untimed format and
the operator's profile (strong reader, weaker on freshness of grammar
conventions) mean ELAR needs targeted drilling, not volume. Let the
allocator's availability report drive how much authoring is actually needed.

---

## 5. Coverage targets

The allocator needs items where the learner is, not everywhere equally. Build
against a **coverage matrix**, fill lazily:

- Math: 4 domains × 6 diagnostic levels = 24 cells. Initial target **~40
  items/cell mid-levels (3–5), ~20 at the extremes** — the CAT and the learner
  both live mid-scale. ≈ 700–800 math items; OpenStax + OPL clear this
  comfortably.
- ELAR: reading (by passage genre × question type) and editing (by convention
  tag). Initial target ~250 items total, Tier-1-heavy.
- Essay: 10–15 prompts + rubric + any scored official samples. Volume is
  irrelevant; the loop is prompt → draft → rubric-anchored critique → revise.

Stop acquiring when `availability` stops flagging. Corpus building is subject
to the same rule as everything else here: **a satisfied objective reads zero.**
An over-built corpus is procrastination with extra steps.

---

## 6. Style fidelity

Proxy items train the skill but not the exam's voice. One more schema field:

- `style = native` — official TSIA2/ACCUPLACER items, or items reviewed as
  matching TSIA2 format conventions (single-answer MC, characteristic stem
  phrasing, embedded-calculator assumptions on the subset of math items that
  provide one)
- `style = proxy` — everything else, however skill-aligned

Daily drilling is style-agnostic (the skill is the point; the exam is untimed
so format speed matters little). `--full-sitting` simulation draws exclusively
from `style ∈ {native}` ∪ anchors, so the dress rehearsal sounds like the exam.

Schema deltas from this document: `items.role`, `items.style`,
`items.model_transformed`, `items.difficulty_prior_src`, and the `passages`
table. All additive.

---

## 7. Sequenced plan (fits inside the v0/v1 milestones)

1. **Weekend of v0:** hand-ingest Tier 0 samples + one OpenStax algebra
   chapter via the YAML path (no importer yet). Enough to run the loop.
2. **v1, first:** OpenStax importer (PDF/CNX extraction; odd-answers only),
   then the STAAR importer (PDF + key). ~500 math items.
3. **v1, second:** NAEP importer with difficulty priors; gold set to 50; batch
   labeling pass over everything ingested.
4. **v1, third:** OPL importer (PG format parser — the biggest single
   engineering line item in this file, and worth it: it is the difference
   between hundreds and thousands of math items).
5. **Ongoing, allocator-driven:** fill cells the availability report flags;
   author ELAR editing items as gaps demand.

The trap in this file is the same trap as everywhere else: corpus engineering
is more fun than studying. Steps 1–2 unblock real studying; everything after
is pull-based, triggered by the allocator saying it is starving — not by the
builder wanting to build.
ENDOFDOC
echo "written: $(wc -l < /mnt/user-data/outputs/CONTENT.md) lines"
