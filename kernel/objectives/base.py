"""The objective plugin interface. DESIGN.md §7.

An objective answers four questions and nothing else:

    progress(state)       -> [0, 1]   where am I?
    gradient(state, tag)  -> float    marginal value of improving this tag
    satisfied(state)      -> bool     may I stop?
    report(state)         -> summary  human-readable position

Keeping the surface this small is what makes DESIGN.md principle 3 checkable:
threshold logic never leaks into the scheduler, scheduler logic never leaks
into an objective. Anything an objective wants to know that isn't in StateView
is a signal the view is wrong, not that the interface should grow.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from kernel.state.view import StateView

# Rating units used for the numeric gradient. Small enough to be local, large
# enough to survive float noise through a normal CDF.
GRADIENT_EPSILON = 10.0


@dataclass
class RouteReport:
    route_id: str
    expression: str
    p_success: float
    position: float | None = None
    margin: float | None = None
    satisfied: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class ObjectiveReport:
    objective_type: str
    progress: float
    satisfied: bool
    headline: str
    routes: list[RouteReport] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Objective(ABC):
    """Base class for all objectives.

    Subclasses receive their configuration dict from the product's
    objective.yaml. No objective may read product identity or exam structure
    beyond what that config hands it.
    """

    type_name: str = "abstract"

    def __init__(self, config: dict[str, Any]):
        self.config = config

    @abstractmethod
    def progress(self, state: StateView) -> float:
        """Position in [0, 1]. Not percent-complete-of-syllabus; percent of goal."""

    @abstractmethod
    def satisfied(self, state: StateView) -> bool:
        """The stopping rule. DESIGN.md principle 9: a satisfied objective reads zero."""

    @abstractmethod
    def report(self, state: StateView) -> ObjectiveReport:
        """Human-readable position, including per-route detail where routes exist."""

    def gradient(self, state: StateView, tag_slug: str) -> float:
        """Marginal goal value of improving `tag_slug`, per rating point.

        The default is a numeric derivative of `progress`. Objectives with a
        cheap analytic form may override; most should not bother, because the
        numeric version is correct by construction and this is not the hot
        path.
        """
        if self.satisfied(state):
            # Further study of a satisfied objective is worth exactly nothing,
            # and the plugin says so. DESIGN.md §7.1.
            return 0.0
        base = self.progress(state)
        moved = self.progress(state.perturb(tag_slug, GRADIENT_EPSILON))
        return max(0.0, (moved - base) / GRADIENT_EPSILON)


_REGISTRY: dict[str, type[Objective]] = {}


def register(cls: type[Objective]) -> type[Objective]:
    _REGISTRY[cls.type_name] = cls
    return cls


def build(config: dict[str, Any]) -> Objective:
    """Instantiate the objective named by `config['type']`.

    `deadline` wraps rather than replaces, so it is resolved here too --
    composition (`deadline(threshold(...))`) keeps calendar pressure out of
    every individual objective's logic. DESIGN.md §7.4.
    """
    # Import for side-effect registration; avoids a circular import at module load.
    from kernel.objectives import deadline, mastery, maximize, threshold  # noqa: F401

    kind = config.get("type")
    if kind not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY)) or "none loaded"
        raise ValueError(f"unknown objective type {kind!r}; known types: {known}")
    return _REGISTRY[kind](config)
