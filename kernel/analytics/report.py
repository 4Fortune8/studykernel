"""Report composition. DESIGN.md §16 v0: position estimate, per-route
P(success), domain reliability table.

The report has one job beyond information: it must be able to say **stop**.
Principle 9 -- a satisfied objective reads zero, and a study tool that cannot
tell you that you are done is an engagement product.
"""

from __future__ import annotations

import sqlite3

from kernel.allocator import Allocation, starved_tags
from kernel.objectives.base import ObjectiveReport
from kernel.state.view import StateView

BAR_WIDTH = 24


def _bar(value: float, width: int = BAR_WIDTH) -> str:
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "#" * filled + "." * (width - filled)


def render(
    state: StateView,
    objective_report: ObjectiveReport,
    allocations: list[Allocation],
    conn: sqlite3.Connection | None = None,
) -> str:
    lines: list[str] = []
    lines.append("=" * 68)
    lines.append(f"  {state.product_id}  --  {objective_report.headline}")
    lines.append("=" * 68)

    if objective_report.satisfied:
        lines.append("")
        lines.append("  *** OBJECTIVE SATISFIED WITH MARGIN. STOP STUDYING. ***")
        lines.append("")

    # ---- position
    if state.variables:
        lines.append("\nPOSITION")
        for name, est in sorted(state.variables.items()):
            sd = f" +/- {est.sd:.0f}" if est.sd else ""
            lines.append(f"  {name:<24} {est.value:>8.1f}{sd}")

    # ---- routes
    if objective_report.routes:
        lines.append("\nROUTES  (P(success) per route, steepest first)")
        for route in objective_report.routes:
            mark = "OK " if route.satisfied else "   "
            lines.append(
                f"  {mark}{_bar(route.p_success)}  {route.p_success:>6.1%}  "
                f"{route.expression}"
            )

    # ---- reliability
    lines.append("\nRELIABILITY  (lower bound of 95% CI -- the mastery bar sits here)")
    lines.append(f"  {'tag':<28}{'rating':>8}{'n':>5}{'rel_lo':>9}{'var':>9}{'band':>7}")
    for tag in sorted(state.tags.values(), key=lambda t: t.reliability_lo):
        band = "--" if tag.items_in_band == 0 else str(tag.items_in_band)
        lines.append(
            f"  {tag.slug:<28}{tag.rating:>8.0f}{tag.n_attempts:>5}"
            f"{tag.reliability_lo:>9.2f}{tag.variance:>9.0f}{band:>7}"
        )

    # ---- what to do next
    lines.append("\nNEXT  (gradient x learnability x availability)")
    servable = [a for a in allocations if a.priority > 0]
    if not servable:
        if objective_report.satisfied:
            lines.append("  Nothing. The objective is met -- this is the answer, not a bug.")
        else:
            lines.append(
                "  Nothing servable. Every prioritized tag is starved of items; "
                "see the acquisition backlog below."
            )
    for alloc in servable[:8]:
        routed = f"  (via {alloc.routed_from})" if alloc.routed_from else ""
        lines.append(
            f"  {alloc.priority:>8.4f}  {alloc.tag_slug}{routed}   "
            f"p(correct)~{alloc.predicted_p_correct:.2f}  "
            f"band {alloc.target_band[0]:.0f}-{alloc.target_band[1]:.0f}"
        )
        for reason in alloc.reasons:
            lines.append(f"            - {reason}")

    # ---- content backlog
    starved = starved_tags(allocations)
    if starved:
        lines.append("\nCONTENT ACQUISITION BACKLOG  (wanted by the objective, unservable)")
        lines.append("  This list is ordered by the goal. Acquire down it, then stop.")
        for alloc in starved[:8]:
            lines.append(
                f"  {alloc.tag_slug:<28} needs items in band "
                f"{alloc.target_band[0]:.0f}-{alloc.target_band[1]:.0f}"
            )

    for note in objective_report.notes:
        lines.append(f"\n  note: {note}")

    if conn is not None:
        lines.append(_unresolved_note(conn, state.learner_id))

    return "\n".join(lines)


def _unresolved_note(conn: sqlite3.Connection, learner_id: str) -> str:
    """Attempts whose exchange never happened, and those a reader rejected.

    These are different things and the note used to conflate them, reporting
    both as "never cleared the explain-back gate". That was wrong in the
    common case: the *local* gate is what lets an attempt exist at all
    (`session.submit_explain_back` persists nothing until it passes), so an
    attempt with `resolved = 0` has almost always cleared it and is merely
    waiting on a reader. Saying otherwise nags about work that was done, which
    is the fastest way to teach someone to ignore the notes.

    Waived attempts are counted in neither. Declining the exchange is an end
    state (DESIGN.md §10), not an omission.
    """
    row = conn.execute(
        """SELECT
             SUM(CASE WHEN d.attempt_id IS NULL AND a.exchange_waived_at IS NULL
                      THEN 1 ELSE 0 END) AS open_n,
             SUM(CASE WHEN d.explain_back_ok = 0 THEN 1 ELSE 0 END) AS rejected_n
           FROM attempts a
      LEFT JOIN diagnoses d ON d.attempt_id = a.attempt_id
          WHERE a.learner_id = ?""",
        (learner_id,),
    ).fetchone()

    notes = []
    if int(row["open_n"] or 0):
        notes.append(
            f"\n  note: {int(row['open_n'])} attempt(s) awaiting a tutoring exchange "
            "(the briefing is kept; `study history` lists them)."
        )
    if int(row["rejected_n"] or 0):
        notes.append(
            f"\n  note: {int(row['rejected_n'])} explain-back(s) a reader rejected "
            "-- those attempts stay unresolved."
        )
    return "".join(notes)
