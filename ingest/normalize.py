"""Shared importer machinery: normalizer, hasher, dedupe logger, JSONL writer.

DATA_SOURCING_MATH.md §5. Order matters, so it lives in one place rather than
being reimplemented per importer.

The rule that shapes everything here: **items without a verifiable answer key
are dropped at ingest.** Grading is deterministic or the item does not exist
(Datasourcing.md §3). An item stored and then skipped at serve time is worse
than one never stored -- it inflates the availability term and hides a real
content gap.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from kernel.pedagogy import grading

_WS = re.compile(r"\s+")

# Stems referencing a figure that did not survive extraction. Dropped rather
# than flagged -- DATA_SOURCING_MATH.md §4 and §3.3.
_FIGURE_REF = re.compile(
    r"figure|graph below|shown below|the diagram|\[asy\]|following table", re.IGNORECASE
)


def normalize_stem(text: str) -> str:
    """Whitespace only. LaTeX is stored raw -- it is the ground truth.

    Stripping it would destroy the content a future web UI renders with KaTeX,
    and the CLI showing raw LaTeX is a known cosmetic cost, not a data problem.
    DATA_SOURCING_MATH.md §5.2.
    """
    text = unicodedata.normalize("NFKC", text)
    return _WS.sub(" ", text).strip()


def content_id(product_id: str, stem: str) -> str:
    """Content-addressed ID: lowercase, all whitespace removed, then hashed.

    This is what makes cross-source dedupe work -- identical items circulate
    between question banks with whitespace variance.
    """
    canonical = _WS.sub("", normalize_stem(stem)).casefold()
    return hashlib.sha256((product_id + canonical).encode()).hexdigest()[:16]


def passage_id(text: str) -> str:
    canonical = _WS.sub("", normalize_stem(text)).casefold()
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def references_missing_figure(stem: str) -> bool:
    return bool(_FIGURE_REF.search(stem))


@dataclass
class IngestReport:
    """Per-source counts, drops and flags. Printed at the end of every run.

    DATA_SOURCING_MATH.md §8 asks for this explicitly, and it matters more
    than it looks: the drop tallies are how you notice that a `\\boxed{}`
    extractor silently stopped working.
    """

    source: str
    kept: int = 0
    dropped: Counter = field(default_factory=Counter)
    flagged: Counter = field(default_factory=Counter)
    by_tag: Counter = field(default_factory=Counter)

    def drop(self, reason: str) -> None:
        self.dropped[reason] += 1

    def flag(self, name: str) -> None:
        self.flagged[name] += 1

    def keep(self, tags: Iterable[str]) -> None:
        self.kept += 1
        for tag in tags:
            self.by_tag[tag] += 1

    def render(self) -> str:
        lines = [f"--- {self.source} ---", f"  kept: {self.kept}"]
        if self.by_tag:
            lines.append("  by tag:")
            for tag, n in self.by_tag.most_common():
                lines.append(f"    {tag:<40} {n:>6}")
        if self.dropped:
            lines.append(f"  dropped: {sum(self.dropped.values())}")
            for reason, n in self.dropped.most_common():
                lines.append(f"    {reason:<40} {n:>6}")
        if self.flagged:
            lines.append("  flagged (held out of serving until reviewed):")
            for name, n in self.flagged.most_common():
                lines.append(f"    {name:<40} {n:>6}")
        return "\n".join(lines)


def make_record(
    *,
    product_id: str,
    source: str,
    source_ref: str,
    section: str,
    item_type: str,
    stem: str,
    answer_key: str | None,
    tags: list[dict[str, Any]],
    license: str,
    redistributable: int,
    choices: list[str] | None = None,
    official_expl: str | None = None,
    difficulty_prior: float | None = None,
    difficulty_prior_src: str | None = None,
    passage: dict[str, Any] | None = None,
    role: str = "pool",
    style: str = "proxy",
    flags: list[str] | None = None,
) -> dict[str, Any]:
    stem = normalize_stem(stem)
    record = {
        "item_id": content_id(product_id, stem),
        "product_id": product_id,
        "source": source,
        "source_ref": source_ref,
        "section": section,
        "item_type": item_type,
        "stem": stem,
        "choices": choices,
        "answer_key": answer_key,
        "official_expl": official_expl,
        "tags": tags,
        "license": license,
        "redistributable": redistributable,
        "model_transformed": 0,
        "difficulty_prior": difficulty_prior,
        "difficulty_prior_src": difficulty_prior_src,
        "role": role,
        "style": style,
        "flags": flags or [],
    }
    if passage:
        record["passage"] = passage
        record["passage_id"] = passage["passage_id"]
    return record


def write_jsonl(records: list[dict[str, Any]], path: Path) -> int:
    """Write records, dropping cross-source duplicates and ungradable items."""
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    written = 0
    with path.open("w") as fh:
        for rec in records:
            if rec["item_id"] in seen:
                continue
            if rec["item_type"] != "essay" and not grading.is_gradable(rec["answer_key"]):
                continue
            seen.add(rec["item_id"])
            fh.write(json.dumps(rec) + "\n")
            written += 1
    return written


# Difficulty prior bands. These are starting positions only -- a few hundred
# Glicko-2 updates will dominate them. The reason to seed bands at all is the
# allocator's first week, before any attempt data exists.
# DATA_SOURCING_MATH.md §6.
BANDS: dict[str, float] = {
    "low": 1250.0,
    "low_mid": 1350.0,
    "mid": 1450.0,
    "mid_high": 1550.0,
    "high": 1650.0,
    "top": 1750.0,
}

# Half-width of the deterministic spread applied within a band.
BAND_SPREAD = 60.0


def band_prior(band: str, seed_text: str) -> float:
    """A band's center, spread deterministically by item content.

    Without this every item from a source shares one exact rating, and the
    allocator's band query becomes a knife edge -- it either matches a source
    entirely or misses it entirely. Spreading items across the band is also
    closer to the truth: a source's items are not all equally hard, we simply
    do not know which are which yet.

    Derived from the stem hash rather than a RNG so a re-ingest reproduces the
    same bank, and so two sources that happen to share an item place it
    identically.
    """
    digest = hashlib.sha256(seed_text.encode()).digest()
    unit = int.from_bytes(digest[:4], "big") / 0xFFFFFFFF  # [0, 1]
    return BANDS[band] + (unit * 2.0 - 1.0) * BAND_SPREAD
