"""`study` -- the command line for the drill loop.

The loop is: capture (blind) -> grade (deterministic) -> tutor briefing (out)
-> record (in). DESIGN.md §16 v0.

Principle 10 governs the interaction design: friction on the diagnostic loop
is fatal, and every step here has to justify itself. The explain-back gate is
the sole exception, because there the friction *is* the mechanism.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from kernel import allocator, config
from kernel.analytics import report as report_mod
from kernel.exchange import briefing as briefing_mod
from kernel.exchange import record as record_mod
from kernel.objectives import base as objective_base
from kernel.pedagogy import capture as capture_mod
from kernel.pedagogy import explain_back, grading, hints
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
    product, conn = _load(args)
    state, objective = _state_and_objective(product, conn, args.learner)

    if objective.satisfied(state):
        print("Objective satisfied with margin. Stop studying.")
        return 0

    edges = db.load_edges(conn, product["product_id"])
    ranked = [a for a in allocator.rank(state, objective, edges, limit=25) if a.priority > 0]
    if args.tag:
        ranked = [a for a in ranked if a.tag_slug == args.tag]
    if not ranked:
        print("Nothing servable. `study report` shows what the corpus is missing.")
        return 1

    alloc = ranked[0]
    lo, hi = alloc.target_band
    item = db.pick_item(conn, product["product_id"], alloc.tag_slug, lo, hi)
    if item is None:
        print(f"no item for {alloc.tag_slug} in band {lo:.0f}-{hi:.0f}")
        return 1

    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    section = product["sections"][item["section_slug"]]
    choices = json.loads(item["choices_json"]) if item["choices_json"] else None

    passage = None
    if item["passage_id"]:
        row = conn.execute(
            "SELECT text FROM passages WHERE passage_id = ?", (item["passage_id"],)
        ).fetchone()
        passage = row["text"] if row else None

    print("=" * 68)
    print(f"tag: {alloc.tag_slug}    item: {item['item_id']}")
    print("=" * 68)
    if passage:
        print(f"\n{passage}\n")
    print(item["stem"])
    if choices:
        for letter, choice in zip("ABCDEFGH", choices, strict=False):
            print(f"  {letter}. {choice}")

    # ---- pre-answer capture, written blind
    fields = capture_mod.active_fields(product)
    print("\n--- before you answer (the key is not shown yet) ---")
    cap = capture_mod.Capture()
    for field in fields:
        answer = input(f"{capture_mod.KNOWN_FIELDS[field]}\n> ").strip()
        if field == "confidence":
            cap.confidence = int(answer) if answer.isdigit() else None
        else:
            setattr(cap, field, answer)
    try:
        capture_mod.validate(cap, fields)
    except capture_mod.CaptureError as exc:
        print(f"\ncapture rejected: {exc}\nNothing recorded.")
        return 1

    given = input("\nYour answer\n> ").strip()
    level_raw = input(f"Lowest hint level you needed (0-{hints.MAX_LEVEL})\n> ").strip()
    min_hint = int(level_raw) if level_raw.isdigit() else 0

    correct = grading.grade(given, item["answer_key"])
    print(f"\n{'CORRECT' if correct else 'WRONG'}   key: {item['answer_key']}")

    # ---- the explain-back gate: mandatory, no skip flag
    while True:
        explanation = input(
            "\nExplain the solution path in your own words (this is the gate):\n> "
        ).strip()
        gate = explain_back.check(explanation)
        if gate.passed:
            break
        print(f"  {gate.reason}")

    cap_row = cap.as_row()
    cap_row["answer_given"] = given
    attempt_id = db.record_attempt(
        conn,
        args.learner,
        product["product_id"],
        item,
        alloc.tag_slug,
        cap_row,
        correct,
        min_hint,
        started_at,
    )

    b_item = briefing_mod.BriefingItem(
        item_id=item["item_id"],
        stem=item["stem"],
        choices=choices,
        answer_key=item["answer_key"],
        official_expl=item["official_expl"],
        passage=passage,
        section_slug=item["section_slug"],
        explanation_policy=section.get("explanation_policy", "withheld"),
        tags=[alloc.tag_slug],
    )
    b_cap = briefing_mod.BriefingCapture(
        confidence=cap.confidence,
        rationale=cap.rationale,
        verification_method=cap.verification_method,
        answer_given=given,
        correct=correct,
        min_hint_level=min_hint,
    )
    text = briefing_mod.render(b_item, b_cap)
    text += f"\n\n--- LEARNER'S EXPLAIN-BACK ---\n{explanation}\n"

    out = Path(args.briefing_out)
    out.write_text(text)
    db.record_exchange(conn, attempt_id, briefing_mod.PROMPT_VERSION, text, None)

    print("\n" + "=" * 68)
    print(f"Briefing written to {out}")
    print("Paste it into your chat client, then:")
    print(f"  study record {attempt_id} --product {args.product}")
    print("=" * 68)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    product, conn = _load(args)
    row = conn.execute(
        "SELECT item_id FROM attempts WHERE attempt_id = ?", (args.attempt_id,)
    ).fetchone()
    if row is None:
        print(f"no attempt {args.attempt_id}")
        return 1

    print("Paste the returned JSON block, then Ctrl-D:")
    pasted = sys.stdin.read()

    product_codes = frozenset(product.get("error_code_weights", {}) or {})
    try:
        diagnosis = record_mod.parse(pasted, row["item_id"], product_codes)
    except record_mod.RecordError as exc:
        print(f"rejected: {exc}")
        return 1

    db.record_diagnosis(conn, args.attempt_id, diagnosis)
    conn.execute(
        "UPDATE exchanges SET payload_json = ? WHERE attempt_id = ?",
        (json.dumps(diagnosis.raw), args.attempt_id),
    )
    conn.commit()

    print(f"recorded: {diagnosis.error_code}")
    print(f"one fix:  {diagnosis.one_fix}")
    if diagnosis.disputed_key:
        print("item flagged disputed_key")
    if diagnosis.explain_back_ok is False:
        print("explain-back rejected -- the attempt stays unresolved")
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
    p_drill.add_argument("--briefing-out", default="briefing.txt")

    p_record = sub.add_parser("record", help="record a returned diagnosis")
    p_record.add_argument("attempt_id", type=int)

    sub.add_parser("report", help="position, routes, reliability, backlog")

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
    "set": cmd_set,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (config.PackError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
