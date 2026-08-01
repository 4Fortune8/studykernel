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
from kernel.storage import db

PROFILE_COOKIE = "studykernel_profile"


class NoProductConfigured(RuntimeError):
    """Raised at startup when STUDY_PRODUCT is unset."""


def product_dir() -> Path:
    configured = os.environ.get("STUDY_PRODUCT")
    if not configured:
        raise NoProductConfigured(
            "STUDY_PRODUCT is not set. Point it at a product pack directory "
            "under products/, e.g. STUDY_PRODUCT=products/<pack> study-web"
        )
    return Path(configured)


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
