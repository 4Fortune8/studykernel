# HANDOFF.md — session state

**Written:** 2026-08-01, end of the v0 scaffold session.
**Purpose:** what a fresh session needs that the other docs do not say.

Read [DESIGN.md](DESIGN.md) for *why the system is shaped this way* — it is
the authority and this file never overrides it. Read
[README.md](README.md) for the quick start, [WEB_UI.md](WEB_UI.md) for the
front-end plan. This file covers the rest: environment quirks, the state of
the working data, decisions taken during implementation, and what is
unverified.

---

## 1. Where things stand

Repo renamed `gre-study-tool` → **`studykernel`**, both on GitHub
(`4Fortune8/studykernel`) and locally
(`/home/orangepi/code_projects/studykernel`).

Three commits this session, working tree clean, pushed:

```
80656b6  Plan the local web UI
e793210  README: document the install step the study command actually needs
36b6420  Scaffold v0: learning kernel, tsi-ready product, HF importers
```

**76 tests pass.** The loop runs end to end on 8,169 imported items.

Built: the kernel (state, objectives, allocator, pedagogy, exchange,
storage), both product packs, four importers, the CLI, and the purity test.
Not built: everything in DESIGN.md §16 v1/v2 — FSRS, learned-edge accrual,
batch labeling, the gold set, the ELAR injection generator, the essay loop,
`--ladder` / `--full-sitting`, and `maximize`'s fragility weighting.

---

## 2. Environment quirks that will bite immediately

This machine is an Orange Pi running Python **3.10.12**, and three things
here are not the defaults you would assume.

| Thing | Reality | Consequence |
|---|---|---|
| Python | 3.10, not 3.11+ | `datetime.UTC` does not exist. Use `timezone.utc`. This already bit once. |
| setuptools | was 59, upgraded to 83 | 59 predates PEP 660, so `pip install -e .` fails outright |
| git-lfs | **not installed** | LFS-pointer files look like real files at 130 bytes |
| `study` | at `~/.local/bin/study` | already on the fish PATH |
| fastapi | not installed | needed before any WEB_UI.md work starts |

Install, if the venv/package ever needs rebuilding:

```bash
python3 -m pip install --user --upgrade 'setuptools>=68'
python3 -m pip install -e '.[ingest,dev]' --no-build-isolation
```

Everything also works uninstalled as `python3 -m kernel.cli <command>`.

`--product` has **no default** — the kernel is not allowed to name a product.
Set `STUDY_PRODUCT=products/tsi-ready` or pass `--product`.

---

## 3. The data situation

The datasets were **not** downloaded when this session started, despite
appearances: `race/` and `mmlu/` contained 130-byte git-lfs pointers, and
were committed as gitlinks with no `.gitmodules`, so a clone got empty
directories. GSM8K and MATH were absent entirely.

`scripts/fetch_datasets.py` pulls all four from the HuggingFace CDN over plain
HTTPS — **no git-lfs and no `datasets` package needed**. Re-running is cheap;
it skips what is already present.

Everything lives under gitignored `content/`:

```
content/datasets/           85 MB  raw parquet (gsm8k, competition_math, race, mmlu)
content/ingest/            9.5 MB  importer output, JSONL
content/stale-lfs-pointers/       the old useless race/ and mmlu/ pointer dirs
```

A fresh clone has none of this. Rebuild with:

```bash
python3 scripts/fetch_datasets.py
for m in hf_gsm8k hf_mmlu hf_math hf_race; do python3 -m ingest.importers.$m; done
study init && for f in gsm8k mmlu competition_math race; do study ingest content/ingest/$f.jsonl; done
```

### What is in `study.db` now

| | count |
|---|---|
| items | 8,169 |
| tags / edges | 44 / 20 |
| passages | 398 |
| attempts | **1 — synthetic, see below** |

By source: `competition_math` 3,053 (redistributable=0), `gsm8k` 2,819 (=1),
`race` 1,359 (=0), `mmlu` 938 (=1). Flagged: 2,062 `coarse_tag`, 545
`nonstd_answer`.

> **Delete attempt 1 before studying for real.** It is a piped-stdin test of
> the drill loop with a nonsense rationale and a wrong answer, and it has
> already moved `algebraic-reasoning-l4` from 1500 to 1355 and written a
> diagnosis. It is real data in the log and it is fictional.
>
> Simplest fix is `rm study.db && study init && study ingest ...` — nothing
> else in there is worth keeping yet.

---

## 4. Decisions taken during implementation

These resolve or deviate from things DESIGN.md left open. Each is
load-bearing enough that a fresh session should not silently re-decide it.

1. **Glicko-2 over plain Elo** (§17 Q1, which leaned this way). The
   `rating_deviation` margin policy needs a maintained measurement
   uncertainty; plain Elo has no such quantity and the margin would have to be
   invented.
2. **`product_id`, not `exam_id`** (§15 says `exam_id`). §15 is labeled
   "deltas from v0.2", and v0.3 restructured around products rather than exam
   packs. Same key, current vocabulary.
3. **Analytic route probabilities, not Monte Carlo** (§17 Q2 prefers
   simulation, calls it v1). AND multiplies, OR is a noisy-or; the
   independence assumption is documented in `kernel/objectives/routes.py` and
   the simulation slots in behind the same interface later.
4. **`mastery.gradient` raises `NotImplementedError`** rather than guessing
   (§17 Q6: ship the interface in v0, the implementation when a use case
   exists). A plausible-looking wrong gradient would misallocate every session
   invisibly.
5. **Within a route group, `progress` takes the max, not a noisy-or.**
   Alternative routes to the same requirement read the same underlying
   ability, so combining them would inflate P(success) exactly where an honest
   number matters most.

### Three modeling fixes found by running it, not reading it

All three have regression tests. Each would have quietly broken the system:

- **Combined uncertainty collapsed.** Averaging 28 untouched tags at RD 350
  under an independence assumption gave a standard error of 66 — near
  certainty about a learner who had answered nothing. Now correlation-aware
  (ρ=0.6, `ABILITY_CORRELATION` in `kernel/state/variables.py`). Moved
  `crc_estimate` from ±10 to ±40 at zero attempts.
- **Fixed target bands caused false starvation.** A narrow band on an
  untouched tag is false precision and made `availability` report starvation
  against a corpus full of usable items. Bands now widen with RD.
- **One zeroed route group killed every gradient.** `progress` multiplies
  across groups, so the ELAR route gated on an unset essay score made the
  product exactly zero — and every numeric partial derivative with it. The
  tool reported "nothing to study" because of one missing data point.
  Threshold gradients are now taken per route, per §7.1.

The purity test also caught three violations in kernel code within the first
hour, including `--product` defaulting to `products/tsi-ready`. It works.

---

## 5. Unverified — do not treat these as settled

1. **The essay cut.** `products/tsi-ready/objective.yaml` uses `essay_score >= 5`.
   Published values run 4 vs 5 and some institutions use finer placement bands.
   DESIGN.md §12 flagged this and it is **still unchecked against the actual
   target school.** The whole ELAR route depends on it.
2. **The `crc_estimate` scale mapping is a guess.**
   `rating_range: [1200, 1750] → scale_range: [910, 990]` is a placeholder.
   Until ~40 official College Board sample items are hand-entered to anchor it
   (DATA_SOURCING_MATH.md §6), **P(pass) is ordinal, not absolute** — useful
   for ranking what to study, not for deciding you are ready.
3. **36 of 44 tags have no items.** Correct and honest, not a bug: importers
   only populate levels 2/3/4/6, ELAR mechanics tags need the injection
   generator that does not exist yet, and geometry is thin (the `[asy]` filter
   dropped 402 figure items as designed). The report's acquisition backlog
   *is* the todo list, ordered by the objective.
4. **2,062 items carry `coarse_tag`** and are awaiting the batch-labeling
   pass. Under the provenance rule they serve practice but never reinforce
   DAG edges — that gate is enforced in `kernel/pedagogy/dag.py`, not by
   convention.

---

## 6. The next thing to do

**`kernel/session.py` is extracted** (2026-08-01). `cmd_drill` is now ~60
lines of prompting with no decisions in it, and both front ends will share
`DrillSession`. The 76 existing tests pass unchanged — but note what that
does *not* prove: none of them ever touched `cmd_drill`, so "unchanged" was
always a weaker claim than it read. `tests/test_session.py` adds 28 tests that
do cover the loop, and they are what will catch drift.

Three things the service does that the CLI never did, each an invariant that
used to live in control flow:

- `Served` — the pre-answer view — **has no `answer_key` field**. WEB_UI.md
  §4.1 is enforced by the type, not by remembering.
- Phases advance in one order; out-of-order calls raise `PhaseError`.
- `min_hint_level` is the highest rung actually served, floored by the
  learner's self-report. The CLI still self-reports; a web front end serving
  rungs one at a time gets a measurement for free (§4.2).

Next: WEB_UI.md phase 1, then phase 2.

### Priority (2026-08-01)

**The essay is deferred. Math and ELAR multiple choice are the focus.** So
WEB_UI.md phase 3 moves behind phase 2, and §5 above item 1 — the unverified
essay cut — stops being urgent, because nothing depends on it until the essay
loop is built.

**This priority is currently blocked, and the block is not obvious.** The
allocator will not serve a single English item while the essay is unscored:

```
$ study next --limit 40      # essay_score = 0
  0.0001  algebraic-reasoning-l4
  ... seven math tags, no ELAR tag at all
```

`informational-analysis` has **1,359 items sitting in band** and a gradient of
exactly `0.00000000`. The corpus is not the problem; the objective is. The
ELAR route is one expression —

```yaml
- "elar_crc_estimate >= 945 AND essay_score >= 5"
```

— and an AND multiplies, so with `essay_score = 0` the partial derivative with
respect to ELAR ability is `P'(elar) × 0 = 0`. §4's per-route gradient fix
addressed one zeroed **group** killing the others; it does not reach inside a
route, where a zeroed **conjunct** does the same thing to its own partners.
Same failure mode, one level down, and still live.

Two ways out. The first was rejected: `study set essay_score 5` unblocks it
immediately and is a lie unless a real scored practice essay exists, and it
inflates P(pass), which is the one thing this tool is not allowed to do.

**Taken instead: the essay is split into its own route group**, which is what
§7.1 says groups are for — separate requirements, both of which must hold.
`products/tsi-ready/objective.yaml` now reads:

   ```yaml
   elar:
     - "elar_crc_estimate >= 945"
   essay:
     - "essay_score >= 5"
   ```

Measured before and after on the same database: **P(pass) is unchanged at
0.0%**, the essay route still reads 0% and still blocks readiness, and
`informational-analysis` returns to the top of the ranking. Only gradient
attribution changes. The essay stays a gate; it stops also being a gag on the
reading half.

Note what this cost: a product-config change, no kernel change. That is the
architecture working.

**Still unfixed, and it will recur:** the kernel takes gradients per route but
not per conjunct. Any future route of the form `A AND B` where B is an
unmeasured manual variable will silently zero A's gradient the same way. The
generalizable fix belongs in `kernel/objectives/threshold.py`; splitting the
route is the local workaround, not the cure.

---

## 7. The standing risk

Every doc in this repo names the same failure mode and it has not stopped
being true: **sixty hours of building and ten of studying.** Corpus
engineering is more fun than studying. So is kernel engineering. So is
building a web UI.

The system is usable *today* for math practice at levels 2–4. The honest next
move may be to use it for a week and let the availability report say what to
build, rather than building the next thing on the list.
