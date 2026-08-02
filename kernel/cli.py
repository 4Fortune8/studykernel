"""`study` -- the command line for the drill loop.

The loop is: capture (blind) -> grade (deterministic) -> tutor briefing (out)
-> record (in). DESIGN.md §16 v0.

The loop itself lives in `kernel/session.py`; this module is the terminal
adapter over it. Anything here that decides something rather than prompting
for it or printing it belongs on the other side of that seam.

Principle 10 governs the interaction design: friction on the diagnostic loop
is fatal, and every step here has to justify itself. The explain-back gate is
the sole exception, because there the friction *is* the mechanism.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

from kernel import allocator, config, session
from kernel.analytics import report as report_mod
from kernel.exchange import record as record_mod
from kernel.exchange import relay
from kernel.objectives import base as objective_base
from kernel.pedagogy import capture as capture_mod
from kernel.pedagogy import grading, hints
from kernel.storage import db

DEFAULT_LEARNER = "me"


def _product_dir(args: argparse.Namespace) -> Path:
    return Path(args.product)


def _load(args: argparse.Namespace):
    product = config.load_product(_product_dir(args))
    conn = db.connect(args.db)
    return product, conn


def _state_and_objective(product: dict, conn, learner: str):
    state = db.load_state(conn, learner, product["product_id"], product)
    objective = objective_base.build(product["objective"])
    return state, objective


def _prompt_verification() -> str:
    """Ask the verification question as a menu rather than a blank line.

    The field is a closed set, and a closed set typed from memory is a set
    typed wrong. Numbers are accepted alongside slugs because "1,4" is the
    whole point of closing it, and the loop re-asks instead of returning a
    bad value -- a rejected capture aborts the drill, and losing a drill to a
    misspelt slug is the friction DESIGN.md principle 10 warns about.

    The explanations print with the menu. On the web they are on hover; here
    there is nowhere to hover, and an unread option is a randomly ticked one.
    """
    methods = capture_mod.VERIFICATION_METHODS
    print("\nHow did you check it? Numbers or slugs, comma-separated.")
    for i, method in enumerate(methods, 1):
        print(f"  {i}. {method.label} ({method.slug})")
        print(f"     {method.detail}")

    while True:
        raw = input("> ").strip()
        tokens = [t.strip() for t in raw.split(",") if t.strip()]
        if not tokens:
            print("Pick at least one -- or the last option, if you didn't check.")
            continue
        resolved, bad = [], []
        for token in tokens:
            if token.isdigit() and 1 <= int(token) <= len(methods):
                resolved.append(methods[int(token) - 1].slug)
            elif token in capture_mod.VERIFICATION_BY_SLUG:
                resolved.append(token)
            else:
                bad.append(token)
        if bad:
            print(f"Not on the list: {', '.join(bad)}. Try again.")
            continue
        if capture_mod.NO_CHECK in resolved and len(resolved) > 1:
            print("Either you checked it or you didn't -- not both. Try again.")
            continue
        return ",".join(resolved)


# --------------------------------------------------------------- commands


def cmd_init(args: argparse.Namespace) -> int:
    product = config.load_product(_product_dir(args))
    conn = db.connect(args.db)
    db.migrate(conn)
    db.upsert_product(conn, product, config.digest(_product_dir(args)))
    db.upsert_taxonomy(
        conn, product["product_id"], product["_tags"], product["_edges"]
    )
    db.ensure_learner(conn, args.learner)
    print(
        f"initialized {args.db} for product {product['product_id']!r}: "
        f"{len(product['_tags'])} tags, {len(product['_edges'])} seed edge(s)"
    )
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Load a JSONL file of normalized item records produced by an importer."""
    product, conn = _load(args)
    path = Path(args.file)
    records = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            rec = json.loads(line)
            rec.setdefault("product_id", product["product_id"])
            records.append(rec)

    for rec in records:
        if rec.get("passage"):
            db.insert_passage(conn, rec["passage"])
    inserted, deduped = db.insert_items(conn, records)
    print(f"ingested {inserted} item(s); {deduped} duplicate(s) skipped")
    return 0


def cmd_next(args: argparse.Namespace) -> int:
    product, conn = _load(args)
    state, objective = _state_and_objective(product, conn, args.learner)
    edges = db.load_edges(conn, product["product_id"])
    ranked = allocator.rank(state, objective, edges, limit=args.limit)

    if objective.satisfied(state):
        print("Objective satisfied with margin. Stop studying.")
        return 0
    if not ranked or ranked[0].priority <= 0:
        print("Nothing servable -- run `study report` for the acquisition backlog.")
        return 1

    for alloc in ranked:
        if alloc.priority <= 0:
            continue
        via = f"  (routed from {alloc.routed_from})" if alloc.routed_from else ""
        print(
            f"{alloc.priority:>8.4f}  {alloc.tag_slug}{via}\n"
            f"          gradient={alloc.gradient:.6f} learnability={alloc.learnability:.2f} "
            f"availability={alloc.availability:.2f}"
        )
    return 0


def cmd_drill(args: argparse.Namespace) -> int:
    """Terminal adapter over `session.DrillSession`. Prompting only, no logic.

    Every decision here belongs to the service: this function asks questions,
    prints answers, and owns exactly one thing the service does not -- writing
    the briefing to a file, because the kernel does no file I/O.
    """
    product, conn = _load(args)
    drill = session.DrillSession(conn, product, args.learner)

    served = drill.start(tag=args.tag, section=args.section)
    if isinstance(served, session.Satisfied):
        print(served.message)
        return 0
    if isinstance(served, session.Starved):
        print(served.reason)
        return 1

    print("=" * 68)
    print(f"tag: {served.tag_slug}    item: {served.item_id}")
    print("=" * 68)
    if served.passage:
        print(f"\n{served.passage}\n")
    print(served.stem)
    if served.choices:
        for letter, choice in zip("ABCDEFGH", served.choices, strict=False):
            print(f"  {letter}. {choice}")

    # ---- pre-answer capture, written blind
    print("\n--- before you answer (the key is not shown yet) ---")
    values = {
        name: _prompt_verification()
        if name == "verification_method"
        else input(f"{capture_mod.KNOWN_FIELDS[name]}\n> ")
        for name in served.capture_fields
    }
    try:
        drill.submit_capture(
            served.token, session.build_capture(values, served.capture_fields)
        )
    except capture_mod.CaptureError as exc:
        print(f"\ncapture rejected: {exc}\nNothing recorded.")
        return 1

    # Re-prompt rather than grade an empty box -- see session.BlankAnswer. The
    # same courtesy for a numeric item, where the way a right answer gets typed
    # wrong is submitting the variable you named instead of the value you
    # solved it to (grading.input_shape). Unbounded like the blank loop above,
    # and for the same reason: there is a correct thing to type, and re-asking
    # costs a line where grading it costs an item.
    given = input("\nYour answer\n> ").strip()
    while True:
        if not given:
            prompt = "An answer is required; there is nothing to grade otherwise"
        elif served.input_shape == grading.NUMERIC and not re.fullmatch(
            grading.NUMERIC_INPUT_PATTERN, given
        ):
            prompt = "This one wants the number itself, not the name you gave it"
        else:
            break
        given = input(f"{prompt}\n> ").strip()

    level_raw = input(f"Lowest hint level you needed (0-{hints.MAX_LEVEL})\n> ").strip()
    min_hint = int(level_raw) if level_raw.isdigit() else 0

    verdict = drill.submit_answer(served.token, given, reported_hint_level=min_hint)
    print(f"\n{'CORRECT' if verdict.correct else 'WRONG'}   key: {verdict.answer_key}")

    # ---- the explain-back gate: mandatory, no skip flag
    # On a hit the blind rationale already answers "why is this right", so it is
    # handed back to edit rather than asked for twice; an empty line keeps it.
    # On a miss the useful question is what the path *was* -- and `?` declares
    # there was none, which is an answer to that question and not a way around
    # it (pedagogy/explain_back).
    if verdict.correct:
        print(f"\nYou wrote, before you knew: {values.get('rationale', '')}")
        ask = "Did it hold for that reason? (enter to keep it, or rewrite):\n> "
    else:
        ask = (
            "\nExplain the solution path in your own words (this is the gate).\n"
            "Enter ? if you did not know where to start:\n> "
        )

    while True:
        text = input(ask)
        stuck = not verdict.correct and text.strip() == "?"
        if verdict.correct and not text.strip():
            text = values.get("rationale", "")
        gate = drill.submit_explain_back(served.token, text, stuck=stuck)
        if gate.passed:
            break
        print(f"  {gate.reason}")

    briefing = drill.briefing(served.token)
    out = Path(args.briefing_out)
    out.write_text(briefing.text)

    # The briefing is written either way, before the send is attempted. It is
    # the artifact that makes the exchange recoverable, and a relay that fails
    # must leave the learner exactly where the no-API path would have.
    if relay.configured() and not args.no_relay:
        print("\nSending the briefing for tutoring...")
        try:
            diagnosis = drill.tutor(served.token)
        except (relay.RelayError, record_mod.RecordError) as exc:
            print(f"  relay failed: {exc}")
        else:
            drill.finish(served.token)
            print()
            _print_diagnosis(diagnosis)
            return 0

    drill.finish(served.token)
    print("\n" + "=" * 68)
    print(f"Briefing written to {out}")
    if relay.configured():
        print("Paste it into your chat client, or send it with:")
        print(f"  study record {briefing.attempt_id} --auto --product {args.product}")
    else:
        print("Paste it into your chat client, then:")
        print(f"  study record {briefing.attempt_id} --product {args.product}")
    print("=" * 68)
    return 0


def _print_diagnosis(diagnosis: record_mod.Diagnosis) -> None:
    print(f"recorded: {diagnosis.error_code}")
    if diagnosis.divergence:
        print(f"where it broke: {diagnosis.divergence}")
    print(f"one fix:  {diagnosis.one_fix}")
    if diagnosis.explanation:
        print(f"\n{diagnosis.explanation}")
    if diagnosis.disputed_key:
        print("item flagged disputed_key")
    if diagnosis.explain_back_ok is False:
        print("explain-back rejected -- the attempt stays unresolved")


def cmd_record(args: argparse.Namespace) -> int:
    product, conn = _load(args)
    drill = session.DrillSession(conn, product, args.learner)

    try:
        if args.auto:
            # Same stored briefing the clipboard path would have used, sent
            # rather than carried. `--auto` is explicit here, unlike the drill
            # loop: this command exists to be handed a reply, and guessing that
            # a bare `study record` meant "go and fetch one" would be a
            # different command wearing the same name.
            print("Sending the stored briefing for tutoring...")
            diagnosis = drill.tutor_attempt(args.attempt_id)
        else:
            print("Paste the returned JSON block, then Ctrl-D:")
            diagnosis = drill.record(args.attempt_id, sys.stdin.read())
    except (record_mod.RecordError, relay.RelayError) as exc:
        print(f"rejected: {exc}")
        return 1

    _print_diagnosis(diagnosis)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    product, conn = _load(args)
    state, objective = _state_and_objective(product, conn, args.learner)
    edges = db.load_edges(conn, product["product_id"])
    ranked = allocator.rank(state, objective, edges, limit=25)
    obj_report = objective.report(state)
    db.snapshot_objective(conn, args.learner, product["product_id"], obj_report)
    print(report_mod.render(state, obj_report, ranked, conn))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Past questions answered, by category and outcome."""
    product, conn = _load(args)
    product_id = product["product_id"]

    tallies = db.tally_by_category(conn, args.learner, product_id)
    if not tallies:
        print("nothing answered yet")
        return 0

    print("BY CATEGORY  (weakest first; a record, not a measurement)")
    print(f"  {'tag':<34}{'n':>5}{'correct':>10}{'hint':>7}{'open':>6}")
    for t in tallies:
        print(
            f"  {t.tag_slug:<34}{t.n:>5}{t.n_correct:>6} {t.accuracy:>6.0%}"
            f"{t.mean_hint_level:>6.1f}{t.n_open or 0:>6}"
        )

    attempts = db.list_past_attempts(
        conn, args.learner, product_id,
        tag_slug=args.tag, outcome=args.outcome, state=args.state,
        limit=args.limit,
    )
    print(f"\nATTEMPTS  (newest first, {len(attempts)} shown)")
    for a in attempts:
        mark = "ok " if a.correct else "XX "
        state = {"diagnosed": a.error_code or "diagnosed", "waived": "skipped", "open": "OPEN"}[
            a.exchange_state
        ]
        print(
            f"  {a.attempt_id:>5}  {a.submitted_at[:16].replace('T', ' ')}  {mark}"
            f"L{a.min_hint_level}  {(a.tag_slug or '-'):<30}  {state}"
        )
        print(f"         {a.stem[:66]}")

    open_n = sum(1 for a in attempts if a.exchange_state == "open")
    if open_n:
        print(
            f"\n  {open_n} exchange(s) still open. The briefing is stored for each: "
            "`study record <attempt_id>` picks any of them back up."
        )
    return 0


def cmd_profile(args: argparse.Namespace) -> int:
    """List or create learner profiles.

    A profile is a `learner_id` and a human name. There is no login: this
    database sits on one machine and the people using it are in the room.
    Switching is `--learner` here and a cookie in the web UI; either way the
    profile itself lives in the database, so both front ends see the same set.
    """
    _, conn = _load(args)

    if args.profile_command == "add":
        db.ensure_learner(conn, args.learner_id, args.name)
        print(f"profile {args.learner_id!r} ready")
        return 0

    profiles = db.list_profiles(conn)
    if not profiles:
        print("no profiles yet -- `study profile add <id> --name '...'`")
        return 0
    print(f"  {'learner_id':<20}{'name':<24}{'attempts':>9}")
    for profile in profiles:
        active = "*" if profile.learner_id == args.learner else " "
        print(
            f"{active} {profile.learner_id:<20}{profile.display_name:<24}"
            f"{profile.n_attempts:>9}"
        )
    print("\n* is the active profile (--learner / STUDY_LEARNER)")
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    """Record a learner-reported scalar, e.g. a scored practice essay."""
    product, conn = _load(args)
    db.set_manual_value(
        conn, args.learner, product["product_id"], args.name, float(args.value)
    )
    print(f"{args.name} = {args.value}")
    return 0


# ------------------------------------------------------------------ parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="study", description=__doc__)
    parser.add_argument("--db", default=os.environ.get("STUDY_DB", "study.db"))
    # No default product. The kernel does not get to know which products
    # exist -- naming one here would be the first crack in DESIGN.md §14's
    # one rule, and the purity test would (correctly) fail on it.
    parser.add_argument(
        "--product",
        default=os.environ.get("STUDY_PRODUCT"),
        required="STUDY_PRODUCT" not in os.environ,
        help="path to a product pack directory (or set STUDY_PRODUCT)",
    )
    parser.add_argument("--learner", default=os.environ.get("STUDY_LEARNER", DEFAULT_LEARNER))
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database and load the product pack")

    p_ingest = sub.add_parser("ingest", help="load a JSONL file of item records")
    p_ingest.add_argument("file")

    p_next = sub.add_parser("next", help="show the allocator's ranking")
    p_next.add_argument("--limit", type=int, default=10)

    p_drill = sub.add_parser("drill", help="run one item through the loop")
    p_drill.add_argument("--tag", help="force a tag instead of taking the top-ranked")
    p_drill.add_argument(
        "--section",
        help="restrict to one subject; the allocator still picks the tag within it",
    )
    p_drill.add_argument("--briefing-out", default="briefing.txt")
    p_drill.add_argument(
        "--no-relay",
        action="store_true",
        help="write the briefing and stop, even when an API key is configured",
    )

    p_record = sub.add_parser("record", help="record a returned diagnosis")
    p_record.add_argument("attempt_id", type=int)
    p_record.add_argument(
        "--auto",
        action="store_true",
        help="send the stored briefing to the configured API instead of reading a paste",
    )

    sub.add_parser("report", help="position, routes, reliability, backlog")

    p_history = sub.add_parser("history", help="past questions answered")
    p_history.add_argument("--tag", help="one category only")
    p_history.add_argument("--outcome", choices=["correct", "wrong"])
    p_history.add_argument(
        "--state", choices=["open", "diagnosed", "waived"],
        help="exchange state; 'open' is the come-back-to-it list",
    )
    p_history.add_argument("--limit", type=int, default=20)

    p_profile = sub.add_parser("profile", help="list or create learner profiles")
    profile_sub = p_profile.add_subparsers(dest="profile_command", required=False)
    p_profile_add = profile_sub.add_parser("add", help="create a profile")
    p_profile_add.add_argument("learner_id")
    p_profile_add.add_argument("--name", help="human-readable name for the switcher")
    profile_sub.add_parser("list", help="show every profile (the default)")

    p_set = sub.add_parser("set", help="record a learner-reported variable")
    p_set.add_argument("name")
    p_set.add_argument("value")

    return parser


COMMANDS = {
    "init": cmd_init,
    "ingest": cmd_ingest,
    "next": cmd_next,
    "drill": cmd_drill,
    "record": cmd_record,
    "report": cmd_report,
    "history": cmd_history,
    "profile": cmd_profile,
    "set": cmd_set,
}


def main(argv: list[str] | None = None) -> int:
    # Before the parser, because `--product` defaults out of the environment
    # and `.env` is part of that environment. Real exported variables still
    # win -- see `config.load_env_file`.
    config.load_env_file()
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (config.PackError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
