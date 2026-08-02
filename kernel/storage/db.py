"""SQLite storage with an append-only activity log. DESIGN.md §4.

`attempts` is never updated in place -- state is a projection of the log, so a
rating rule can change and history stays replayable. Ratings are cached on
`learner_state` and `items` for query speed, not as the source of truth.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kernel.allocator import target_band
from kernel.state import glicko2, reliability, variables, variance
from kernel.state.view import SectionState, StateView, TagState

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
DEFAULT_DB = Path("study.db")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    _add_missing_columns(conn)
    conn.commit()


# Columns added after a database already existed. `CREATE TABLE IF NOT EXISTS`
# is a no-op on a live table, so a new column in schema.sql never reaches one
# without this. Additive only -- nothing here rewrites or drops data, because
# `attempts` is the append-only source of truth and a migration that can lose
# rows is a migration that will.
LATE_COLUMNS: tuple[tuple[str, str, str], ...] = (
    ("learners", "display_name", "TEXT"),
    ("attempts", "tag_slug", "TEXT"),
    ("attempts", "exchange_waived_at", "TEXT"),
    # No CHECK on the added column: SQLite's ALTER TABLE ADD COLUMN cannot
    # carry one. The default covers every existing row, and `record_attempt`
    # is the only writer.
    ("attempts", "stuck", "INTEGER NOT NULL DEFAULT 0"),
    ("diagnoses", "divergence", "TEXT"),
    ("diagnoses", "explanation", "TEXT"),
    # Null on every row written before the relay existed, which is correct:
    # those all came back through the clipboard, but the log should say it did
    # not know rather than assert something nobody recorded.
    ("exchanges", "responder", "TEXT"),
)

# The default `responder`. A reply carried back by hand is the original path
# and stays the unmarked case; the relay is what has to name itself.
RESPONDER_PASTE = "paste"


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in LATE_COLUMNS:
        present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            continue  # table itself is new; schema.sql already created it correctly
        if column not in present:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


# ------------------------------------------------------------------ writing


def upsert_product(conn: sqlite3.Connection, product: dict[str, Any], digest: str) -> None:
    conn.execute(
        """INSERT INTO products (product_id, display_name, objective_type,
                                 config_json, pack_digest, loaded_at)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(product_id) DO UPDATE SET
             display_name=excluded.display_name,
             objective_type=excluded.objective_type,
             config_json=excluded.config_json,
             pack_digest=excluded.pack_digest,
             loaded_at=excluded.loaded_at""",
        (
            product["product_id"],
            product.get("display_name", product["product_id"]),
            product["objective"]["type"],
            json.dumps(product),
            digest,
            now(),
        ),
    )

    for slug, spec in (product.get("sections") or {}).items():
        conn.execute(
            """INSERT INTO sections (product_id, slug, display_name, item_count,
                                     explanation_policy, blueprint_weight)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id, slug) DO UPDATE SET
                 display_name=excluded.display_name,
                 item_count=excluded.item_count,
                 explanation_policy=excluded.explanation_policy,
                 blueprint_weight=excluded.blueprint_weight""",
            (
                product["product_id"],
                slug,
                spec.get("display_name", slug),
                spec.get("item_count"),
                spec.get("explanation_policy", "withheld"),
                float(spec.get("blueprint_weight", 1.0)),
            ),
        )

    for code, w in (product.get("error_code_weights") or {}).items():
        conn.execute(
            """INSERT INTO error_code_weights (product_id, code, weight)
               VALUES (?, ?, ?)
               ON CONFLICT(product_id, code) DO UPDATE SET weight=excluded.weight""",
            (product["product_id"], code, float(w)),
        )
    conn.commit()


def upsert_taxonomy(
    conn: sqlite3.Connection, product_id: str, tags: list[dict], edges: list[dict]
) -> None:
    for tag in tags:
        conn.execute(
            """INSERT INTO tags (product_id, slug, display_name, section_slug,
                                 parent_slug, level, coverage_weight)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(product_id, slug) DO UPDATE SET
                 display_name=excluded.display_name,
                 section_slug=excluded.section_slug,
                 parent_slug=excluded.parent_slug,
                 level=excluded.level,
                 coverage_weight=excluded.coverage_weight""",
            (
                product_id,
                tag["slug"],
                tag.get("display_name", tag["slug"]),
                tag.get("section"),
                tag.get("parent"),
                tag.get("level"),
                float(tag.get("coverage_weight", 1.0)),
            ),
        )
    for edge in edges:
        conn.execute(
            """INSERT INTO tag_edges (product_id, parent_slug, child_slug,
                                      confidence, source, n_observations)
               VALUES (?, ?, ?, ?, ?, 0)
               ON CONFLICT(product_id, parent_slug, child_slug) DO UPDATE SET
                 confidence=excluded.confidence, source=excluded.source""",
            (
                product_id,
                edge["parent"],
                edge["child"],
                float(edge.get("confidence", 1.0)),
                edge.get("source", "seed"),
            ),
        )
    conn.commit()


def insert_items(conn: sqlite3.Connection, records: list[dict]) -> tuple[int, int]:
    """Bulk insert normalized item records. Returns (inserted, deduped)."""
    inserted = deduped = 0
    for rec in records:
        cur = conn.execute(
            """INSERT OR IGNORE INTO items (
                   item_id, product_id, section_slug, item_type, stem, choices_json,
                   answer_key, official_expl, passage_id, role, style,
                   source, source_ref, license, redistributable, model_transformed,
                   difficulty_prior, difficulty_prior_src,
                   rating, rating_deviation, volatility, n_attempts, flags_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
            (
                rec["item_id"],
                rec["product_id"],
                rec["section"],
                rec["item_type"],
                rec["stem"],
                json.dumps(rec["choices"]) if rec.get("choices") else None,
                rec.get("answer_key"),
                rec.get("official_expl"),
                rec.get("passage_id"),
                rec.get("role", "pool"),
                rec.get("style", "proxy"),
                rec["source"],
                rec.get("source_ref"),
                rec["license"],
                int(rec.get("redistributable", 0)),
                int(rec.get("model_transformed", 0)),
                rec.get("difficulty_prior"),
                rec.get("difficulty_prior_src"),
                float(rec.get("difficulty_prior") or glicko2.DEFAULT_RATING),
                glicko2.DEFAULT_RD,
                glicko2.DEFAULT_VOLATILITY,
                json.dumps(rec.get("flags", [])),
                now(),
            ),
        )
        if cur.rowcount:
            inserted += 1
            for tag in rec.get("tags", []):
                conn.execute(
                    """INSERT OR IGNORE INTO item_tags
                       (item_id, tag_slug, label_source, reviewed, confidence)
                       VALUES (?, ?, ?, ?, ?)""",
                    (
                        rec["item_id"],
                        tag["slug"],
                        tag.get("label_source", "imported"),
                        int(tag.get("reviewed", 0)),
                        tag.get("confidence"),
                    ),
                )
        else:
            deduped += 1
    conn.commit()
    return inserted, deduped


def insert_passage(conn: sqlite3.Connection, passage: dict) -> None:
    conn.execute(
        """INSERT OR IGNORE INTO passages
           (passage_id, text, genre, source, license, word_count)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            passage["passage_id"],
            passage["text"],
            passage.get("genre"),
            passage["source"],
            passage["license"],
            passage.get("word_count"),
        ),
    )


def ensure_learner(
    conn: sqlite3.Connection, learner_id: str, display_name: str | None = None
) -> None:
    conn.execute(
        """INSERT INTO learners (learner_id, created_at, display_name)
           VALUES (?, ?, ?)
           ON CONFLICT (learner_id) DO UPDATE SET
               display_name = COALESCE(excluded.display_name, learners.display_name)""",
        (learner_id, now(), display_name),
    )
    conn.commit()


@dataclass(frozen=True)
class Profile:
    """A learner, named for a human. No credentials -- see schema.sql."""

    learner_id: str
    display_name: str
    created_at: str
    n_attempts: int

    @property
    def used(self) -> bool:
        """A profile with no attempts has nothing to lose if it is a typo."""
        return self.n_attempts > 0


def list_profiles(conn: sqlite3.Connection) -> list[Profile]:
    """Every profile in the database, most-used first."""
    rows = conn.execute(
        """SELECT l.learner_id, l.created_at,
                  COALESCE(l.display_name, l.learner_id) AS display_name,
                  (SELECT COUNT(*) FROM attempts a WHERE a.learner_id = l.learner_id)
                      AS n_attempts
           FROM learners l
           ORDER BY n_attempts DESC, l.created_at ASC"""
    )
    return [Profile(**dict(row)) for row in rows]


def get_profile(conn: sqlite3.Connection, learner_id: str) -> Profile | None:
    return next((p for p in list_profiles(conn) if p.learner_id == learner_id), None)


# ------------------------------------------------------------------ reading


def load_passage(conn: sqlite3.Connection, passage_id: str | None) -> str | None:
    """Passage text for an item, or None when the item stands alone."""
    if not passage_id:
        return None
    row = conn.execute(
        "SELECT text FROM passages WHERE passage_id = ?", (passage_id,)
    ).fetchone()
    return row["text"] if row else None


def _items_in_band(
    conn: sqlite3.Connection, product_id: str, tag_slug: str, lo: float, hi: float
) -> int:
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM items i
           JOIN item_tags t ON t.item_id = i.item_id
           WHERE i.product_id = ? AND t.tag_slug = ? AND i.rating BETWEEN ? AND ?""",
        (product_id, tag_slug, lo, hi),
    ).fetchone()
    return int(row["n"])


def load_state(
    conn: sqlite3.Connection, learner_id: str, product_id: str, config: dict
) -> StateView:
    """Project the log and caches into the view objectives read."""
    sections = {
        r["slug"]: SectionState(
            slug=r["slug"],
            blueprint_weight=r["blueprint_weight"],
            explanation_policy=r["explanation_policy"],
        )
        for r in conn.execute(
            "SELECT * FROM sections WHERE product_id = ?", (product_id,)
        )
    }

    tags: dict[str, TagState] = {}
    for row in conn.execute("SELECT * FROM tags WHERE product_id = ?", (product_id,)):
        state = conn.execute(
            """SELECT * FROM learner_state
               WHERE learner_id = ? AND product_id = ? AND tag_slug = ?""",
            (learner_id, product_id, row["slug"]),
        ).fetchone()

        if state is None:
            rating, rd = glicko2.DEFAULT_RATING, glicko2.DEFAULT_RD
            n = successes = 0
            rel_lo = rel_point = var = 0.0
        else:
            rating, rd = state["rating"], state["rating_deviation"]
            n = state["n_attempts"]
            rel_lo = state["reliability_lo"] or 0.0
            rel_point = state["reliability_hi"] or 0.0
            var = state["variance_rolling"] or 0.0
            successes = int(round(rel_point * n))

        tag = TagState(
            slug=row["slug"],
            section=row["section_slug"] or "",
            rating=rating,
            rd=rd,
            n_attempts=n,
            successes=successes,
            reliability_lo=rel_lo,
            reliability_point=rel_point,
            variance=var,
            coverage_weight=row["coverage_weight"],
            level=row["level"],
            parent_slug=row["parent_slug"],
        )
        lo, hi = target_band(tag)
        tags[row["slug"]] = TagState(
            **{**tag.__dict__, "items_in_band": _items_in_band(conn, product_id, row["slug"], lo, hi)}
        )

    manual = {
        r["code"]: r["weight"]
        for r in conn.execute(
            """SELECT code, weight FROM error_code_weights WHERE product_id = ?""",
            (product_id,),
        )
    }
    del manual  # weights are read by analytics, not by the view

    objective_cfg = config.get("objective", {})
    view = StateView(
        learner_id=learner_id,
        product_id=product_id,
        tags=tags,
        sections=sections,
        mastery_bar=float(config.get("mastery_bar", 0.8)),
        var_declarations=objective_cfg.get("variables", {}) or {},
        domain_declarations=objective_cfg.get("domains", {}) or {},
        manual_values=load_manual_values(conn, learner_id, product_id),
    )
    return variables.recompute(view)


def load_manual_values(
    conn: sqlite3.Connection, learner_id: str, product_id: str
) -> dict[str, float]:
    """Learner-reported scalars, read from the most recent objective_state row.

    Stored alongside objective snapshots rather than in their own table: they
    are observations about a point in time, and that is exactly what
    objective_state records.
    """
    row = conn.execute(
        """SELECT route_id, position_estimate FROM objective_state
           WHERE learner_id = ? AND product_id = ? AND route_id LIKE 'manual:%'
           ORDER BY computed_at DESC""",
        (learner_id, product_id),
    ).fetchall()
    out: dict[str, float] = {}
    for r in row:
        name = r["route_id"].split(":", 1)[1]
        out.setdefault(name, r["position_estimate"])
    return out


def set_manual_value(
    conn: sqlite3.Connection, learner_id: str, product_id: str, name: str, value: float
) -> None:
    conn.execute(
        """INSERT INTO objective_state (learner_id, product_id, route_id, p_success,
                                        position_estimate, margin, satisfied, computed_at)
           VALUES (?, ?, ?, 0.0, ?, 0.0, 0, ?)""",
        (learner_id, product_id, f"manual:{name}", value, now()),
    )
    conn.commit()


def load_edges(conn: sqlite3.Connection, product_id: str) -> dict[str, list[tuple[str, float]]]:
    out: dict[str, list[tuple[str, float]]] = {}
    for r in conn.execute(
        "SELECT parent_slug, child_slug, confidence FROM tag_edges WHERE product_id = ?",
        (product_id,),
    ):
        out.setdefault(r["child_slug"], []).append((r["parent_slug"], r["confidence"]))
    for parents in out.values():
        parents.sort(key=lambda pc: pc[1], reverse=True)
    return out


def pick_item(
    conn: sqlite3.Connection, product_id: str, tag_slug: str, lo: float, hi: float
) -> sqlite3.Row | None:
    """Least-attempted item in the band, so calibration spreads instead of pooling."""
    return conn.execute(
        """SELECT i.* FROM items i
           JOIN item_tags t ON t.item_id = i.item_id
           WHERE i.product_id = ? AND t.tag_slug = ? AND i.rating BETWEEN ? AND ?
           ORDER BY i.n_attempts ASC, RANDOM() LIMIT 1""",
        (product_id, tag_slug, lo, hi),
    ).fetchone()


# ------------------------------------------------------------- the log itself


def record_attempt(
    conn: sqlite3.Connection,
    learner_id: str,
    product_id: str,
    item: sqlite3.Row,
    tag_slug: str,
    capture: dict[str, Any],
    correct: bool,
    min_hint_level: int,
    started_at: str,
) -> int:
    """Append an attempt and update both sides of the rating. Returns attempt_id."""
    tag_row = conn.execute(
        """SELECT * FROM learner_state
           WHERE learner_id = ? AND product_id = ? AND tag_slug = ?""",
        (learner_id, product_id, tag_slug),
    ).fetchone()

    if tag_row is None:
        learner_rating = glicko2.Rating()
        n = successes = 0
        deltas: list[float] = []
    else:
        learner_rating = glicko2.Rating(
            tag_row["rating"], tag_row["rating_deviation"], tag_row["volatility"]
        )
        n = tag_row["n_attempts"]
        successes = int(round((tag_row["reliability_hi"] or 0.0) * n))
        deltas = [
            r["rating_delta"]
            for r in conn.execute(
                """SELECT a.rating_delta FROM attempts a
                   JOIN item_tags t ON t.item_id = a.item_id
                   WHERE a.learner_id = ? AND t.tag_slug = ? AND a.rating_delta IS NOT NULL
                   ORDER BY a.attempt_id DESC LIMIT ?""",
                (learner_id, tag_slug, variance.DEFAULT_WINDOW),
            )
        ]

    item_rating = glicko2.Rating(
        item["rating"], item["rating_deviation"], item["volatility"]
    )
    new_learner, new_item = glicko2.apply_attempt(learner_rating, item_rating, correct)
    delta = new_learner.rating - learner_rating.rating

    cur = conn.execute(
        """INSERT INTO attempts (learner_id, product_id, item_id, started_at,
               submitted_at, confidence, rationale, verification_method, answer_given,
               correct, min_hint_level, time_to_first_selection_ms, time_total_ms,
               pass_number, rating_delta, resolved, tag_slug, stuck)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0,?,?)""",
        (
            learner_id,
            product_id,
            item["item_id"],
            started_at,
            now(),
            capture.get("confidence"),
            capture.get("rationale"),
            capture.get("verification_method"),
            capture.get("answer_given"),
            int(correct),
            min_hint_level,
            capture.get("time_to_first_selection_ms"),
            capture.get("time_total_ms"),
            capture.get("pass_number", 1),
            delta,
            tag_slug,
            int(bool(capture.get("stuck"))),
        ),
    )
    attempt_id = int(cur.lastrowid)

    n += 1
    successes += int(correct)
    interval = reliability.wilson(successes, n)
    new_deltas = ([*deltas, delta])[-variance.DEFAULT_WINDOW :]

    conn.execute(
        """INSERT INTO learner_state (learner_id, product_id, tag_slug, rating,
               rating_deviation, volatility, n_attempts, reliability_lo,
               reliability_hi, variance_rolling, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(learner_id, product_id, tag_slug) DO UPDATE SET
             rating=excluded.rating, rating_deviation=excluded.rating_deviation,
             volatility=excluded.volatility, n_attempts=excluded.n_attempts,
             reliability_lo=excluded.reliability_lo,
             reliability_hi=excluded.reliability_hi,
             variance_rolling=excluded.variance_rolling,
             updated_at=excluded.updated_at""",
        (
            learner_id,
            product_id,
            tag_slug,
            new_learner.rating,
            new_learner.rd,
            new_learner.volatility,
            n,
            interval.lo,
            # `reliability_hi` doubles as the stored point estimate; the CI's
            # upper bound is recomputable and the point estimate is not.
            interval.point,
            variance.rolling_variance(new_deltas),
            now(),
        ),
    )

    conn.execute(
        """UPDATE items SET rating=?, rating_deviation=?, volatility=?,
                            n_attempts=n_attempts+1 WHERE item_id=?""",
        (new_item.rating, new_item.rd, new_item.volatility, item["item_id"]),
    )
    conn.commit()
    return attempt_id


def record_diagnosis(conn: sqlite3.Connection, attempt_id: int, diagnosis: Any) -> None:
    conn.execute(
        """INSERT INTO diagnoses (attempt_id, error_code, prerequisite_gaps_json,
                                  one_fix, divergence, explanation, trigger_miss,
                                  explain_back, explain_back_ok)
           VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(attempt_id) DO UPDATE SET
             error_code=excluded.error_code,
             prerequisite_gaps_json=excluded.prerequisite_gaps_json,
             one_fix=excluded.one_fix, divergence=excluded.divergence,
             explanation=excluded.explanation,
             trigger_miss=excluded.trigger_miss,
             explain_back=excluded.explain_back,
             explain_back_ok=excluded.explain_back_ok""",
        (
            attempt_id,
            diagnosis.error_code,
            json.dumps(diagnosis.prerequisite_gaps),
            diagnosis.one_fix,
            getattr(diagnosis, "divergence", None),
            getattr(diagnosis, "explanation", None),
            int(diagnosis.trigger_miss),
            getattr(diagnosis, "explain_back", None),
            None if diagnosis.explain_back_ok is None else int(diagnosis.explain_back_ok),
        ),
    )
    if diagnosis.explain_back_ok:
        conn.execute("UPDATE attempts SET resolved = 1 WHERE attempt_id = ?", (attempt_id,))
    if diagnosis.disputed_key:
        conn.execute(
            """UPDATE items
               SET flags_json = json_insert(flags_json, '$[#]', 'disputed_key')
               WHERE item_id = ?""",
            (diagnosis.item_id,),
        )
    conn.commit()


def record_exchange(
    conn: sqlite3.Connection, attempt_id: int, prompt_version: str, briefing: str, payload: str | None
) -> None:
    conn.execute(
        """INSERT INTO exchanges (attempt_id, prompt_version, briefing, payload_json, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (attempt_id, prompt_version, briefing, payload, now()),
    )
    conn.commit()


# ------------------------------------------------------ answered questions


@dataclass(frozen=True)
class PastAttempt:
    """One answered item, as the history page shows it."""

    attempt_id: int
    submitted_at: str
    tag_slug: str | None
    item_id: str
    stem: str
    answer_given: str | None
    answer_key: str | None
    correct: bool
    min_hint_level: int
    confidence: int | None
    error_code: str | None
    one_fix: str | None
    waived_at: str | None
    has_briefing: bool

    @property
    def exchange_state(self) -> str:
        """`diagnosed`, `waived`, or `open`.

        `open` is the only one that is a to-do. A waived attempt is finished:
        DESIGN.md §10 makes the tutoring exchange optional, so declining it is
        an end state, not an omission.
        """
        if self.error_code:
            return "diagnosed"
        if self.waived_at:
            return "waived"
        return "open"


_HISTORY_SELECT = """
    SELECT a.attempt_id, a.submitted_at, a.item_id, a.answer_given, a.correct,
           a.min_hint_level, a.confidence, a.exchange_waived_at AS waived_at,
           -- Older rows predate attempts.tag_slug; fall back to any tag the
           -- item carries rather than showing them as uncategorised.
           COALESCE(a.tag_slug,
                    (SELECT t.tag_slug FROM item_tags t
                      WHERE t.item_id = a.item_id
                      ORDER BY t.tag_slug LIMIT 1)) AS tag_slug,
           i.stem, i.answer_key,
           d.error_code, d.one_fix,
           EXISTS(SELECT 1 FROM exchanges e
                   WHERE e.attempt_id = a.attempt_id) AS has_briefing
      FROM attempts a
      JOIN items i ON i.item_id = a.item_id
 LEFT JOIN diagnoses d ON d.attempt_id = a.attempt_id
     WHERE a.learner_id = ? AND a.product_id = ?
"""


def _as_past(row: sqlite3.Row) -> PastAttempt:
    return PastAttempt(
        attempt_id=row["attempt_id"],
        submitted_at=row["submitted_at"],
        tag_slug=row["tag_slug"],
        item_id=row["item_id"],
        stem=row["stem"],
        answer_given=row["answer_given"],
        answer_key=row["answer_key"],
        correct=bool(row["correct"]),
        min_hint_level=row["min_hint_level"],
        confidence=row["confidence"],
        error_code=row["error_code"],
        one_fix=row["one_fix"],
        waived_at=row["waived_at"],
        has_briefing=bool(row["has_briefing"]),
    )


def list_past_attempts(
    conn: sqlite3.Connection,
    learner_id: str,
    product_id: str,
    tag_slug: str | None = None,
    outcome: str | None = None,
    state: str | None = None,
    limit: int = 200,
) -> list[PastAttempt]:
    """Answered items, newest first, filterable by tag, outcome and state."""
    sql = _HISTORY_SELECT
    params: list[Any] = [learner_id, product_id]

    if tag_slug:
        sql += " AND COALESCE(a.tag_slug, '') = ?"
        params.append(tag_slug)
    if outcome == "correct":
        sql += " AND a.correct = 1"
    elif outcome == "wrong":
        sql += " AND a.correct = 0"
    if state == "open":
        sql += " AND d.attempt_id IS NULL AND a.exchange_waived_at IS NULL"
    elif state == "diagnosed":
        sql += " AND d.attempt_id IS NOT NULL"
    elif state == "waived":
        sql += " AND a.exchange_waived_at IS NOT NULL"

    sql += " ORDER BY a.submitted_at DESC, a.attempt_id DESC LIMIT ?"
    params.append(limit)
    return [_as_past(r) for r in conn.execute(sql, params)]


def get_past_attempt(
    conn: sqlite3.Connection, learner_id: str, product_id: str, attempt_id: int
) -> PastAttempt | None:
    row = conn.execute(
        _HISTORY_SELECT + " AND a.attempt_id = ?", (learner_id, product_id, attempt_id)
    ).fetchone()
    return _as_past(row) if row else None


@dataclass(frozen=True)
class CategoryTally:
    """How one tag is going. The row of the past-questions summary."""

    tag_slug: str
    n: int
    n_correct: int
    mean_hint_level: float
    n_open: int

    @property
    def accuracy(self) -> float:
        return self.n_correct / self.n if self.n else 0.0


def tally_by_category(
    conn: sqlite3.Connection, learner_id: str, product_id: str
) -> list[CategoryTally]:
    """Per-tag outcomes, weakest first.

    Ordered by accuracy rather than volume: the point of looking is to find
    where you are losing items, and sorting by count would bury a tag you got
    wrong three times out of three under one you have practised forty times.

    This is a description of what happened, not a measurement of ability --
    `study report`'s reliability table is the latter, and it is the one with
    the confidence intervals. Raw accuracy over four items means very little,
    which is exactly why mastery lives on lower bounds (DESIGN.md §6.2).
    """
    # Grouped in an outer query on purpose. `GROUP BY tag_slug` in the inner
    # one binds to the *column* `attempts.tag_slug`, not to the COALESCE that
    # shares its name -- so every pre-migration row (column NULL, fallback
    # populated) collapsed into a single group labelled with whichever member
    # happened to surface. It reported three attempts under one tag while the
    # attempt list beside it showed them under two.
    rows = conn.execute(
        """SELECT tag_slug,
                  COUNT(*) AS n,
                  SUM(correct) AS n_correct,
                  AVG(min_hint_level) AS mean_hint,
                  SUM(is_open) AS n_open
             FROM (
               SELECT COALESCE(a.tag_slug,
                        (SELECT t.tag_slug FROM item_tags t
                          WHERE t.item_id = a.item_id
                          ORDER BY t.tag_slug LIMIT 1)) AS tag_slug,
                      a.correct, a.min_hint_level,
                      CASE WHEN d.attempt_id IS NULL
                            AND a.exchange_waived_at IS NULL
                           THEN 1 ELSE 0 END AS is_open
                 FROM attempts a
            LEFT JOIN diagnoses d ON d.attempt_id = a.attempt_id
                WHERE a.learner_id = ? AND a.product_id = ?
             )
         GROUP BY tag_slug""",
        (learner_id, product_id),
    ).fetchall()

    tallies = [
        CategoryTally(
            tag_slug=r["tag_slug"] or "(untagged)",
            n=r["n"],
            n_correct=int(r["n_correct"] or 0),
            mean_hint_level=float(r["mean_hint"] or 0.0),
            n_open=int(r["n_open"] or 0),
        )
        for r in rows
    ]
    return sorted(tallies, key=lambda t: (t.accuracy, -t.n))


def waive_exchange(conn: sqlite3.Connection, attempt_id: int, waived: bool = True) -> None:
    """Mark an attempt as not needing a tutoring exchange, or undo that."""
    conn.execute(
        "UPDATE attempts SET exchange_waived_at = ? WHERE attempt_id = ?",
        (now() if waived else None, attempt_id),
    )
    conn.commit()


def load_briefing(conn: sqlite3.Connection, attempt_id: int) -> sqlite3.Row | None:
    """The stored briefing, so a skipped exchange can be picked up later."""
    return conn.execute(
        """SELECT briefing, prompt_version, payload_json, responder
             FROM exchanges WHERE attempt_id = ?""",
        (attempt_id,),
    ).fetchone()


def snapshot_objective(
    conn: sqlite3.Connection, learner_id: str, product_id: str, report: Any
) -> None:
    """Persist per-route position. Position over time is itself a progress artifact."""
    for route in report.routes:
        conn.execute(
            """INSERT INTO objective_state (learner_id, product_id, route_id, p_success,
                   position_estimate, margin, satisfied, computed_at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (
                learner_id,
                product_id,
                route.route_id,
                route.p_success,
                route.position,
                route.margin,
                int(route.satisfied),
                now(),
            ),
        )
    conn.commit()
