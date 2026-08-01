"""Deriving the named variables that route expressions read.

This module is the load-bearing half of DESIGN.md §5's sorting rule. A route
says `section_estimate >= 950`. The kernel has no idea what that number means
on any real score scale and must never learn. What it knows is a small set of
variable *kinds* -- ways of turning tag-level state into a scalar with an
uncertainty -- and the product pack says which kind produces the variable and
what it is called.

Add a variable kind only when an existing one genuinely cannot express a
product's scoring shape. Every kind added here is surface area that all future
products inherit.

Kinds:
  scaled_ability  blueprint-weighted mean tag rating, linearly mapped onto a
                  reported score scale. sd propagates from tag RD.
  manual          a scalar the learner reports (a practice essay score).
                  sd is 0 -- an earned score is not a measurement of ability,
                  it is an observation.
  achieved_level  the highest taxonomy level whose tags clear the mastery bar.
                  Per-domain only; this is what all_domains(...) reads.
  reliability     a tag group's reliability lower bound, as a [0, 1] scalar.
"""

from __future__ import annotations

import math
from dataclasses import replace

from kernel.state.view import Estimate, StateView, TagState


# How correlated per-tag abilities are assumed to be when combined into one
# position estimate. NOT a free parameter -- setting it to 0 (independence) is
# actively wrong and dangerous: averaging 28 untouched tags at RD 350 would
# report a standard error of 66, i.e. near-certainty about a learner who has
# answered nothing. Tag abilities in one subject share a common cause, so they
# are strongly positively correlated and the combined uncertainty must not
# shrink toward zero just because the taxonomy is finely divided.
ABILITY_CORRELATION = 0.6


def _weighted_rating(tags: list[TagState]) -> tuple[float, float]:
    """Blueprint-weighted mean rating and its standard error.

    The error term interpolates between independence (quadrature, shrinks like
    1/sqrt(n)) and perfect correlation (weighted mean of RDs, no shrinkage at
    all). A thin history therefore yields a wide interval and, downstream, a
    large stopping margin -- that coupling is the intended behavior, and it is
    what stops the tool congratulating a learner who has not started.
    """
    if not tags:
        return 0.0, 0.0
    total_w = sum(t.coverage_weight for t in tags) or 1.0
    mean = sum(t.rating * t.coverage_weight for t in tags) / total_w

    independent = sum((t.rd * t.coverage_weight) ** 2 for t in tags) / (total_w**2)
    correlated = (sum(t.rd * t.coverage_weight for t in tags) / total_w) ** 2
    var = (1.0 - ABILITY_CORRELATION) * independent + ABILITY_CORRELATION * correlated
    return mean, math.sqrt(var)


def _scaled_ability(state: StateView, spec: dict) -> Estimate:
    section = spec.get("section")
    tags = state.tags_in_section(section) if section else list(state.tags.values())
    mean, sd = _weighted_rating(tags)

    r_lo, r_hi = spec.get("rating_range", [1000.0, 2000.0])
    s_lo, s_hi = spec.get("scale_range", [0.0, 100.0])
    if r_hi == r_lo:
        raise ValueError("scaled_ability rating_range must span a nonzero interval")
    factor = (s_hi - s_lo) / (r_hi - r_lo)

    value = s_lo + (mean - r_lo) * factor
    # Clamp to the reported scale: a score scale has ends, and letting the
    # estimate run past them would make P(cross) look better than it is.
    value = max(s_lo, min(s_hi, value))
    return Estimate(value, abs(sd * factor))


def _achieved_level(state: StateView, spec: dict, tags: list[TagState]) -> Estimate:
    """Highest level whose tags all clear the mastery bar.

    Deliberately strict: a level counts as achieved only if *every* tag at or
    below it clears the bar. Partial credit here would let a hollow level pass
    on the strength of its easiest tag, which is the exact failure the
    no-domain-holes route exists to detect.
    """
    bar = spec.get("mastery_bar", state.mastery_bar)
    levels = sorted({t.level for t in tags if t.level is not None})
    achieved = 0
    for level in levels:
        at_or_below = [t for t in tags if t.level is not None and t.level <= level]
        if at_or_below and all(t.reliability_lo >= bar for t in at_or_below):
            achieved = level
        else:
            break
    return Estimate(float(achieved), 0.0)


def _reliability(state: StateView, spec: dict, tags: list[TagState]) -> Estimate:
    if not tags:
        return Estimate(0.0, 0.0)
    return Estimate(min(t.reliability_lo for t in tags), 0.0)


def _compute_one(state: StateView, name: str, spec: dict) -> Estimate:
    kind = spec.get("kind")
    if kind == "scaled_ability":
        return _scaled_ability(state, spec)
    if kind == "manual":
        return Estimate(float(state.manual_values.get(name, spec.get("default", 0.0))), 0.0)
    if kind == "reliability":
        section = spec.get("section")
        tags = state.tags_in_section(section) if section else list(state.tags.values())
        return _reliability(state, spec, tags)
    if kind == "achieved_level":
        # Only meaningful per-domain; a global achieved_level would average
        # away exactly the holes it is meant to expose.
        raise ValueError(
            f"variable {name!r}: achieved_level is a domain variable, declare it "
            "under `domains.variables`"
        )
    raise ValueError(f"variable {name!r}: unknown kind {kind!r}")


def _domains(state: StateView) -> dict[str, list[TagState]]:
    """Group tags into domains as the product declares.

    `top_level_tags` treats each parentless tag as a domain and every tag
    beneath it as its members -- the shape a published taxonomy with diagnostic
    levels arrives in.
    """
    decl = state.domain_declarations or {}
    source = decl.get("source", "top_level_tags")
    section = decl.get("section")
    pool = state.tags_in_section(section) if section else list(state.tags.values())

    if source != "top_level_tags":
        raise ValueError(f"unknown domain source {source!r}")

    groups: dict[str, list[TagState]] = {
        t.slug: [] for t in pool if t.parent_slug is None
    }
    for tag in pool:
        root = tag.slug if tag.parent_slug is None else tag.parent_slug
        # Walk up to the top-level ancestor.
        seen = 0
        while root in state.tags and state.tags[root].parent_slug is not None and seen < 16:
            root = state.tags[root].parent_slug  # type: ignore[assignment]
            seen += 1
        groups.setdefault(root, []).append(tag)
    return groups


def recompute(state: StateView) -> StateView:
    """Recompute every declared variable from current tag state.

    Called after each attempt and, critically, inside `StateView.perturb` --
    which is what makes numeric gradients correct: nudging a tag's rating
    propagates all the way through to route probabilities.
    """
    variables = {
        name: _compute_one(state, name, spec)
        for name, spec in (state.var_declarations or {}).items()
    }

    domain_specs = (state.domain_declarations or {}).get("variables", {})
    domain_variables: dict[str, dict[str, Estimate]] = {}
    if domain_specs:
        for domain, tags in _domains(state).items():
            per_domain: dict[str, Estimate] = {}
            for name, spec in domain_specs.items():
                kind = spec.get("kind")
                if kind == "achieved_level":
                    per_domain[name] = _achieved_level(state, spec, tags)
                elif kind == "reliability":
                    per_domain[name] = _reliability(state, spec, tags)
                elif kind == "scaled_ability":
                    mean, sd = _weighted_rating(tags)
                    r_lo, r_hi = spec.get("rating_range", [1000.0, 2000.0])
                    s_lo, s_hi = spec.get("scale_range", [0.0, 100.0])
                    factor = (s_hi - s_lo) / (r_hi - r_lo)
                    per_domain[name] = Estimate(
                        max(s_lo, min(s_hi, s_lo + (mean - r_lo) * factor)),
                        abs(sd * factor),
                    )
                else:
                    raise ValueError(f"domain variable {name!r}: unknown kind {kind!r}")
            domain_variables[domain] = per_domain

    return replace(state, variables=variables, domain_variables=domain_variables)
