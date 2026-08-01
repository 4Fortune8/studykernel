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
  storage/       SQLite, append-only activity log
products/
  tsi-ready/     Product 1: threshold objective, seeded taxonomy
  gre-forge/     Product 2 sketch: deadline(maximize), learned DAG
ingest/          importers (GSM8K, MATH, MMLU, RACE) + shared normalizer
content/         datasets and generated JSONL — gitignored
tests/           including kernel_purity_test.py
```

## Quick start

```bash
pip install -e '.[ingest,dev]'
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

## The loop

Capture (blind) → grade (deterministic) → tutor briefing out → record in.

There is no API. The briefing goes to a file, tutoring happens in whatever
chat client you like, and one structured JSON block comes back. `study record`
rejects it on `item_id` mismatch, which is what makes that safe.

## The rules this is built on

1. **What to study is goal-optimized; how to study never is.** Objectives set
   priority; pedagogy sets method and is invariant across all of them.
2. **No kernel code may name an exam.** `tests/kernel_purity_test.py` enforces
   it. It caught three violations in the first hour of this scaffold.
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

v0 scaffold. The loop runs end to end on ~8,200 imported items. What is not
built: FSRS retention, learned-edge accrual, batch labeling and the gold set,
the ELAR error-injection generator, the essay loop, `--ladder` and
`--full-sitting`, and the `maximize` objective's fragility weighting. See
DESIGN.md §16 for the v1/v2 milestones.

The failure mode this project is most exposed to is sixty hours of building
and ten of studying. Corpus engineering is more fun than studying; so is
kernel engineering. Acquisition is meant to be pull-based — triggered by the
availability report saying it is starving, not by the builder wanting to build.
