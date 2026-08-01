"""Threshold objective: cross the line or don't. DESIGN.md §7.1.

The outcome is a step function -- 990 equals 951, 949 equals 910 -- so this
optimizes **P(crossing)**, not expected score. Three consequences fall out and
all three are implemented here rather than left to the caller:

1. The gradient concentrates near the boundary. Far above the cut every
   gradient goes to zero, because further study of a satisfied objective is
   worth exactly nothing. The base class handles that; §7.1 is why.
2. Routes are first-class. Groups are conjunctive (each is a separate
   requirement); routes within a group are alternatives.
3. `satisfied` requires the cut **plus a margin sized to measurement
   uncertainty**. This is the part people skip, and it is the whole reason
   threshold objectives get a crisp stopping rule at all.
"""

from __future__ import annotations

from typing import Any

from kernel.objectives import routes as routes_mod
from kernel.objectives.base import (
    GRADIENT_EPSILON,
    ObjectiveReport,
    Objective,
    RouteReport,
    register,
)
from kernel.state.view import StateView


@register
class ThresholdObjective(Objective):
    type_name = "threshold"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        raw = config.get("routes") or {}
        if not raw:
            raise ValueError("threshold objective requires at least one route group")
        # group -> [expression, ...]
        self.route_groups: dict[str, list[str]] = {
            group: list(exprs) for group, exprs in raw.items()
        }
        self.margin_policy: str = config.get("margin_policy", "rating_deviation")
        self.margin_multiplier: float = float(config.get("margin_multiplier", 1.0))
        self.fixed_margins: dict[str, float] = config.get("margins", {}) or {}
        for group, exprs in self.route_groups.items():
            for expr in exprs:
                routes_mod.parse(expr)  # fail loudly at load, not mid-session

    # ------------------------------------------------------------ margins

    def _margins(self, state: StateView) -> dict[str, float]:
        """Per-variable margin, in that variable's own units.

        `rating_deviation` sizes the margin from the current measurement
        uncertainty, so a learner with sparse data must clear the cut by more
        than one with a long, stable history. That is the intended behavior:
        the margin is a statement about how well we know the position, not
        about how good the learner is.
        """
        if self.margin_policy == "none":
            return {}
        if self.margin_policy == "fixed":
            return dict(self.fixed_margins)
        if self.margin_policy == "rating_deviation":
            return {
                name: est.sd * self.margin_multiplier
                for name, est in state.variables.items()
            }
        raise ValueError(f"unknown margin_policy {self.margin_policy!r}")

    # ---------------------------------------------------------- evaluation

    def _route_probabilities(self, state: StateView) -> dict[str, list[tuple[str, float]]]:
        return {
            group: [(expr, routes_mod.probability(expr, state)) for expr in exprs]
            for group, exprs in self.route_groups.items()
        }

    def progress(self, state: StateView) -> float:
        """P(all requirements met) via the best available route in each group.

        Within a group the maximum is used rather than a noisy-or: alternative
        routes to the same requirement are strongly correlated (they read the
        same underlying ability), so combining them would inflate P(success)
        exactly where the learner most needs an honest number.
        """
        p = 1.0
        for _group, scored in self._route_probabilities(state).items():
            p *= max((prob for _expr, prob in scored), default=0.0)
        return p

    def gradient(self, state: StateView, tag_slug: str) -> float:
        """Steepest per-route improvement, not the derivative of `progress`.

        The base class's numeric derivative of `progress` is wrong here, and
        wrong in a way that silently disables the whole allocator. `progress`
        multiplies across groups, so a single group sitting at exactly zero --
        an ELAR route gated on an essay score the learner has never recorded,
        say -- makes the product zero everywhere, and every partial derivative
        with it. The tool would then report "nothing to study" because of an
        unrelated missing data point.

        DESIGN.md §7.1 says the plugin evaluates P(success) per route and
        routes effort toward the steepest, so that is what is measured: the
        best marginal gain this tag buys on any single route.
        """
        if self.satisfied(state):
            # Further study of a satisfied objective is worth exactly nothing.
            return 0.0

        moved = state.perturb(tag_slug, GRADIENT_EPSILON)
        best = 0.0
        for exprs in self.route_groups.values():
            for expr in exprs:
                before = routes_mod.probability(expr, state)
                after = routes_mod.probability(expr, moved)
                best = max(best, (after - before) / GRADIENT_EPSILON)
        return best

    def unmeasured_variables(self, state: StateView) -> list[str]:
        """Declared manual variables the learner has never reported.

        These read as their default and can hold a route at zero without any
        study fixing it, so the report must name them rather than let the
        learner read a flat 0% as a verdict on their ability.
        """
        return sorted(
            name
            for name, spec in (state.var_declarations or {}).items()
            if spec.get("kind") == "manual" and name not in state.manual_values
        )

    def satisfied(self, state: StateView) -> bool:
        margins = self._margins(state)
        return all(
            any(
                routes_mod.satisfied_with_margin(expr, state, margins) for expr in exprs
            )
            for exprs in self.route_groups.values()
        )

    def report(self, state: StateView) -> ObjectiveReport:
        margins = self._margins(state)
        route_reports: list[RouteReport] = []
        notes: list[str] = []

        for group, scored in self._route_probabilities(state).items():
            for expr, prob in scored:
                used = routes_mod.variables_used(expr)
                primary = next(
                    (v for v in sorted(used) if v in state.variables), None
                )
                est = state.variables.get(primary) if primary else None
                route_reports.append(
                    RouteReport(
                        route_id=f"{group}:{expr}",
                        expression=expr,
                        p_success=prob,
                        position=est.value if est else None,
                        margin=margins.get(primary or "", 0.0),
                        satisfied=routes_mod.satisfied_with_margin(expr, state, margins),
                        detail={"group": group},
                    )
                )

        route_reports.sort(key=lambda r: r.p_success, reverse=True)
        done = self.satisfied(state)

        if done:
            # DESIGN.md principle 9: the tool must be able to say stop.
            notes.append(
                "Objective satisfied with margin. Further study of this objective "
                "has zero gradient -- the allocator will return nothing."
            )
        else:
            steepest = route_reports[0] if route_reports else None
            if steepest is not None:
                notes.append(f"Closest route: {steepest.expression}")

        unmeasured = self.unmeasured_variables(state)
        if unmeasured:
            notes.append(
                "never recorded, so any route reading them sits at 0% regardless "
                f"of study: {', '.join(unmeasured)}  (fix with `study set <name> <value>`)"
            )

        starved = [t.slug for t in state.tags.values() if t.items_in_band == 0]
        if starved:
            notes.append(
                f"{len(starved)} tag(s) have no items in band -- content acquisition "
                f"signal, not a skip: {', '.join(sorted(starved)[:5])}"
            )

        progress = self.progress(state)
        return ObjectiveReport(
            objective_type=self.type_name,
            progress=progress,
            satisfied=done,
            headline=f"P(pass) = {progress:.1%}" + ("  [SATISFIED]" if done else ""),
            routes=route_reports,
            notes=notes,
        )
