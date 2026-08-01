"""Mastery objective: retention of a body of knowledge, no exam at all.

DESIGN.md §7.3 -- this plugin is the existence proof that the kernel is a
*studying* system rather than a *test-prep* system. §17 Q6 settles its
schedule: ship the interface in v0, the implementation when a real use case
exists. This file is that decision, honored literally.

`progress` and `satisfied` work now, because they need only the reliability
bar the kernel already computes. `gradient` needs FSRS retrievability, which
is a v1 milestone item, and raises rather than guessing -- a plausible-looking
wrong gradient would silently misallocate every session, which is the failure
mode DESIGN.md §10 calls the worst one because it is invisible.
"""

from __future__ import annotations

from typing import Any

from kernel.objectives.base import ObjectiveReport, Objective, register
from kernel.state.view import StateView


@register
class MasteryObjective(Objective):
    type_name = "mastery"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.bar: float = float(config.get("reliability_bar", 0.85))

    def _cleared(self, state: StateView) -> list[str]:
        return [t.slug for t in state.tags.values() if t.reliability_lo >= self.bar]

    def progress(self, state: StateView) -> float:
        if not state.tags:
            return 0.0
        return len(self._cleared(state)) / len(state.tags)

    def satisfied(self, state: StateView) -> bool:
        """All tags above the reliability bar with stable retention.

        The retention half of that sentence is not yet checkable, so this
        reports False even at full coverage rather than claiming a completion
        it cannot verify.
        """
        return False

    def gradient(self, state: StateView, tag_slug: str) -> float:
        raise NotImplementedError(
            "mastery.gradient needs FSRS retrievability (v1 milestone). "
            "The interface ships in v0 per DESIGN.md §17 Q6; the implementation "
            "waits for a real use case -- post-exam retention is the natural first one."
        )

    def report(self, state: StateView) -> ObjectiveReport:
        cleared = self._cleared(state)
        return ObjectiveReport(
            objective_type=self.type_name,
            progress=self.progress(state),
            satisfied=False,
            headline=f"{len(cleared)}/{len(state.tags)} tags above reliability bar {self.bar:.2f}",
            notes=[
                "FSRS retention scheduling is not implemented (v1); this objective "
                "cannot yet be allocated against."
            ],
        )
