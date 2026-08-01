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
from kernel.pedagogy import capture as capture_mod
from kernel.pedagogy import errors
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

    # ---- checking
    if conn is not None:
        lines.extend(_checking_section(conn, state.learner_id, state.product_id))

    for note in objective_report.notes:
        lines.append(f"\n  note: {note}")

    if conn is not None:
        lines.append(_unresolved_note(conn, state.learner_id))

    return "\n".join(lines)


def _checking_section(
    conn: sqlite3.Connection, learner_id: str, product_id: str
) -> list[str]:
    """What checking bought, and what not checking cost. DESIGN.md §13.1.

    The `verification_method` capture field spent its whole life as a text box
    nothing read, which is the same as not collecting it. Closing it to a set
    made it countable; this is the count, and it is the only reason the field
    is worth the friction of asking.

    The headline is deliberately the narrow number -- wrong, unchecked, and
    coded as an error a check catches -- rather than the broad one. "You got
    11 wrong without checking" includes items no check would have saved,
    because you cannot verify your way out of not knowing the rule
    (`errors.CHECKABLE_CODES`). Overstating a real problem gets the whole
    section discounted, and this section only works if it is believed.

    Self-gating: a product with the field off has no rows here and prints
    nothing. No config is plumbed in to ask.
    """
    rows = conn.execute(
        """SELECT a.verification_method AS method,
                  a.correct             AS correct,
                  d.error_code          AS error_code
             FROM attempts a
        LEFT JOIN diagnoses d ON d.attempt_id = a.attempt_id
            WHERE a.learner_id = ? AND a.product_id = ?
              AND a.verification_method IS NOT NULL""",
        (learner_id, product_id),
    ).fetchall()
    if not rows:
        return []

    checked_n = checked_right = unchecked_n = unchecked_right = 0
    unreadable = preventable = 0
    method_counts: dict[str, int] = {}

    for row in rows:
        verdict = capture_mod.was_checked(row["method"])
        if verdict is None:
            unreadable += 1
            continue
        if verdict:
            checked_n += 1
            checked_right += int(bool(row["correct"]))
            for slug in row["method"].split(","):
                method_counts[slug] = method_counts.get(slug, 0) + 1
        else:
            unchecked_n += 1
            unchecked_right += int(bool(row["correct"]))
            if not row["correct"] and row["error_code"] in errors.CHECKABLE_CODES:
                preventable += 1

    if not checked_n and not unchecked_n:
        # Every row predates the closed set. Reporting 0% off nothing would be
        # a lie shaped like a statistic.
        return [
            "\nCHECKING",
            f"  No countable data yet -- all {unreadable} attempt(s) were "
            "recorded before this field became a fixed set.",
        ]

    lines = ["\nCHECKING  (untimed means checking was free -- this is what it bought)"]

    def _row(label: str, n: int, right: int) -> str:
        rate = f"{right / n:>6.0%}" if n else "     --"
        return f"  {label:<18}{n:>4} attempt(s){rate} correct"

    lines.append(_row("checked", checked_n, checked_right))
    lines.append(_row("did not check", unchecked_n, unchecked_right))

    if preventable:
        lines.append(
            f"  >>> {preventable} wrong after no check, and coded "
            f"{'/'.join(sorted(errors.CHECKABLE_CODES))} -- a check was the fix."
        )
    elif unchecked_n:
        lines.append(
            "  No unchecked miss was coded as an error a check catches, so far."
        )

    if method_counts:
        # Which habits actually exist. One method at high count and the rest
        # at zero is a learner with a single reflex, which is a different
        # problem from not checking at all and is invisible in the split above.
        ranked = sorted(method_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        used = ", ".join(
            f"{capture_mod.VERIFICATION_BY_SLUG[slug].label} {n}"
            for slug, n in ranked
        )
        lines.append(f"  methods used: {used}")
        never = [
            m.label
            for m in capture_mod.VERIFICATION_METHODS
            if m.slug != capture_mod.NO_CHECK and m.slug not in method_counts
        ]
        if never:
            lines.append(f"  never used:   {', '.join(never)}")

    if unreadable:
        lines.append(
            f"  ({unreadable} older attempt(s) excluded -- free text from "
            "before this field became a fixed set.)"
        )
    return lines


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
