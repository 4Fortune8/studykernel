#!/usr/bin/env python3
"""Fetch dataset parquet files from the HuggingFace CDN into content/datasets/.

Plain HTTPS, no git-lfs and no `datasets` dependency. Existing files are
skipped, so re-running is cheap and safe.

The repo layout under content/ is gitignored (DESIGN.md §14) -- these are
user-supplied working files, not version-controlled content.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

CONTENT = Path(__file__).resolve().parent.parent / "content" / "datasets"

# repo -> which files to keep. A prefix tuple keeps every file under it;
# None keeps every parquet in the repo.
REPOS: dict[str, tuple[str, ...] | None] = {
    "openai/gsm8k": ("main/",),
    "dim/competition_math": None,
    "ehovy/race": ("all/", "high/", "middle/"),
    # Only the three subsets DATA_SOURCING_MATH.md §4 actually uses.
    "cais/mmlu": (
        "elementary_mathematics/",
        "high_school_mathematics/",
        "high_school_statistics/",
    ),
}

API = "https://huggingface.co/api/datasets/{repo}/tree/main/{path}?recursive=1"
RESOLVE = "https://huggingface.co/datasets/{repo}/resolve/main/{path}"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "studykernel-fetch"})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def list_parquet(repo: str) -> list[str]:
    entries = json.loads(_get(API.format(repo=repo, path="")))
    return sorted(
        e["path"] for e in entries if e["type"] == "file" and e["path"].endswith(".parquet")
    )


def wanted(path: str, keep: tuple[str, ...] | None) -> bool:
    return keep is None or any(path.startswith(p) for p in keep)


def main() -> int:
    total_bytes = 0
    for repo, keep in REPOS.items():
        dest_root = CONTENT / repo.split("/")[-1]
        try:
            paths = [p for p in list_parquet(repo) if wanted(p, keep)]
        except urllib.error.HTTPError as exc:
            print(f"!! {repo}: cannot list tree ({exc}); skipping", flush=True)
            continue

        if not paths:
            print(f"!! {repo}: no parquet matched the keep filter", flush=True)
            continue

        print(f"== {repo}: {len(paths)} file(s) -> {dest_root}", flush=True)
        for path in paths:
            dest = dest_root / path
            if dest.exists() and dest.stat().st_size > 1024:
                print(f"   skip {path} (have it)", flush=True)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob = _get(RESOLVE.format(repo=repo, path=path))
            dest.write_bytes(blob)
            total_bytes += len(blob)
            print(f"   got  {path}  {len(blob) / 1e6:.1f} MB", flush=True)

    print(f"\ndone: {total_bytes / 1e6:.1f} MB fetched into {CONTENT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
