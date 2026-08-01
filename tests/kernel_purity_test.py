"""The purity test. DESIGN.md §14 -- "not a joke".

One rule: **no kernel code may name an exam.** It is the cheapest possible
enforcement of the architecture, and it will catch the drift the moment it
happens, which is the only time drift is cheap to fix.

The failure this prevents is not stylistic. The moment `kernel/` knows what a
TSIA2 is, the second product stops being a config change and becomes a fork,
and the claim that this is a learning kernel rather than a test-prep app
becomes false without anyone deciding to make it false.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
KERNEL = ROOT / "kernel"
# The web front end is held to the same rule. It is an adapter over the kernel,
# exactly as `cli.py` is, and a front end that hardcodes one product is a fork
# of the UI waiting to happen -- the same failure this test exists to catch,
# one layer out. It ships in the same wheel; it obeys the same rule.
SCANNED = (KERNEL, ROOT / "web")

# Exam and product names. Anything here appearing in kernel/ is a bug.
FORBIDDEN = (
    "tsia",
    "tsi-ready",
    "tsi_ready",
    "accuplacer",
    "writeplacer",
    "gre",
    "gre-forge",
    "gre_forge",
    "sat",
    "act",
    "lsat",
    "mcat",
    "gmat",
    "toefl",
    "staar",
    "naep",
    "openstax",
    "khan",
    "ets",
    "crc",
)

# Words that legitimately contain a forbidden substring. Checked as whole
# words below, so this list stays short by construction.
ALLOWED_CONTEXT = re.compile(
    r"\b(aggregate|aggregates|aggregated|aggregation|greater|greatest|degree|degrees|"
    r"progress|regression|agreement|ingredient|satisfied|satisfies|satisfy|satisfying|"
    r"saturation|saturate|state|states|stated|action|actions|active|actual|actually|"
    r"exact|exactly|fact|factor|factoring|character|characteristic|interact|"
    r"abstract|extract|extractor|contract|contracts|tract|practice|practical)\b",
    re.IGNORECASE,
)

WORD = re.compile(r"[a-z][a-z0-9_-]*", re.IGNORECASE)


def kernel_files() -> list[Path]:
    return sorted(
        p
        for root in SCANNED
        if root.exists()
        for p in root.rglob("*")
        if p.suffix in {".py", ".sql", ".md", ".yaml", ".yml", ".html", ".css"}
        and "__pycache__" not in p.parts
    )


def test_kernel_files_exist():
    assert kernel_files(), "purity test found no kernel files -- check the path"


@pytest.mark.parametrize("path", kernel_files(), ids=lambda p: p.name)
def test_kernel_names_no_exam(path: Path):
    """Whole-word scan. Substring matching would flag 'aggregate' for 'gre'."""
    violations: list[str] = []

    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        stripped = ALLOWED_CONTEXT.sub("", line)
        for word in WORD.findall(stripped):
            if word.lower() in FORBIDDEN:
                violations.append(f"{path.name}:{lineno}: {word!r} in {line.strip()!r}")

    assert not violations, (
        "kernel code names an exam or a product -- move it to product config:\n"
        + "\n".join(violations)
    )


def test_kernel_does_not_import_products_or_ingest():
    """The dependency arrow points one way: products -> kernel, never back."""
    offenders = [
        f"{p.name}: {line.strip()}"
        for p in kernel_files()
        if p.suffix == ".py"
        for line in p.read_text().splitlines()
        if re.match(r"\s*(from|import)\s+(products|ingest)\b", line)
    ]
    assert not offenders, "kernel imports outside itself:\n" + "\n".join(offenders)
