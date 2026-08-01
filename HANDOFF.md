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
| pip | was 22.0.2 (system), upgraded to **26.2** in `~/.local` | 22.0.2 has no `--dry-run`, so you cannot check a resolution without performing it |
| git-lfs | **not installed** | LFS-pointer files look like real files at 130 bytes |
| `study` | at `~/.local/bin/study` | already on the fish PATH |
| fastapi | not installed | needed before any WEB_UI.md work starts |

The web dependencies **do** resolve to prebuilt aarch64 wheels on this box —
checked with `pip install --dry-run fastapi uvicorn jinja2`, which pulls
`pydantic_core-2.46.4-cp310-cp310-manylinux_2_17_aarch64.whl`. No Rust
toolchain, no source build. That was the real risk on an ARM SBC, and it is
not one.

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

### The web package exists (shell only)

`web/` is a real package in the wheel — `study-web` console script, FastAPI
app, `deps.py`, templates, one CSS file. What works: profile switching. What
does not exist yet: `Now`, the drill flow, `Report`. The `[web]` extra
resolves to prebuilt aarch64 wheels on this machine (see §2).

Three decisions baked in, each answering a trap named before it was built:

1. **Single worker, structurally.** `study-web` calls `uvicorn.run(app, ...)`
   with the app *object*. Uvicorn can only fork workers from an import string,
   so `--workers 2` is not available rather than merely inadvisable. Under two
   workers a drill started on one is `UnknownDrill` on the other about half
   the time, and the learner loses a blind capture they cannot honestly
   rewrite.
2. **A lost drill is a page, not a traceback.** `UnknownDrill` has a handler
   returning 410 and `drill_lost.html`, which says that nothing was recorded.
   Restart-mid-drill happens with one worker too.
3. **Drills expire.** `DrillStore` sweeps on `put` with a 6-hour idle TTL,
   refreshed on every access. Abandonment is the normal way a drill ends —
   closing a tab — and without expiry the store grew for the life of the
   process, holding an item row and its passage per entry.

The purity test now scans `web/` as well as `kernel/`, on the grounds that a
front end hardcoding one product is the same fork risk one layer out. It
caught a violation in `web/deps.py` within a minute of being extended: an
error message naming a product pack in its example. It keeps earning its keep.

### Profiles

Two people can share the install. `learners` gained a nullable `display_name`;
everything else was already keyed by `learner_id`, so no other schema changed.

- CLI: `study profile add <id> --name "..."`, `study profile`, `--learner`.
- Web: profile in the database, **selection in a cookie**. A server-side
  "current profile" would mean two devices could not be used at once and
  switching on one would silently move the other.
- No credentials anywhere, by design. Anyone at the machine can switch to any
  profile. It separates data; it does not protect it.

`db.migrate()` gained an additive `ALTER TABLE` pass (`LATE_COLUMNS`), because
`CREATE TABLE IF NOT EXISTS` is a no-op on a live table and a new column would
otherwise never reach an existing `study.db`. Verified on a copy of the real
database: column added, 8,169 items and the attempt log untouched.

### `recommend()` and the `Now` page

`DrillSession.recommend()` exists and `start()` is built on it. The split is
the point: a home page renders on every visit, and if asking *what should I
study* also minted a token and reserved an item, refreshing would churn item
selection and fill the store with drills nobody opened. `recommend()` returns
`Recommendation | Satisfied | Starved` and reserves nothing; `start()` is that
plus the commitment.

`Recommendation` carries gradient, learnability and availability separately
rather than pre-multiplied, because they fail differently and the difference
*is* the diagnosis: low gradient means the objective does not care, low
learnability means wrong difficulty, low availability means the corpus cannot
serve it. `Now` shows all three under "Why this".

`DrillSession.position()` returns the `ObjectiveReport` and — unlike
`study report` — does **not** snapshot to `objective_state`. A page that
renders on every visit must not write a position sample each time, or the
trajectory chart in phase 4 ends up measuring how often a tab was opened.
There is a test for that.

All three states of `Now` were driven over HTTP, not just unit-tested:

| State | Verified |
|---|---|
| servable | recommendation, the three multiplicands, expected hit rate, position |
| satisfied | full-page stop; **zero** start controls and **no** position readout |
| starved | corpus-problem message pointing at the acquisition backlog |

The satisfied page deliberately omits the position table. The answer there is
*stop*, and a progress readout underneath it is an invitation to one more
session — §6's whole point.

**The start control is currently the terminal command** — `Now` prints
`study drill --tag <slug>` rather than a button, because the browser drill
flow does not exist yet. That is honest and useful today: the page decides
what to study, the terminal does it. It becomes a real button in §5.2.

### The drill flow

Built, and the whole loop runs in the browser: present → blind capture →
answer → verdict → gate → briefing → paste → diagnosis. HTMX is vendored at
`web/static/vendor/htmx.min.js` (51 KB, no build step, works offline).

`DrillSession.view(token)` was the piece this needed. Phase lives on the
server, so `GET /drill/{token}` re-renders wherever the drill actually is —
refresh, back button and double-submit all redraw the truth instead of
replaying a step. The browser holds a token and nothing else.

`tests/test_web.py` drives all of it over real HTTP via `TestClient`. The
§4.1 leak test WEB_UI.md asks for is there, and it caught a mistake worth
recording: the first version of it ran against real RACE data whose key was
the single letter `D`, which matches inside `<div>`. It reported a leak that
was not one. The suite version uses a distinctive key (`quokka-7`) and strips
the rendered choice list — where the keyed string legitimately appears as one
option among four — before asserting the key is absent from everything else.
**A leak test on a one-character key is worthless; it will pass on noise or
fail on it.**

Two behaviours worth knowing:

- **`min_hint_level` is now measured, not reported.** Requesting L1 in the
  browser and then answering yields `L1` with no self-report anywhere — the
  CLI's "lowest hint level you needed" prompt has no equivalent here and does
  not need one. Confirmed end to end on a copy of the real database.
- **`record_for(token, …)`** replaces `record(attempt_id, …)` for the web,
  which takes the attempt id from the drill rather than the form and refuses
  a second diagnosis for one attempt. `finish()` is deliberately *not* called
  after recording: dropping the token made a refresh render "nothing was
  recorded" when the attempt was in fact in the log. Expiry reclaims it.

The web app now runs `db.migrate()` at startup. Only `study init` called it
before, so an older database reached the web layer missing `display_name` and
every page died on a raw sqlite error — which is exactly what happened the
first time it was pointed at a copy of the real `study.db`.

> **Run `study init` once against your real database** (or just start the web
> app) if you have not since the profiles change. Additive and idempotent.

### KaTeX is bundled

KaTeX 0.16.22 under `web/static/vendor/katex/` — 604 KB, **woff2 fonts only**
(20 files), no network at runtime. The `.woff` and `.ttf` fallbacks were
stripped from the stylesheet rather than shipped: every browser that will ever
open this supports woff2, and a `url()` pointing at a file that is not in the
wheel is a 404 waiting for whoever reads the network tab. Checked: all 20
fonts the CSS references are present, and nothing else is referenced.

**The corpus needed two different treatments, which is the non-obvious part.**

| Source | How LaTeX is stored | Handled by |
|---|---|---|
| MATH, GSM8K stems | `$…$` (349 of a 400 sample), `$$…$$`, `\[…\]` | auto-render |
| MMLU choices | **bare** — `\frac{7}{9}`, no delimiter at all (179 of 9,188) | `web/mathtext.py` |
| RACE choices | prose, no LaTeX | left alone |

auto-render cannot find what is not marked, so bare expressions are wrapped in
`$…$` at render time by `mathtext.delimit`. Nothing stored changes —
DATA_SOURCING_MATH.md §5.2's rule that items keep the source's bytes still
holds, and grading still reads the raw value.

The wrapping rule is deliberately conservative, because the two failures are
not symmetric: an unrendered fraction is ugly, and a *sentence* rendered as
mathematics is unanswerable. So it wraps only strings that contain a LaTeX
command and, once commands are removed, contain no three-letter word.
`tests/test_mathtext.py` is mostly the cases it must decline.

Rendering is scoped to `.math` elements, never the document, because the
briefing `<pre>` is copied verbatim into a chat client — rendering it would
put HTML on the clipboard instead of the prompt. It re-runs on
`htmx:afterSwap`, since swapped-in panels have never been through it.

Verified three ways, because "does the maths render" is not something a unit
test can answer:

1. `katex.renderToString` in node against seven real corpus expressions —
   7/7, and a deliberately malformed one degrades to error-coloured source
   instead of throwing (`throwOnError: false`).
2. Headless chromium over five real MATH stems and the MMLU bare fractions:
   **11 rendered spans, 0 errors**, every leftover `\frac` accounted for
   inside KaTeX's own TeX annotation, prose choice untouched, briefing `<pre>`
   left raw.
3. Server log during that render: **zero 404s**, three woff2 fonts fetched,
   no `.woff` or `.ttf` requested.

Next: the `Report` page, the last item in phase 1.

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
