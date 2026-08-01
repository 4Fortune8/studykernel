"""MMLU importer. DATA_SOURCING_MATH.md §4.

The only multiple-choice source of the three -- matching the actual TSIA2 item
format -- plus direct coverage of statistics. MIT, so `redistributable = 1`.

Two subsets are mixed-domain and are the one place in this pipeline that needs
the batch-label protocol. Until that pass runs they take a coarse tag plus a
`coarse_tag` flag: wrong for maybe a third of items, but servable on day one
and marked for the real pass. Under the provenance rule they serve practice
immediately and never reinforce DAG edges.

Known hazard, accepted: MMLU has a documented low-single-digit answer-key
error rate. The detection path is the `disputed_key` flag in the exchange
return payload -- cheap to add, worth it for this source.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pyarrow.parquet as pq

from ingest import normalize

SOURCE = "mmlu"
LICENSE = "MIT"
LETTERS = "ABCD"

# subset -> (tag, band, needs_labeling_pass)
SUBSETS: dict[str, tuple[str, str, bool]] = {
    "elementary_mathematics": ("quantitative-reasoning-l2", "low", True),
    "high_school_mathematics": ("algebraic-reasoning-l4", "mid_high", True),
    # Single-domain; no labeling pass needed.
    "high_school_statistics": ("probabilistic-statistical-reasoning-l4", "mid", False),
}

# dev is 5 items/subset -- skip it.
SPLITS = ("test", "validation")


def load(data_dir: Path) -> list[dict]:
    report = normalize.IngestReport(SOURCE)
    records: list[dict] = []

    for subset, (tag, band, needs_pass) in SUBSETS.items():
        for split in SPLITS:
            path = data_dir / subset / f"{split}-00000-of-00001.parquet"
            if not path.exists():
                report.drop(f"missing: {subset}/{split}")
                continue

            for idx, row in enumerate(pq.read_table(path).to_pylist()):
                stem = row["question"]
                choices = list(row["choices"])

                # A few items reference figures or "the following" content
                # that isn't present.
                if normalize.references_missing_figure(stem):
                    report.drop("references a missing figure")
                    continue
                if len(choices) != 4:
                    report.drop("not four options")
                    continue

                answer_idx = int(row["answer"])
                if not 0 <= answer_idx < 4:
                    report.drop("answer index out of range")
                    continue

                flags = ["coarse_tag"] if needs_pass else []
                if flags:
                    report.flag("coarse_tag")

                records.append(
                    normalize.make_record(
                        product_id="tsi-ready",
                        source=SOURCE,
                        source_ref=f"{subset}/{split}/{idx}",
                        section="math",
                        item_type="mc-one",
                        stem=stem,
                        choices=choices,
                        answer_key=LETTERS[answer_idx],
                        tags=[
                            {
                                "slug": tag,
                                # Mixed-domain subsets carry a model-grade
                                # label until the batch pass runs; the
                                # provenance gate keeps them out of the DAG.
                                "label_source": "model" if needs_pass else "imported",
                                "reviewed": 0,
                            }
                        ],
                        license=LICENSE,
                        redistributable=1,
                        difficulty_prior=normalize.band_prior(band, stem),
                        difficulty_prior_src="subset-band",
                        flags=flags,
                    )
                )
                report.keep([tag])

    print(report.render())
    if any(needs for _t, _b, needs in SUBSETS.values()):
        print(
            "\n  NOTE: two subsets are mixed-domain and carry coarse tags.\n"
            "  ~30 batch-label round trips are outstanding before these tags\n"
            "  can be trusted for routing. They serve practice meanwhile."
        )
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="content/datasets/mmlu")
    parser.add_argument("--out", default="content/ingest/mmlu.jsonl")
    args = parser.parse_args()

    records = load(Path(args.data))
    written = normalize.write_jsonl(records, Path(args.out))
    print(f"wrote {written} record(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
