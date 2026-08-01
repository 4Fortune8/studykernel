"""Storage concerns: schema migration and learner profiles.

Profiles are not accounts. There is no credential anywhere in this file and
there is not meant to be one: the database sits on one machine and the people
using it are in the room. What a profile buys is that two people can use the
same install without their ratings, attempts and objective position mixing --
which the schema already supported, since everything is keyed by `learner_id`.
"""

from __future__ import annotations

import sqlite3

import pytest

from kernel.analytics import report
from kernel.storage import db


@pytest.fixture
def conn(tmp_path):
    connection = db.connect(tmp_path / "t.db")
    db.migrate(connection)
    return connection


# ------------------------------------------------------------- migration


def test_a_database_predating_display_name_gains_the_column(tmp_path):
    """`CREATE TABLE IF NOT EXISTS` is a no-op on a live table.

    Without the ALTER pass, a new column in schema.sql silently never reaches
    an existing database and every read of it fails at runtime instead.
    """
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """CREATE TABLE learners (
               learner_id TEXT PRIMARY KEY,
               created_at TEXT NOT NULL
           );
           INSERT INTO learners VALUES ('me', '2026-01-01T00:00:00+00:00');"""
    )
    old.commit()
    old.close()

    migrated = db.connect(path)
    db.migrate(migrated)

    columns = {r["name"] for r in migrated.execute("PRAGMA table_info(learners)")}
    assert "display_name" in columns
    # Additive only: the existing row survives untouched.
    assert [p.learner_id for p in db.list_profiles(migrated)] == ["me"]


def test_migration_is_idempotent(conn):
    db.migrate(conn)
    db.migrate(conn)
    columns = [r["name"] for r in conn.execute("PRAGMA table_info(learners)")]
    assert columns.count("display_name") == 1


# -------------------------------------------------------------- profiles


def test_a_profile_without_a_name_falls_back_to_its_id(conn):
    db.ensure_learner(conn, "me")
    assert db.get_profile(conn, "me").display_name == "me"


def test_a_name_is_kept_and_not_clobbered_by_a_later_bare_ensure(conn):
    """`study init` calls ensure_learner with no name on every run.

    If that overwrote the stored name with NULL, naming a profile in the web
    UI would survive exactly until the next `study init`.
    """
    db.ensure_learner(conn, "sam", "Sam")
    db.ensure_learner(conn, "sam")
    assert db.get_profile(conn, "sam").display_name == "Sam"


def test_a_name_can_be_changed(conn):
    db.ensure_learner(conn, "sam", "Sam")
    db.ensure_learner(conn, "sam", "Samira")
    assert db.get_profile(conn, "sam").display_name == "Samira"


def test_an_unknown_profile_is_none_not_an_error(conn):
    """The web layer asks this of a cookie it did not write. It must not raise."""
    assert db.get_profile(conn, "ghost") is None


def test_profiles_are_listed_most_used_first(conn):
    db.ensure_learner(conn, "quiet", "Quiet")
    db.ensure_learner(conn, "busy", "Busy")
    conn.execute(
        """INSERT INTO products (product_id, display_name, objective_type,
               config_json, pack_digest, loaded_at)
           VALUES ('p', 'P', 'threshold', '{}', 'd', '2026-01-01')"""
    )
    conn.execute(
        """INSERT INTO items (item_id, product_id, section_slug, item_type, stem,
               answer_key, source, license, rating, rating_deviation, volatility,
               n_attempts, created_at)
           VALUES ('i1', 'p', 's', 'mc', 'q', 'a', 'test', 'test',
                   1500, 350, 0.06, 0, '2026-01-01')"""
    )
    for _ in range(3):
        conn.execute(
            """INSERT INTO attempts (learner_id, product_id, item_id,
                   started_at, submitted_at, correct, min_hint_level)
               VALUES ('busy', 'p', 'i1', '2026-01-01', '2026-01-01', 1, 0)"""
        )
    conn.commit()

    listed = db.list_profiles(conn)
    assert [p.learner_id for p in listed] == ["busy", "quiet"]
    assert listed[0].n_attempts == 3
    assert listed[0].used is True
    assert listed[1].used is False


# --------------------------------------------------- past questions


@pytest.fixture
def history_db(conn):
    """Two items on different tags, and attempts of both schema vintages.

    Pre-migration rows have `attempts.tag_slug` NULL and have to fall back to
    the item's own tag; post-migration rows carry it. Both shapes live in one
    real database forever, so both belong in the fixture.
    """
    db.ensure_learner(conn, "me")
    conn.execute(
        """INSERT INTO products (product_id, display_name, objective_type,
               config_json, pack_digest, loaded_at)
           VALUES ('p', 'P', 'threshold', '{}', 'd', '2026-01-01')"""
    )
    for item_id, tag in (("i-alg", "algebra"), ("i-read", "reading")):
        conn.execute(
            """INSERT INTO items (item_id, product_id, section_slug, item_type, stem,
                   answer_key, source, license, rating, rating_deviation, volatility,
                   n_attempts, created_at)
               VALUES (?, 'p', 's', 'mc', ?, 'a', 'test', 'test',
                       1500, 350, 0.06, 0, '2026-01-01')""",
            (item_id, f"stem for {item_id}"),
        )
        conn.execute(
            """INSERT INTO item_tags (item_id, tag_slug, label_source, reviewed)
               VALUES (?, ?, 'official', 1)""",
            (item_id, tag),
        )

    def attempt(item_id, correct, hint, tag_slug, when):
        conn.execute(
            """INSERT INTO attempts (learner_id, product_id, item_id, started_at,
                   submitted_at, correct, min_hint_level, tag_slug)
               VALUES ('me', 'p', ?, ?, ?, ?, ?, ?)""",
            (item_id, when, when, correct, hint, tag_slug),
        )

    # Pre-migration: tag_slug NULL on both, different items.
    attempt("i-alg", 0, 1, None, "2026-01-01T01:00:00+00:00")
    attempt("i-read", 1, 0, None, "2026-01-02T01:00:00+00:00")
    # Post-migration: tag recorded at the time it was served.
    attempt("i-read", 1, 0, "reading", "2026-01-03T01:00:00+00:00")
    conn.commit()
    return conn


def test_old_rows_are_grouped_by_the_item_tag_not_lumped_together(history_db):
    """The bug this caught: GROUP BY bound to the column, not the alias.

    `attempts.tag_slug` is NULL on every pre-migration row and shares its name
    with the COALESCE that fills it in, so SQLite grouped all of them into one
    bucket labelled with whichever member surfaced first. The summary claimed
    three attempts on one tag while the list beside it showed two tags.
    """
    tallies = {t.tag_slug: t for t in db.tally_by_category(history_db, "me", "p")}
    assert set(tallies) == {"algebra", "reading"}
    assert tallies["algebra"].n == 1
    assert tallies["reading"].n == 2


def test_the_summary_and_the_list_agree_on_every_category(history_db):
    """They are separate queries over the same rows; drift between them is a bug."""
    from collections import Counter

    listed = Counter(a.tag_slug for a in db.list_past_attempts(history_db, "me", "p"))
    tallied = {t.tag_slug: t.n for t in db.tally_by_category(history_db, "me", "p")}
    assert dict(listed) == tallied


def test_categories_are_ordered_weakest_first(history_db):
    tallies = db.tally_by_category(history_db, "me", "p")
    assert [t.tag_slug for t in tallies] == ["algebra", "reading"]
    assert tallies[0].accuracy == 0.0
    assert tallies[1].accuracy == 1.0


def test_waiving_is_an_end_state_not_an_open_one(history_db):
    attempt_id = history_db.execute("SELECT MIN(attempt_id) FROM attempts").fetchone()[0]
    assert db.get_past_attempt(history_db, "me", "p", attempt_id).exchange_state == "open"

    db.waive_exchange(history_db, attempt_id)
    assert db.get_past_attempt(history_db, "me", "p", attempt_id).exchange_state == "waived"
    assert not db.list_past_attempts(history_db, "me", "p", state="open") == [], "others remain"
    assert attempt_id not in [
        a.attempt_id for a in db.list_past_attempts(history_db, "me", "p", state="open")
    ]

    db.waive_exchange(history_db, attempt_id, waived=False)
    assert db.get_past_attempt(history_db, "me", "p", attempt_id).exchange_state == "open"


def test_history_filters_compose(history_db):
    assert len(db.list_past_attempts(history_db, "me", "p", outcome="correct")) == 2
    assert len(db.list_past_attempts(history_db, "me", "p", outcome="wrong")) == 1
    assert len(db.list_past_attempts(history_db, "me", "p", tag_slug="reading")) == 1, (
        "tag filtering matches the stored column, not the fallback"
    )


def test_history_is_scoped_to_one_learner(history_db):
    db.ensure_learner(history_db, "other")
    assert db.list_past_attempts(history_db, "other", "p") == []
    assert db.tally_by_category(history_db, "other", "p") == []


# ------------------------------------------------- the checking section


@pytest.fixture
def checking_db(conn):
    """Attempts of both vintages: closed-set slugs and pre-closure free text.

    Both shapes live in one real database forever -- the field was a text box
    for its first 32 rows -- so both belong in the fixture.
    """
    db.ensure_learner(conn, "me")
    db.ensure_learner(conn, "someone-else")
    conn.execute(
        """INSERT INTO products (product_id, display_name, objective_type,
               config_json, pack_digest, loaded_at)
           VALUES ('p', 'P', 'threshold', '{}', 'd', '2026-01-01')"""
    )
    conn.execute(
        """INSERT INTO items (item_id, product_id, section_slug, item_type, stem,
               answer_key, source, license, rating, rating_deviation, volatility,
               n_attempts, created_at)
           VALUES ('i', 'p', 's', 'mc', 'stem', 'a', 'test', 'test',
                   1500, 350, 0.06, 0, '2026-01-01')"""
    )

    def attempt(method, correct, code=None, learner="me"):
        cur = conn.execute(
            """INSERT INTO attempts (learner_id, product_id, item_id, started_at,
                   submitted_at, correct, verification_method, tag_slug)
               VALUES (?, 'p', 'i', '2026-01-01', '2026-01-01', ?, ?, 'algebra')""",
            (learner, correct, method),
        )
        if code:
            conn.execute(
                "INSERT INTO diagnoses (attempt_id, error_code, one_fix) VALUES (?, ?, 'f')",
                (cur.lastrowid, code),
            )

    return conn, attempt


def checking(conn, learner="me"):
    return "\n".join(report._checking_section(conn, learner, "p"))


def test_the_checking_split_counts_both_sides(checking_db):
    conn, attempt = checking_db
    attempt("back_substitution,unit_check", 1)
    attempt("recomputed", 1)
    attempt("none", 0, "execution_error")
    text = checking(conn)
    assert "checked              2 attempt(s)  100% correct" in text
    assert "did not check        1 attempt(s)    0% correct" in text


def test_the_headline_counts_only_errors_a_check_would_have_caught(checking_db):
    """`knowledge_gap` is worse and no check finds it.

    Counting it here would inflate a real number, and an inflated number gets
    the whole section discounted -- which is the one thing it cannot afford.
    """
    conn, attempt = checking_db
    attempt("none", 0, "execution_error")
    attempt("none", 0, "misread")
    attempt("none", 0, "knowledge_gap")
    text = checking(conn)
    assert ">>> 2 wrong after no check" in text


def test_an_unchecked_miss_nobody_diagnosed_is_not_counted_as_preventable(checking_db):
    """No diagnosis means no error code, and no grounds to call it careless."""
    conn, attempt = checking_db
    attempt("none", 0)
    text = checking(conn)
    assert ">>>" not in text
    assert "No unchecked miss was coded" in text


def test_free_text_from_before_the_closed_set_is_excluded_and_said_so(checking_db):
    conn, attempt = checking_db
    attempt("wowowowowow", 0)
    attempt("Not sure what to put here", 1)
    attempt("back_substitution", 1)
    text = checking(conn)
    assert "checked              1 attempt(s)" in text
    assert "2 older attempt(s) excluded" in text


def test_a_history_that_is_entirely_free_text_reports_no_number(checking_db):
    """0% off nothing is a lie shaped like a statistic."""
    conn, attempt = checking_db
    attempt("math", 0)
    text = checking(conn)
    assert "No countable data yet" in text
    assert "%" not in text


def test_the_methods_never_used_are_named(checking_db):
    """One reflex and no others is a different problem from not checking."""
    conn, attempt = checking_db
    attempt("back_substitution", 1)
    attempt("back_substitution", 1)
    text = checking(conn)
    assert "methods used: Back-substitution 2" in text
    assert "never used:" in text
    assert "Size check" in text
    assert "I didn't check" not in text  # not a method, and never "unused"


def test_a_product_with_the_field_off_prints_nothing(checking_db):
    """Self-gating: no rows, no section, no config plumbed in to ask."""
    conn, attempt = checking_db
    assert checking(conn) == ""


def test_the_checking_section_is_scoped_to_one_learner(checking_db):
    conn, attempt = checking_db
    attempt("none", 0, "execution_error", learner="someone-else")
    assert checking(conn, "me") == ""
    assert ">>> 1 wrong" in checking(conn, "someone-else")
