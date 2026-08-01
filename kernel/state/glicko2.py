"""Glicko-2 rating for learners and items on a shared scale.

DESIGN.md §6.1 puts learner and item on one scale so item difficulty
calibrates itself from attempt data, removing the need to trust hand- or
model-labeled difficulty. §17 Q1 left the Elo variant open, leaning Glicko-2.

Glicko-2 it is, for one concrete reason: the `rating_deviation` margin policy
needs an explicit, maintained measurement uncertainty. Plain Elo has no such
quantity, so the margin would have to be invented. Glicko-2 also handles
sparse per-tag data honestly -- RD grows back during inactivity, which is
exactly right for a tag left alone for a month.

Updates are applied per attempt rather than per rating period. That is the
standard simplification for one-at-a-time play and keeps the study loop
stateless between items.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Conversion between the human-readable (1500-centered) scale and Glicko-2's
# internal scale.
SCALE = 173.7178
DEFAULT_RATING = 1500.0
DEFAULT_RD = 350.0
DEFAULT_VOLATILITY = 0.06

# System constant: constrains volatility change over time. Glickman recommends
# 0.3-1.2; smaller is steadier. Study data is noisier than chess, so stay low.
TAU = 0.5

# Convergence tolerance for the volatility solver.
EPSILON = 1e-6


@dataclass
class Rating:
    """A rating on the public (1500-centered) scale."""

    rating: float = DEFAULT_RATING
    rd: float = DEFAULT_RD
    volatility: float = DEFAULT_VOLATILITY

    @property
    def mu(self) -> float:
        return (self.rating - DEFAULT_RATING) / SCALE

    @property
    def phi(self) -> float:
        return self.rd / SCALE


def _g(phi: float) -> float:
    return 1.0 / math.sqrt(1.0 + 3.0 * phi * phi / (math.pi * math.pi))


def expected_score(player: Rating, opponent: Rating) -> float:
    """P(player wins) -- i.e. P(learner answers this item correctly)."""
    return 1.0 / (1.0 + math.exp(-_g(opponent.phi) * (player.mu - opponent.mu)))


def _solve_volatility(phi: float, sigma: float, v: float, delta: float) -> float:
    """Illinois-variant regula falsi, per Glickman's step 5."""
    a = math.log(sigma * sigma)
    phi_sq, delta_sq = phi * phi, delta * delta

    def f(x: float) -> float:
        ex = math.exp(x)
        denom = phi_sq + v + ex
        return (ex * (delta_sq - denom) / (2.0 * denom * denom)) - ((x - a) / (TAU * TAU))

    big_a = a
    if delta_sq > phi_sq + v:
        big_b = math.log(delta_sq - phi_sq - v)
    else:
        k = 1
        while f(a - k * TAU) < 0:
            k += 1
        big_b = a - k * TAU

    f_a, f_b = f(big_a), f(big_b)
    while abs(big_b - big_a) > EPSILON:
        c = big_a + (big_a - big_b) * f_a / (f_b - f_a)
        f_c = f(c)
        if f_c * f_b <= 0:
            big_a, f_a = big_b, f_b
        else:
            f_a /= 2.0
        big_b, f_b = c, f_c

    return math.exp(big_a / 2.0)


def update(player: Rating, opponent: Rating, score: float) -> Rating:
    """Return the player's post-attempt rating. `score` is 1.0 or 0.0.

    Call twice per attempt -- once for the learner with the item as opponent,
    once for the item with `1 - score`. That mirroring is what makes item
    difficulty self-calibrating.
    """
    g_opp = _g(opponent.phi)
    e = expected_score(player, opponent)

    v = 1.0 / (g_opp * g_opp * e * (1.0 - e))
    delta = v * g_opp * (score - e)

    sigma_new = _solve_volatility(player.phi, player.volatility, v, delta)

    # RD grows by the new volatility, then shrinks by the information gained.
    phi_star = math.sqrt(player.phi**2 + sigma_new**2)
    phi_new = 1.0 / math.sqrt(1.0 / (phi_star * phi_star) + 1.0 / v)
    mu_new = player.mu + phi_new * phi_new * g_opp * (score - e)

    return Rating(
        rating=DEFAULT_RATING + SCALE * mu_new,
        rd=min(SCALE * phi_new, DEFAULT_RD),
        volatility=sigma_new,
    )


def apply_attempt(learner: Rating, item: Rating, correct: bool) -> tuple[Rating, Rating]:
    """One attempt updates both sides. Returns (learner', item')."""
    score = 1.0 if correct else 0.0
    # Both updates read the pre-attempt state of the other side.
    return update(learner, item, score), update(item, learner, 1.0 - score)


def decay(rating: Rating, periods: float) -> Rating:
    """Grow RD for inactivity. A tag untouched for weeks is less certain."""
    phi = math.sqrt(rating.phi**2 + periods * rating.volatility**2)
    return Rating(rating.rating, min(SCALE * phi, DEFAULT_RD), rating.volatility)
