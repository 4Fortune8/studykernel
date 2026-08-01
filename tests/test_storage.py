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
