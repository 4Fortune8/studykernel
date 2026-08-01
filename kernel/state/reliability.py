"""Reliability -- the proficiency measure. DESIGN.md §6.2.

Competence is "can solve it at all". Proficiency is "solves it reliably and
recognizes when it applies". The mastery bar sits on the *lower bound* of a
binomial confidence interval, never the point estimate: two-in-a-row means
nothing, and putting the CI in the code makes that structural rather than a
matter of discipline.

Wilson score interval, because it stays sane at small n and at proportions
near 0 or 1 -- which is precisely where a study log lives. The normal
approximation would hand back a lower bound of 1.0 after two correct answers,
i.e. exactly the failure this measure exists to prevent.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# 1.96 = 95% two-sided. The bar is applied to `lo`, so the effective claim is
# one-sided at 97.5% -- deliberately conservative.
DEFAULT_Z = 1.96


@dataclass(frozen=True)
class Interval:
    lo: float
    point: float
    hi: float
    n: int

    @property
    def width(self) -> float:
        return self.hi - self.lo


def wilson(successes: int, n: int, z: float = DEFAULT_Z) -> Interval:
    """Wilson score interval for a binomial proportion."""
    if n <= 0:
        return Interval(0.0, 0.0, 1.0, 0)

    p = successes / n
    z2 = z * z
    denom = 1.0 + z2 / n
    center = (p + z2 / (2 * n)) / denom
    spread = (z / denom) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return Interval(max(0.0, center - spread), p, min(1.0, center + spread), n)


def meets_bar(successes: int, n: int, bar: float, z: float = DEFAULT_Z) -> bool:
    """Mastery test. The lower bound must clear the bar, not the point estimate.

    This is why a hot streak cannot fake mastery: a short run leaves the
    interval too wide for its floor to reach the bar, however clean the run.
    """
    return wilson(successes, n, z).lo >= bar


def trigger_miss_rate(missed: int, applicable: int) -> float:
    """Second proficiency signal: knowing a rule but not recognizing its cue.

    The competence/proficiency gap made measurable (DESIGN.md §6.2). Returns
    0.0 when the tag has never been the applicable one -- no evidence of a gap
    is not evidence of none, and the allocator reads the CI width for that.
    """
    return missed / applicable if applicable > 0 else 0.0
