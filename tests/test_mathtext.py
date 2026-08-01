"""Deciding what is mathematics. `web/mathtext.py`.

KaTeX's auto-render finds delimited maths on its own. What it cannot find is
MMLU's choices, which are stored as bare expressions -- `\\frac{7}{9}` with no
`$` anywhere -- so those get wrapped before the page is written.

The asymmetry that shapes every test here: leaving a fraction ugly costs
nothing, and rendering an English sentence as mathematics makes an item
unreadable at the exact moment someone is trying to answer it. So the rule is
conservative, and the cases that matter most are the ones it must decline.
"""

from __future__ import annotations

import pytest

from web import mathtext


@pytest.mark.parametrize(
    "text",
    [
        r"\frac{7}{9}",
        r"\frac{125}{648}",
        r"x^{2}+1",
        r"\sqrt{2}/3",
        r"\dfrac{1}{2}",
        r"  \frac{1}{2}  ",  # surrounding whitespace is not prose
    ],
)
def test_bare_expressions_are_wrapped(text):
    assert mathtext.looks_like_bare_math(text)
    assert mathtext.delimit(text).startswith("$")
    assert mathtext.delimit(text).endswith("$")


@pytest.mark.parametrize(
    "text",
    [
        r"$\frac{7}{9}$",          # already delimited; the author said where
        r"What is $\frac{7}{9}$?",
        r"\[ x = 1 \]",
        r"\(x\)",
    ],
)
def test_already_delimited_text_is_left_alone(text):
    assert not mathtext.looks_like_bare_math(text)
    assert mathtext.delimit(text) == text


@pytest.mark.parametrize(
    "text",
    [
        "change one full day",
        "set our watch back or ahead",
        "The author implies the opposite.",
        r"the \emph{best} answer here",   # a command inside a sentence
        "42",
        "1/2",
        "0.75",
        "",
        "   ",
    ],
)
def test_prose_and_plain_answers_are_never_wrapped(text):
    """The expensive failure. An item rendered as maths is unanswerable."""
    assert not mathtext.looks_like_bare_math(text)
    assert mathtext.delimit(text) == text


def test_none_renders_as_empty_rather_than_the_word_none(drill_none=None):
    assert mathtext.delimit(None) == ""


def test_the_real_corpus_choices_split_the_way_the_rule_expects():
    """Sampled from what the importers actually stored.

    RACE choices are prose, MMLU's are bare expressions, and both live in the
    same `choices_json` column -- so one rule has to separate them.
    """
    race = ["change one full day", "set our watch back", "set our watch ahead"]
    mmlu = [r"\frac{7}{9}", r"\frac{8}{9}", r"\frac{5}{9}"]
    assert not any(mathtext.looks_like_bare_math(c) for c in race)
    assert all(mathtext.looks_like_bare_math(c) for c in mmlu)
