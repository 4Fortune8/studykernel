"""`deadline(inner)` -- a wrapper, not a peer. DESIGN.md §7.4.

Takes any objective plus a date and reshapes its gradient: as time runs out,
consolidating near-threshold tags beats opening new fronts. Composition is the
point -- it keeps calendar pressure out of every individual objective's logic,
so no objective ever grows a `days_remaining` branch.

Config shape:

    objective:
      type: deadline
      date: 2026-09-15
      inner:
        type: threshold
        routes: {...}

The consolidation weighting is live; FSRS review compression (the other half
of §7.4) is a v1 item and is noted in the report rather than faked.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from kernel.objectives.base import ObjectiveReport, Objective, build, register
from kernel.state.view import StateView

# Below this many days out, consolidation weighting starts to bite.
HORIZON_DAYS = 60.0


@register
class DeadlineObjective(Objective):
    type_name = "deadline"

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        inner_cfg = config.get("inner")
        if not inner_cfg:
            raise ValueError("deadline objective requires an `inner` objective")
        self.inner: Objective = build(inner_cfg)
        raw_date = config["date"]
        self.date: date = (
            raw_date if isinstance(raw_date, date) else datetime.fromisoformat(str(raw_date)).date()
        )

    def days_remaining(self, today: date | None = None) -> int:
        return max(0, (self.date - (today or date.today())).days)

    def _urgency(self) -> float:
        """0.0 when far out, approaching 1.0 at the deadline."""
        days = self.days_remaining()
        return max(0.0, min(1.0, 1.0 - days / HORIZON_DAYS))

    def progress(self, state: StateView) -> float:
        return self.inner.progress(state)

    def satisfied(self, state: StateView) -> bool:
        return self.inner.satisfied(state)

    def gradient(self, state: StateView, tag_slug: str) -> float:
        """Inner gradient, reweighted toward tags that are close to done.

        Near the deadline a tag with a nearly-cleared reliability bar is worth
        more per minute than an untouched one of equal raw gradient, because
        there is time to finish the first and not the second. Far from the
        deadline the reweighting vanishes and the inner objective's ordering
        stands unmodified.
        """
        base = self.inner.gradient(state, tag_slug)
        if base <= 0.0:
            return base
        urgency = self._urgency()
        if urgency <= 0.0:
            return base
        tag = state.tags.get(tag_slug)
        if tag is None:
            return base
        # Consolidation: how far along this tag already is, in [0, 1].
        maturity = min(1.0, tag.reliability_lo / state.mastery_bar) if state.mastery_bar else 0.0
        return base * (1.0 - urgency + urgency * (0.25 + 1.5 * maturity))

    def report(self, state: StateView) -> ObjectiveReport:
        inner = self.inner.report(state)
        days = self.days_remaining()
        inner.notes.insert(0, f"{days} day(s) to {self.date.isoformat()}.")
        if self._urgency() > 0:
            inner.notes.insert(
                1,
                "Consolidation weighting active: near-threshold tags outrank new fronts.",
            )
        inner.notes.append("FSRS review compression under deadline is not implemented (v1).")
        inner.headline = f"{inner.headline}  ({days}d left)"
        return inner
