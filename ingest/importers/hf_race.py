"""RACE importer -- the ELAR reading bank. DATA_SOURCING_ELAR.md §2.

~28K passages / ~98K four-option MCQ items from real middle- and high-school
English exams. Two properties make it the right bulk source: RACE-M sits below
the TSIA2 readiness line and RACE-H at and above it (a ready-made two-band
ladder for Elo priors, the same role the MATH `level` field plays), and
passages arrive with their question sets attached, mapping straight onto the
existing `passage_id` grouping with no schema change.

Research license -> `redistributable = 0`.

**Passages are sampled, never split.** Serving a passage means serving its
full question set, which buys multipart-adjacent practice for free -- and
TSIA2's connected question sets are otherwise unrepresented in any public
dataset (DATA_SOURCING_MATH.md §7.2).

Items arrive with no explanations, which is what forced the `anchored`
explanation policy: the model may justify the key only by quoting the passage
verbatim, and must emit `disputed_key` when it cannot. Genre labeling
(literary vs informational) is a separate cheap batch pass over ~400 passages;
until it runs, items carry `informational-analysis` with a `coarse_tag` flag.
"""

from __future__ import annotations

import argparse
import random
from collections import defaultdict
from pathlib import Path

import pyarrow.parquet as pq

from ingest import normalize

SOURCE = "race"
LICENSE = "race-research"
SEED = 20260730

# ~400 passages, 60% high / 40% middle. ~98K items would drown the bank.
PASSAGE_TARGET = 400
HIGH_SHARE = 0.6

CONFIG_BANDS = {"high": "mid_high", "middle": "low"}

# Exam artifacts that did not survive extraction.
ARTIFACTS = ("_____", "according to the chart", "according to the table")


def load(data_dir: Path) -> list[dict]:
    report = normalize.IngestReport(SOURCE)
    records: list[dict] = []
    rng = random.Random(SEED)

    for config, share in (("high", HIGH_SHARE), ("middle", 1.0 - HIGH_SHARE)):
        quota = int(PASSAGE_TARGET * share)
        band = CONFIG_BANDS[config]

        # Group rows by article so a passage is sampled whole.
        by_article: dict[str, list[dict]] = defaultdict(list)
        for path in sorted((data_dir / config).glob("*.parquet")):
            for row in pq.read_table(path).to_pylist():
                by_article[row["article"]].append(row)

        articles = sorted(by_article)
        rng.shuffle(articles)

        taken = 0
        for article in articles:
            if taken >= quota:
                break
            if any(a in article.lower() for a in ARTIFACTS):
                report.drop("passage contains an exam artifact")
                continue

            rows = by_article[article]
            pid = normalize.passage_id(article)
            passage = {
                "passage_id": pid,
                "text": normalize.normalize_stem(article),
                # Filled by the genre batch pass; items inherit it.
                "genre": None,
                "source": SOURCE,
                "license": LICENSE,
                "word_count": len(article.split()),
            }

            kept_any = False
            for row in rows:
                options = list(row["options"])
                if len(options) != 4:
                    report.drop("not four options")
                    continue
                answer = str(row["answer"]).strip().upper()
                if answer not in "ABCD":
                    report.drop("answer not a letter A-D")
                    continue
                if any("all of the above" in o.lower() for o in options):
                    report.drop("'all of the above' option")
                    continue

                records.append(
                    normalize.make_record(
                        product_id="tsi-ready",
                        source=SOURCE,
                        source_ref=f"{config}/{row.get('example_id', pid)}",
                        section="elar",
                        item_type="mc-one",
                        stem=row["question"],
                        choices=options,
                        answer_key=answer,
                        # No official explanation -> `anchored` policy applies.
                        official_expl=None,
                        tags=[
                            {
                                "slug": "informational-analysis",
                                "label_source": "model",
                                "reviewed": 0,
                            }
                        ],
                        license=LICENSE,
                        redistributable=0,
                        difficulty_prior=normalize.band_prior(band, row["question"]),
                        difficulty_prior_src=f"race-{config}",
                        passage=passage,
                        flags=["coarse_tag"],
                    )
                )
                report.keep(["informational-analysis"])
                report.flag("coarse_tag")
                kept_any = True

            if kept_any:
                taken += 1

    print(report.render())
    print(
        "\n  NOTE: genre labels are unset. ~16 batch round trips over passages\n"
        "  split literary vs informational; items inherit the passage tag.\n"
        "  synthesis-two-sources is unsupplied by this source by design."
    )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="content/datasets/race")
    parser.add_argument("--out", default="content/ingest/race.jsonl")
    args = parser.parse_args()

    records = load(Path(args.data))
    written = normalize.write_jsonl(records, Path(args.out))
    print(f"wrote {written} record(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
