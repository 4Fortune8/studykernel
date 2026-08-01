"""Variance -- the consistency measure. DESIGN.md §6.3.

Rolling variance of rating updates. Motivated by adaptive tests, which read
erratic performance as lower ability, but kept as a kernel-level signal for
every goal type: high variance at a level is the empirical signature of
shallow pattern-knowledge (§3).

That makes this module part of the Goodhart defense, not an analytics nicety.
Pattern-matching is exactly the strategy that produces high-variance
performance, so variance is what the mastery bar punishes.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

# Long enough to be stable, short enough to notice a real change in a week.
DEFAULT_WINDOW = 20


def rolling_variance(deltas: Sequence[float], window: int = DEFAULT_WINDOW) -> float:
    """Sample variance of the most recent `window` rating deltas.

    Returns 0.0 below two observations -- undefined, and the allocator treats
    unknown variance as un-penalized rather than guessing.
    """
    recent = list(deltas)[-window:]
    n = len(recent)
    if n < 2:
        return 0.0
    mean = sum(recent) / n
    return sum((d - mean) ** 2 for d in recent) / (n - 1)


def consistency_penalty(variance: float, reference: float) -> float:
    """Map variance to a [0, 1] multiplier; 1.0 is perfectly consistent.

    `reference` is the learner's own median tag variance, so this compares a
    tag against that learner rather than against a population constant.
    """
    if reference <= 0 or variance <= 0:
        return 1.0
    return 1.0 / (1.0 + variance / reference)


def median(values: Iterable[float]) -> float:
    vals = sorted(values)
    if not vals:
        return 0.0
    mid = len(vals) // 2
    if len(vals) % 2:
        return vals[mid]
    return (vals[mid - 1] + vals[mid]) / 2.0
