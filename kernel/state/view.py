"""The learner state an objective sees.

Objectives and the allocator both read this and nothing else. It is the seam
that keeps DESIGN.md §5's rule enforceable: a view carries tags, sections and
*named derived variables*, but never knows what any of them mean. The product
pack decides that a variable called `crc_estimate` exists and how it is
computed; the kernel only knows it is a number with an uncertainty.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Estimate:
    """A scalar with measurement uncertainty. Never a bare point estimate."""

    value: float
    sd: float = 0.0

    def p_at_least(self, threshold: float) -> float:
        """P(true value >= threshold), normal approximation.

        With sd == 0 this collapses to a step function, which is the right
        behavior for variables the learner reports directly (an essay score
        already earned is not uncertain).
        """
        if self.sd <= 0:
            return 1.0 if self.value >= threshold else 0.0
        z = (self.value - threshold) / self.sd
        return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


@dataclass(frozen=True)
class TagState:
    slug: str
    section: str
    rating: float
    rd: float
    n_attempts: int = 0
    successes: int = 0
    reliability_lo: float = 0.0
    reliability_point: float = 0.0
    variance: float = 0.0
    coverage_weight: float = 1.0
    level: int | None = None
    parent_slug: str | None = None
    # How many items exist in this tag's target band. Populated by the
    # allocator's availability query; 0 means the corpus cannot serve the tag.
    items_in_band: int = 0


@dataclass(frozen=True)
class SectionState:
    slug: str
    blueprint_weight: float = 1.0
    explanation_policy: str = "withheld"


@dataclass(frozen=True)
class StateView:
    learner_id: str
    product_id: str
    tags: dict[str, TagState] = field(default_factory=dict)
    sections: dict[str, SectionState] = field(default_factory=dict)
    # Named variables the objective's route expressions refer to. Computed by
    # kernel.state.variables from the product's declarations.
    variables: dict[str, Estimate] = field(default_factory=dict)
    # Per-domain values for aggregate predicates like all_domains(...).
    domain_variables: dict[str, dict[str, Estimate]] = field(default_factory=dict)
    mastery_bar: float = 0.8

    # Product declarations, carried so recomputation stays a pure function of
    # the view. The kernel reads their *shape*, never their meaning.
    var_declarations: dict[str, dict] = field(default_factory=dict)
    domain_declarations: dict = field(default_factory=dict)
    # Learner-reported scalars (a practice essay score, a mock section score).
    manual_values: dict[str, float] = field(default_factory=dict)

    def tags_in_section(self, section: str) -> list[TagState]:
        return [t for t in self.tags.values() if t.section == section]

    def perturb(self, tag_slug: str, rating_delta: float) -> StateView:
        """Return a copy with one tag's ability nudged upward.

        This is how gradients are taken: numerically, by asking the objective
        what it would say if this one tag improved. It costs an extra objective
        evaluation per tag and buys the ability to add objectives without
        anyone deriving a partial derivative by hand.
        """
        tag = self.tags.get(tag_slug)
        if tag is None:
            return self
        moved = replace(tag, rating=tag.rating + rating_delta)
        tags = dict(self.tags)
        tags[tag_slug] = moved
        # Derived variables must be recomputed against the moved tag, which
        # only the variables module knows how to do. It re-enters here.
        from kernel.state import variables

        return variables.recompute(replace(self, tags=tags))
