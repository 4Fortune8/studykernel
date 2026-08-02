"""Per-request wiring: database, product pack, active profile.

Configuration comes from the same environment variables the CLI reads, so a
terminal session and a browser tab are looking at the same database and the
same product by default. `STUDY_PRODUCT` still has no default -- the kernel is
not allowed to know which products exist, and neither is this.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

from kernel import config
from kernel.exchange import relay
from kernel.storage import db

PROFILE_COOKIE = "studykernel_profile"
SUBJECT_COOKIE = "studykernel_subject"


def relay_ready() -> bool:
    """Whether the tutoring exchange can be sent without the clipboard.

    Read per call rather than captured at import, so a key added to `.env` and
    a restart is the whole setup step, and so a test can turn the relay on and
    off with `monkeypatch.setenv` instead of reloading the module.
    """
    return relay.configured()


class NoProductConfigured(RuntimeError):
    """Raised at startup when the product pack is missing or unreadable."""


class NoDatabase(RuntimeError):
    """Raised at startup when STUDY_DB names a database that does not exist."""


def product_dir() -> Path:
    configured = os.environ.get("STUDY_PRODUCT")
    if not configured:
        raise NoProductConfigured(
            "STUDY_PRODUCT is not set. Point it at a product pack directory "
            "under products/, e.g. STUDY_PRODUCT=products/<pack> study-web"
        )
    return Path(configured)


def preflight() -> None:
    """Fail at startup, with the resolved paths, rather than on a page.

    Both settings are relative by default, so they resolve against whatever
    directory the server was launched from -- and launching from the wrong one
    used to produce a running server that 500ed on the home page and, worse,
    quietly created an empty database wherever it happened to be standing. A
    study tool that invents a blank history because of a `cd` is a study tool
    that loses your history, so this refuses to start instead.
    """
    pack = product_dir()
    if not pack.is_dir():
        raise NoProductConfigured(
            f"no product pack at {pack.resolve()}\n"
            f"  STUDY_PRODUCT = {pack}\n"
            f"  working directory = {Path.cwd()}\n"
            "STUDY_PRODUCT is relative unless you make it absolute, so this is "
            "usually the wrong working directory. Run from the repository root "
            "or give an absolute path."
        )
    load_product()  # surfaces a malformed pack here rather than per request

    path = db_path()
    if not path.exists():
        raise NoDatabase(
            f"no database at {path.resolve()}\n"
            f"  STUDY_DB = {path}\n"
            f"  working directory = {Path.cwd()}\n"
            "Refusing to create one: an empty database here would look like a "
            "learner who has never studied. Run `study init` first, or point "
            "STUDY_DB at the database you meant."
        )


def load_product() -> dict[str, Any]:
    return config.load_product(product_dir())


def db_path() -> Path:
    return Path(os.environ.get("STUDY_DB", "study.db"))


def connect() -> sqlite3.Connection:
    """One connection per request. SQLite objects are not thread-safe."""
    return db.connect(db_path())


def active_learner(request_cookies: dict[str, str], conn: sqlite3.Connection) -> str | None:
    """The selected profile, or None when no valid one is selected.

    The *profile* lives in the database; the *selection* lives in a cookie.
    That split is deliberate: a server-side "current profile" would mean two
    people at two devices could not use this at once, and switching on one
    would silently move the other. There is no authentication here and none is
    implied -- the cookie says which profile you picked, not who you are.
    """
    learner_id = request_cookies.get(PROFILE_COOKIE)
    if not learner_id:
        return None
    return learner_id if db.get_profile(conn, learner_id) else None


def active_subject(request_cookies: dict[str, str], product: dict[str, Any]) -> str | None:
    """The chosen subject, or None for all of them.

    Validated against the product every time rather than trusted: a pack that
    renames or drops a section would otherwise leave a cookie pointing at
    nothing, and the kernel rejects an unknown section outright. A stale
    cookie should quietly mean "everything", not a 500.
    """
    chosen = request_cookies.get(SUBJECT_COOKIE)
    return chosen if chosen in (product.get("sections") or {}) else None
