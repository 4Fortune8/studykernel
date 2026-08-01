"""MATH (Hendrycks) importer via the `dim/competition_math` mirror.
DATA_SOURCING_MATH.md §3.

The only source covering all four domains with per-item subject and difficulty
labels already attached, and the only top-band supply.

`redistributable = 0`, unconditionally. The mirror's presentation does not
change the fact that the underlying content is scraped from math competitions,
so this is the one source of the four that can never ship in a public pack.
The existing `redistributable` query excludes it automatically.

Two filters do the heavy lifting and both are lossy on purpose:

- **Level 5 is dropped** and Level 4 kept only for algebra. Competition
  "Level 1" is already TSIA2 mid-band and the scale climbs fast; Level 5 is
  unambiguously past the exam ceiling.
- **`[asy]` items are dropped outright**, not flagged for review. A
  substantial share of Geometry problems embed Asymptote diagram code and are
  unsolvable without the rendered figure. The review cost exceeds the item
  value when other sources exist. This guts the Geometry subset -- expect to
  keep well under half of it -- which is an accepted, documented gap (§7),
  not a silent one.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

import pyarrow.parquet as pq

from ingest import normalize

# A fraction with no nested braces in either part -- the grading normalizer
# turns these into a/b and compares numerically.
_SIMPLE_FRACTION = re.compile(r"-?\\[dt]?frac\{[^{}]+\}\{[^{}]+\}")

SOURCE = "competition_math"
LICENSE = "scraped-competition"

# type -> (tag slug, top_band_only)
SUBJECT_MAP: dict[str, tuple[str, bool]] = {
    "Prealgebra": ("quantitative-reasoning-l4", False),
    "Algebra": ("algebraic-reasoning-l4", False),
    "Geometry": ("geometric-spatial-reasoning-l4", False),
    "Counting & Probability": ("probabilistic-statistical-reasoning-l4", False),
    "Intermediate Algebra": ("algebraic-reasoning-l6", True),
    # Not on the blueprint / past the ceiling.
    "Number Theory": ("", False),
    "Precalculus": ("", False),
}

LEVEL_BANDS = {
    "Level 1": "mid",
    "Level 2": "mid_high",
    "Level 3": "high",
    "Level 4": "top",
}

TOP_BAND_SUBJECTS = {"Algebra", "Intermediate Algebra"}


def extract_boxed(solution: str) -> str | None:
    """Contents of the **last** `\\boxed{...}`, by brace-matching scan.

    A regex fails here: `\\boxed{\\frac{2}{3}}` is common and nested braces
    are the norm, not the exception. This is the likeliest silent-failure
    point in the whole pipeline, which is why §8 asks for a 20-item spot check
    against the solutions after every change to it.
    """
    marker = "\\boxed{"
    start = solution.rfind(marker)
    if start == -1:
        return None

    i = start + len(marker)
    depth = 1
    out: list[str] = []
    while i < len(solution) and depth > 0:
        ch = solution[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                break
        out.append(ch)
        i += 1

    return "".join(out).strip() if depth == 0 else None


def classify_answer(answer: str) -> tuple[str, list[str]]:
    """(item_type, flags). Anything needing fuzzy matching is held out.

    Deterministic grading is the system's spine. At this corpus size an item
    that would need approximate answer matching is not worth the risk of a
    wrong "wrong".
    """
    cleaned = answer.replace("$", "").replace("\\!", "").strip()

    if _is_plain_number(cleaned):
        return "numeric", []
    if _SIMPLE_FRACTION.fullmatch(cleaned):
        # `\frac{20}{3}` is graded fine -- kernel.pedagogy.grading normalizes
        # it to 20/3. Only *nested* fractions are held out.
        return "numeric", []
    return "numeric", ["nonstd_answer"]


def _is_plain_number(text: str) -> bool:
    try:
        float(text.replace(",", ""))
    except ValueError:
        return False
    return True


def load(data_dir: Path) -> list[dict]:
    report = normalize.IngestReport(SOURCE)
    records: list[dict] = []

    paths = sorted(data_dir.rglob("*.parquet"))
    if not paths:
        raise FileNotFoundError(f"no parquet under {data_dir}")

    for path in paths:
        for idx, row in enumerate(pq.read_table(path).to_pylist()):
            subject = (row.get("type") or "").strip()
            level = (row.get("level") or "").strip()
            problem = row.get("problem") or ""
            solution = row.get("solution") or ""

            mapping = SUBJECT_MAP.get(subject)
            if mapping is None:
                report.drop(f"unknown subject: {subject or '(blank)'}")
                continue
            tag, top_band_only = mapping
            if not tag:
                report.drop(f"off-blueprint subject: {subject}")
                continue

            if level not in LEVEL_BANDS:
                # The field has a junk class; Level 5 is past the ceiling.
                report.drop(f"level filtered: {level or '(blank)'}")
                continue
            if level == "Level 4" and subject not in TOP_BAND_SUBJECTS:
                report.drop("level 4 outside algebra")
                continue

            if "[asy]" in problem or "[asy]" in solution:
                report.drop("asymptote figure (unrenderable)")
                continue

            answer = extract_boxed(solution)
            if answer is None:
                report.drop("no \\boxed{} answer")
                continue

            item_type, flags = classify_answer(answer)
            if flags:
                report.flag(flags[0])

            band = "top" if (top_band_only or level == "Level 4") else LEVEL_BANDS[level]

            records.append(
                normalize.make_record(
                    product_id="tsi-ready",
                    source=SOURCE,
                    source_ref=f"{path.stem}/{idx}",
                    section="math",
                    item_type=item_type,
                    stem=problem,
                    answer_key=answer,
                    official_expl=solution,
                    # The subject label is part of the dataset.
                    tags=[{"slug": tag, "label_source": "imported"}],
                    license=LICENSE,
                    redistributable=0,
                    difficulty_prior=normalize.band_prior(band, problem),
                    difficulty_prior_src="dataset-level",
                    flags=flags,
                )
            )
            report.keep([tag])

    print(report.render())
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="content/datasets/competition_math")
    parser.add_argument("--out", default="content/ingest/competition_math.jsonl")
    args = parser.parse_args()

    records = load(Path(args.data))
    written = normalize.write_jsonl(records, Path(args.out))
    print(f"wrote {written} record(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
