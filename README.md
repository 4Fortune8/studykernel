# studykernel

A learning kernel and the study products built on it.

**The claim:** studying is a resource-allocation problem. Model the learner's
state, define the goal as an objective over that state, and allocate the next
unit of effort where the objective's gradient is steepest.

A curriculum is what you build when you have no objective — the only ordering
left is someone else's chapter sequence. Once the objective is explicit, the
ordering falls out of it, personalized for free. See [DESIGN.md](DESIGN.md).

## Layout

```
kernel/          installable package; contains no exam names (CI-enforced)
  state/         Glicko-2 ratings, reliability CIs, variance
  objectives/    threshold, maximize, mastery, deadline + route expressions
  allocator.py   priority = gradient x learnability x availability
  pedagogy/      hint ladder, capture, explain-back gate, prerequisite DAG
  exchange/      briefing out, structured record in (no API required)
  session.py     the drill loop, headless; front ends are adapters over it
  storage/       SQLite, append-only activity log
  cli.py         the terminal adapter
web/             the local web adapter (FastAPI); same purity rule as kernel/
  app.py         routes + the single-worker entry point
  mathtext.py    which of a corpus's many shapes is actually mathematics
  templates/     Jinja2, drill/ holds the three phases as partials
  static/        one CSS file + vendored htmx and KaTeX (offline, no CDN)
products/
  tsi-ready/     Product 1: threshold objective, seeded taxonomy
  gre-forge/     Product 2 sketch: deadline(maximize), learned DAG
ingest/          importers (GSM8K, MATH, MMLU, RACE) + shared normalizer
content/         datasets and generated JSONL — gitignored
tests/           including kernel_purity_test.py
```

## Quick start

`study` is a package entry point — it does not exist until the package is
installed. Editable installs need PEP 660 support (setuptools 64+), and the
system setuptools on some distributions is older than that:

```bash
python3 -m pip install --user --upgrade 'setuptools>=68'
python3 -m pip install -e '.[ingest,dev]' --no-build-isolation
```

Three optional extras, kept separate so the study loop never depends on any of
them resolving: `ingest` (importers), `dev` (pytest), `web` (the browser UI).

If `study` is still not found afterwards, `~/.local/bin` is not on your PATH.
Every command below also works without installing anything, as
`python3 -m kernel.cli <command>`.

```bash
export STUDY_PRODUCT=products/tsi-ready

python scripts/fetch_datasets.py          # ~89 MB from HuggingFace
python -m ingest.importers.hf_gsm8k       # and hf_mmlu, hf_math, hf_race

study init
study ingest content/ingest/gsm8k.jsonl
study report                              # position, routes, backlog
study drill                               # one item through the loop
study record <attempt_id>                 # paste the returned JSON block
```

`--product` has no default: the kernel is not allowed to know which products
exist. Set `STUDY_PRODUCT` or pass the flag.

## Profiles

Two people can share one install. A profile is a `learner_id` and a name —
there is no login and there are no passwords, because this runs on localhost
for whoever is sitting at it. Ratings, attempts and objective position are all
keyed by learner, so profiles do not mix.

```bash
study profile add alex --name "Alex"
study profile                             # list; * marks the active one
study --learner alex drill                # or export STUDY_LEARNER=alex
```

In the browser the profile lives in the database and the *selection* lives in
a cookie, so two people at two devices can use the same server at once without
switching each other.

## Web UI

```bash
python3 -m pip install -e '.[web]' --no-build-isolation
STUDY_PRODUCT=products/tsi-ready study-web        # http://127.0.0.1:8000
```

**Now** answers one question — what to work on, and why (gradient,
learnability and availability shown separately, because they fail
differently). Three states, and the one that matters is *satisfied*: a
full-page stop with the start control removed, not disabled.

**Drill** runs the whole loop in the browser: passage pinned beside the item,
blind capture, hint rungs one request at a time, server-side grading, the
explain-back gate, then the briefing and paste-back panel with inline
validation. Phase lives on the server, so refreshing redraws where you are
instead of replaying a step.

**History** is the past-questions table: every item answered, grouped by
category with accuracy and mean hint level, filterable by outcome and by
whether the tutoring exchange happened.

Math renders through bundled KaTeX — vendored, woff2 only, no network at
runtime. MMLU stores some choices as bare `\frac{7}{9}` with no delimiters, so
those are wrapped at render time; nothing stored is rewritten.

Still missing: the `Report` page, the last item in WEB_UI.md phase 1.

## The exchange is optional; the gate is not

Two different steps get confused, so: the **explain-back gate** is mandatory
and has no skip anywhere in either front end. The **tutoring exchange** — copy
briefing, paste into a chat client, paste the JSON back — is optional, and
DESIGN.md §10 always said so: *no API is required for the system to work.*

So a drill you got right can end at the verdict. Skip the exchange and the
attempt, the capture, the answer and the briefing are all kept; `History`
lists what is still open and hands the briefing back whenever you want it.
Recording a diagnosis later supersedes the skip.

```bash
study history                      # by category, then every attempt
study history --state open         # the come-back-to-it list
study history --tag algebra --outcome wrong
study record <attempt_id>          # finish any of them, whenever
```

`study-web` runs a single uvicorn worker and there is no flag to change that.
In-progress drills live in an in-process dict, so a second worker would fail
to find roughly half of them and lose blind captures. The entry point passes
uvicorn the app *object* rather than an import string, which is what makes
forking workers unavailable rather than merely discouraged.

## The loop

Capture (blind) → grade (deterministic) → tutor briefing out → record in.

There is no API. The briefing goes to a file, tutoring happens in whatever
chat client you like, and one structured JSON block comes back. `study record`
rejects it on `item_id` mismatch, which is what makes that safe.

The loop lives in `kernel/session.py`, not in the CLI. `DrillSession` owns the
sequence and the invariants that go with it — the answer key is not in the
pre-answer view at all, phases only advance in one order, and the explain-back
gate has no skip path — so a second front end inherits them rather than
reimplementing them. `cli.py` prompts and prints; it decides nothing.

## The rules this is built on

1. **What to study is goal-optimized; how to study never is.** Objectives set
   priority; pedagogy sets method and is invariant across all of them.
2. **No kernel or front-end code may name an exam.**
   `tests/kernel_purity_test.py` scans `kernel/` and `web/` and enforces it.
   Three violations in the first hour of the scaffold; one more the minute the
   scan was extended to `web/`.
3. **One allocator, plural objectives.** Threshold logic never leaks into the
   scheduler; scheduler logic never leaks into an objective.
4. **The model never grades; the model never authors items.**
5. **Mastery lives on reliability lower bounds, not point estimates.**
6. **Structured output or it didn't happen.**
7. **Label provenance gates the DAG.** Unreviewed model labels serve practice;
   they never restructure the study plan.
8. **Content enters version control only with a redistribution license.**
9. **A satisfied objective reads zero.** The tool must be able to say *stop
   studying*. One that cannot is an engagement product.
10. **Friction on the diagnostic loop is fatal** — the explain-back gate is the
    sole exception, because there the friction *is* the mechanism.

## Status

v0 scaffold. The loop runs end to end on ~8,200 imported items in both the
terminal and the browser, 194 tests pass, and the drill loop lives in
`kernel/session.py` with both front ends as adapters over it
([WEB_UI.md](WEB_UI.md)).

Current focus is math and ELAR multiple choice; the essay loop is deferred.
What is not built: FSRS retention, learned-edge accrual, batch labeling and
the gold set, the ELAR error-injection generator, the essay loop, `--ladder`
and `--full-sitting`, and the `maximize` objective's fragility weighting. See
DESIGN.md §16 for the v1/v2 milestones and [HANDOFF.md](HANDOFF.md) for what
is unverified.

Two numbers in `study report` are not yet trustworthy as absolutes: the
`crc_estimate` scale mapping is an unanchored placeholder, so P(pass) is
ordinal rather than absolute, and the essay cut is unverified against the
actual target institution. Both are useful for ranking what to study next,
which is what the tool is for.

The failure mode this project is most exposed to is sixty hours of building
and ten of studying. Corpus engineering is more fun than studying; so is
kernel engineering. Acquisition is meant to be pull-based — triggered by the
availability report saying it is starving, not by the builder wanting to build.
