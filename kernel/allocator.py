"""The allocator. DESIGN.md §8.

    priority(T) = gradient(T) x learnability(T) x availability(T)

This is the entire replacement for a curriculum. Its output *is* the
personalized syllabus, recomputed after every attempt. Nothing here knows what
is being studied.

Each term earns its place:

- **gradient** is goal leverage, and the only term the objective supplies.
- **learnability** is position on the learning curve. It peaks where predicted
  P(correct) is 0.6-0.8 -- hard enough to teach, solvable enough to
  consolidate. Below ~0.3 the prerequisites are missing, and serving the tag
  itself would be the sideways remediation this system exists to avoid, so the
  DAG routes to parents instead. Above ~0.85 the time buys confirmation of the
  known.
- **availability** is the honest term. If a high-priority tag has no items in
  the target band that is a *content acquisition* signal surfaced in the
  report, never a silent skip.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kernel.objectives.base import Objective
from kernel.state import glicko2
from kernel.state.view import StateView, TagState

# Learnability curve. DESIGN.md §8.
LEARNABLE_LO = 0.60
LEARNABLE_HI = 0.80
PREREQ_FLOOR = 0.30
CONFIRMATION_CEILING = 0.85


@dataclass
class Allocation:
    tag_slug: str
    priority: float
    gradient: float
    learnability: float
    availability: float
    predicted_p_correct: float
    target_band: tuple[float, float]
    # Set when the DAG diverted this recommendation to a prerequisite.
    routed_from: str | None = None
    reasons: list[str] = field(default_factory=list)

    @property
    def starved(self) -> bool:
        return self.availability == 0.0


def predicted_p_correct(tag: TagState, item_rating: float) -> float:
    """P(this learner solves an item at `item_rating`) for this tag."""
    learner = glicko2.Rating(tag.rating, tag.rd)
    item = glicko2.Rating(item_rating, glicko2.DEFAULT_RD / 2)
    return glicko2.expected_score(learner, item)


# Offsets below the learner's rating where P(correct) lands in 0.6-0.8, and
# how much the band widens per point of rating deviation.
BAND_CENTER_OFFSET = 155.0
BAND_HALF_WIDTH = 85.0


def target_band(tag: TagState) -> tuple[float, float]:
    """Item rating range that lands in the learnable zone for this tag.

    Inverting the Glicko expectation: P(correct) = 0.6-0.8 puts the learner
    roughly 70-240 points above the item. But that band is only meaningful once
    the learner's position is actually known. On an untouched tag (RD 350) the
    system has no idea where they are, so a narrow band is false precision --
    and worse, it makes the availability term report starvation for a corpus
    that is full of usable items. The band therefore widens with RD and
    tightens to the nominal range as evidence accumulates.
    """
    center = tag.rating - BAND_CENTER_OFFSET
    half = BAND_HALF_WIDTH + tag.rd
    return (center - half, center + half)


def learnability(tag: TagState) -> tuple[float, str | None]:
    """Multiplier in [0, 1] plus a reason when the tag is out of the zone."""
    p = predicted_p_correct(tag, (tag.rating - 155.0))

    if p < PREREQ_FLOOR:
        return 0.0, "below the prerequisite floor -- route to parents instead"
    if p > CONFIRMATION_CEILING:
        # Not zero: consolidation still has some value, just very little.
        return 0.15, "above the confirmation ceiling -- mostly re-proving the known"
    if LEARNABLE_LO <= p <= LEARNABLE_HI:
        return 1.0, None
    # Linear falloff on either side of the plateau.
    if p < LEARNABLE_LO:
        return (p - PREREQ_FLOOR) / (LEARNABLE_LO - PREREQ_FLOOR), None
    return 1.0 - (p - LEARNABLE_HI) / (CONFIRMATION_CEILING - LEARNABLE_HI) * 0.85, None


def availability(tag: TagState) -> float:
    """Do items exist in the band? Saturates fast -- a few is enough to serve."""
    if tag.items_in_band <= 0:
        return 0.0
    return min(1.0, tag.items_in_band / 5.0)


def _route_to_prerequisites(
    tag_slug: str, state: StateView, edges: dict[str, list[tuple[str, float]]]
) -> list[tuple[str, float]]:
    """Highest-confidence prerequisites of a tag that is below the floor."""
    parents = sorted(edges.get(tag_slug, []), key=lambda pc: pc[1], reverse=True)
    return [(slug, conf) for slug, conf in parents if slug in state.tags]


def rank(
    state: StateView,
    objective: Objective,
    edges: dict[str, list[tuple[str, float]]] | None = None,
    limit: int = 10,
) -> list[Allocation]:
    """Rank tags by priority. Empty result means the objective is satisfied.

    A satisfied objective reads zero on every tag (DESIGN.md principle 9), so
    an empty list here is the tool saying *stop studying* -- it is a result,
    not a failure.
    """
    edges = edges or {}
    allocations: list[Allocation] = []

    for slug, tag in state.tags.items():
        gradient = objective.gradient(state, slug)
        learn, reason = learnability(tag)
        avail = availability(tag)
        p = predicted_p_correct(tag, tag.rating - 155.0)

        if learn == 0.0 and reason:
            # Below the floor: serve the prerequisites, not the tag.
            for parent_slug, confidence in _route_to_prerequisites(slug, state, edges):
                parent = state.tags[parent_slug]
                p_learn, _ = learnability(parent)
                allocations.append(
                    Allocation(
                        tag_slug=parent_slug,
                        priority=gradient * p_learn * availability(parent) * confidence,
                        gradient=gradient,
                        learnability=p_learn,
                        availability=availability(parent),
                        predicted_p_correct=predicted_p_correct(
                            parent, parent.rating - 155.0
                        ),
                        target_band=target_band(parent),
                        routed_from=slug,
                        reasons=[f"{slug} is {reason}"],
                    )
                )
            continue

        reasons = [reason] if reason else []
        if avail == 0.0:
            reasons.append("no items in band -- content acquisition needed")

        allocations.append(
            Allocation(
                tag_slug=slug,
                priority=gradient * learn * avail,
                gradient=gradient,
                learnability=learn,
                availability=avail,
                predicted_p_correct=p,
                target_band=target_band(tag),
                reasons=reasons,
            )
        )

    # Collapse duplicates introduced by prerequisite routing, keeping the best.
    best: dict[str, Allocation] = {}
    for alloc in allocations:
        prior = best.get(alloc.tag_slug)
        if prior is None or alloc.priority > prior.priority:
            best[alloc.tag_slug] = alloc

    ranked = sorted(best.values(), key=lambda a: a.priority, reverse=True)
    return ranked[:limit]


def starved_tags(allocations: list[Allocation]) -> list[Allocation]:
    """Tags the objective wants but the corpus cannot serve.

    This list *is* the content-acquisition backlog, ordered by the objective.
    Corpus building becomes goal-directed rather than completionist.
    """
    return [a for a in allocations if a.starved and a.gradient > 0]
