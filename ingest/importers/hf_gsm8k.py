"""GSM8K importer. DATA_SOURCING_MATH.md §2.

Bulk supply for Quantitative Reasoning: multi-step arithmetic word problems
(rates, money, measurement, proportional scaling). The closest style match to
TSIA2 quantitative items of any public dataset.

MIT licensed, so `redistributable = 1` -- one of only two sources in the math
pipeline that could ever ship in a public pack.

**The ingest is capped on purpose.** All 8.8K items would swamp the bank and
skew the allocator's availability term toward one domain, which would quietly
distort every priority the system computes. Full test split plus a seeded
random 1,500 from train.
"""

from __future__ import annotations

import argparse
import random
from pathlib import Path

import pyarrow.parquet as pq

from ingest import normalize

SOURCE = "gsm8k"
LICENSE = "MIT"
TRAIN_SAMPLE = 1500
SEED = 20260730  # fixed so a re-ingest reproduces the same bank


def load(data_dir: Path) -> list[dict]:
    report = normalize.IngestReport(SOURCE)
    records: list[dict] = []

    for split, cap in (("test", None), ("train", TRAIN_SAMPLE)):
        path = data_dir / "main" / f"{split}-00000-of-00001.parquet"
        if not path.exists():
            report.drop(f"missing split file: {split}")
            continue

        rows = list(enumerate(pq.read_table(path).to_pylist()))
        if cap is not None and len(rows) > cap:
            rows = random.Random(SEED).sample(rows, cap)

        for idx, row in rows:
            question = row["question"]
            answer = row["answer"]
            if "####" not in answer:
                report.drop("no #### final answer")
                continue

            reasoning, _, final = answer.partition("####")
            # Final answers are integers throughout, so grading is a clean
            # numeric compare -- no flags needed for this source.
            key = final.strip().replace(",", "")

            records.append(
                normalize.make_record(
                    product_id="tsi-ready",
                    source=SOURCE,
                    source_ref=f"{split}/{idx}",
                    section="math",
                    item_type="numeric",
                    stem=question,
                    answer_key=key,
                    official_expl=reasoning.strip(),
                    # Single-domain by construction; no labeling pass needed.
                    tags=[
                        {"slug": "quantitative-reasoning-l3", "label_source": "imported"}
                    ],
                    license=LICENSE,
                    redistributable=1,
                    # Grade-school ceiling ~ diagnostic levels 2-4.
                    difficulty_prior=normalize.band_prior("low_mid", question),
                    difficulty_prior_src="dataset-band",
                )
            )
            report.keep(["quantitative-reasoning-l3"])

    print(report.render())
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="content/datasets/gsm8k")
    parser.add_argument("--out", default="content/ingest/gsm8k.jsonl")
    args = parser.parse_args()

    records = load(Path(args.data))
    written = normalize.write_jsonl(records, Path(args.out))
    print(f"wrote {written} record(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
