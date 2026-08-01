"""Deciding what on a page is mathematics. Presentation only.

DATA_SOURCING_MATH.md §5.2 stores LaTeX exactly as the source wrote it and
calls raw display "a known cosmetic cost… a future web UI renders it (KaTeX)".
This module is the part of that which cannot happen in the browser, because it
is a judgement about the corpus rather than about markup.

Two shapes arrive from four importers:

- **Delimited.** MATH and GSM8K stems carry `$…$`, `$$…$$` and `\\[…\\]`
  (349 of a 400-stem sample). KaTeX's auto-render extension finds these and
  this module is not involved.
- **Bare.** MMLU choices are raw expressions with no delimiters at all --
  `\\frac{7}{9}` on its own. 179 of 9,188 stored choices look like this and
  auto-render will never touch them, because there is nothing to find.

So bare expressions get wrapped here, before the page is written. Nothing is
rewritten in the database: the stored value stays byte-for-byte what the
source published, which is what keeps grading and provenance honest.

The wrapping test is deliberately conservative. A false positive renders an
English sentence as mathematics, which is worse than leaving a fraction ugly,
so anything with real prose in it is left alone.
"""

from __future__ import annotations

import re

# A LaTeX control sequence, or the sub/superscript-with-group forms that only
# appear in maths.
_LATEX = re.compile(r"\\[a-zA-Z]+|[\^_]\{")

# Any delimiter KaTeX's auto-render already handles. If one is present the
# author has said where the maths is and this module defers to them.
_DELIMITED = re.compile(r"\$|\\\(|\\\[")

# Three or more letters in a row once control sequences are gone: prose.
_WORDS = re.compile(r"[A-Za-z]{3,}")


def looks_like_bare_math(text: str) -> bool:
    """Whether `text` is an undelimited mathematical expression.

    True for `\\frac{7}{9}` and `x^{2}+1`. False for `$\\frac{7}{9}$`, which is
    already delimited, and false for `the \\emph{best} answer`, which is a
    sentence that happens to contain a command.
    """
    stripped = text.strip()
    if not stripped or _DELIMITED.search(stripped) or not _LATEX.search(stripped):
        return False
    return not _WORDS.search(_LATEX.sub("", stripped))


def delimit(text: str | None) -> str:
    """Wrap a bare expression so auto-render can see it. Otherwise unchanged."""
    if text is None:
        return ""
    return f"${text.strip()}$" if looks_like_bare_math(text) else text
