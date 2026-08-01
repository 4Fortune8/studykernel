"""Maximize objective: every point has value. DESIGN.md §7.2.

Scheduled for v2 alongside the second product, and deliberately minimal here.
What exists is the part that proves the seam -- a working gradient that is
*shaped differently from* threshold's, so the two cannot quietly collapse into
each other:

- threshold's gradient concentrates at the boundary and goes to zero above it
- maximize's gradient never reaches zero; it only diminishes

That difference is the whole argument of §7.5. Collapsing every goal into
threshold form would be false, because a threshold objective correctly
abandons satisfied domains and maximize must not.

Not implemented (v2, per §16 and Part V):
- fragility (slow-but-correct) weighting, which on a timed section-adaptive
  exam *is* the score ceiling and is invisible to accuracy metrics
- per-section targets and section-adaptive scoring
"""

from __future__ import annotations

import math
from typing import Any

from kernel.objectives.base import ObjectiveReport, Objective, register
from kernel.state.view import StateView


@register
class MaximizeObjective(Objective):
    type_name = "maximize"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        # Optional soft targets; absence is fine -- maximize has no natural
        # stopping rule, which is exactly why `deadline` exists to wrap it.
        self.targets: dict[str, float] = config.get("targets", {}) or {}
        self.saturation: float = float(config.get("saturation_rating", 1800.0))

    def progress(self, state: StateView) -> float:
        """Blueprint-weighted mean ability, squashed into [0, 1].

        Diminishing returns are in the squash, not in the stopping rule: this
        keeps rising forever, which is the honest shape for a goal where more
        points are always better.
        """
        tags = list(state.tags.values())
        if not tags:
            return 0.0
        total_weight = sum(t.coverage_weight for t in tags) or 1.0
        weighted = sum(t.rating * t.coverage_weight for t in tags) / total_weight
        return 1.0 / (1.0 + math.exp(-(weighted - self.saturation) / 150.0))

    def satisfied(self, state: StateView) -> bool:
        """No natural stopping rule. Targets are advisory; deadlines are real.

        DESIGN.md §7.2: `satisfied` here is deadline- or target-driven. With no
        targets configured this is permanently False, and that is correct
        rather than a gap -- an unbounded goal does not get to claim it is done.
        """
        if not self.targets:
            return False
        return all(
            state.variables.get(name) is not None
            and state.variables[name].value >= target
            for name, target in self.targets.items()
        )

    def report(self, state: StateView) -> ObjectiveReport:
        progress = self.progress(state)
        notes = [
            f"{name}: {state.variables[name].value:.0f} / target {target:.0f}"
            for name, target in self.targets.items()
            if name in state.variables
        ]
        if not self.targets:
            notes.append(
                "No targets configured -- this objective never reports satisfied. "
                "Wrap it in `deadline` to get a stopping rule."
            )
        notes.append("Fragility weighting is not implemented (v2).")
        return ObjectiveReport(
            objective_type=self.type_name,
            progress=progress,
            satisfied=self.satisfied(state),
            headline=f"weighted position {progress:.1%} of saturation",
            notes=notes,
        )
