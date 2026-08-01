"""The web adapter, over HTTP. WEB_UI.md §4.

The kernel already guarantees these invariants -- `Served` has no key field,
phases advance in one order, the gate has no skip. These tests check the thing
the kernel cannot: that the adapter did not undo any of it on the way to the
browser. §4.1 in particular asks for exactly this ("a view-source test belongs
in the suite"), because the failure it prevents is silent. A leaked key does
not raise; it just quietly makes every capture after it worthless, and the
learner cannot tell, because they will not remember whether they peeked.
"""

from __future__ import annotations

import re

import pytest

pytest.importorskip("fastapi", reason="web extra not installed")

from fastapi.testclient import TestClient  # noqa: E402

from kernel import config  # noqa: E402
from kernel.storage import db  # noqa: E402

# Distinctive on purpose. A single-letter key like "D" appears inside "<div>"
# and would make a leak test pass on noise -- which it did, the first time.
SECRET_KEY = "quokka-7"

PRODUCT_YAML = """
product_id: web-probe
display_name: "Probe pack"
mastery_bar: 0.80
sections:
  quant:
    display_name: "Quantitative"
    item_count: 20
    explanation_policy: withheld
    blueprint_weight: 1.0
capture:
  fields:
    - verification_method
error_code_weights:
  execution_error: 2.0
  knowledge_gap: 1.0
  none: 0.0
"""

OBJECTIVE_YAML = """
objective:
  type: threshold
  margin_policy: rating_deviation
  margin_multiplier: 1.0
  variables:
    ability:
      kind: scaled_ability
      section: quant
      rating_range: [1200, 1750]
      scale_range: [910, 990]
  routes:
    quant:
      - "ability >= 980"
"""

TAGS_YAML = """
tags:
  - slug: fractions
    display_name: "Fractions"
    section: quant
edges: []
"""


@pytest.fixture
def client(tmp_path, monkeypatch):
    pack_dir = tmp_path / "pack"
    (pack_dir / "taxonomy").mkdir(parents=True)
    (pack_dir / "product.yaml").write_text(PRODUCT_YAML)
    (pack_dir / "objective.yaml").write_text(OBJECTIVE_YAML)
    (pack_dir / "taxonomy" / "tags.yaml").write_text(TAGS_YAML)
    pack = config.load_product(pack_dir)

    db_path = tmp_path / "web.db"
    conn = db.connect(db_path)
    db.migrate(conn)
    db.upsert_product(conn, pack, "digest")
    db.upsert_taxonomy(conn, pack["product_id"], pack["_tags"], pack["_edges"])
    db.ensure_learner(conn, "learner", "Learner")
    db.insert_items(
        conn,
        [
            {
                "item_id": f"i{n}",
                "product_id": pack["product_id"],
                "section": "quant",
                "item_type": "mc",
                "stem": "Pick the right one.",
                "choices": ["alpha-1", SECRET_KEY, "gamma-3", "delta-4"],
                "answer_key": SECRET_KEY,
                "source": "test",
                "license": "test",
                "tags": [{"slug": "fractions", "label_source": "official", "reviewed": 1}],
            }
            for n in range(6)
        ],
    )
    conn.close()

    monkeypatch.setenv("STUDY_DB", str(db_path))
    monkeypatch.setenv("STUDY_PRODUCT", str(pack_dir))

    from web import app as app_module

    # A fresh store per test: drill tokens are process-global by default and
    # would otherwise leak between tests the way they would between workers.
    monkeypatch.setattr(app_module.session, "DEFAULT_STORE", app_module.session.DrillStore())

    with TestClient(app_module.app) as test_client:
        test_client.cookies.set("studykernel_profile", "learner")
        yield test_client


def start_drill(client) -> str:
    response = client.post("/drill/start", data={"tag": ""}, follow_redirects=False)
    assert response.status_code == 303
    return response.headers["location"].rsplit("/", 1)[1]


CHOICE_LIST = re.compile(r'<ol[^>]*\bclass="[^"]*\bchoices\b[^"]*"[^>]*>.*?</ol>', re.S)


def without_choices(html: str) -> str:
    """The page minus the rendered choice list.

    The keyed string legitimately appears there, as one option among four --
    that is the item. Everywhere else is a leak.

    Matched loosely on the class attribute on purpose: an earlier version
    anchored on `class="choices"` exactly and silently stopped stripping
    anything the moment a second class was added, turning the leak test into
    a test that fails on markup changes. A safety test that cries wolf gets
    muted, which is the one outcome this test cannot afford.
    """
    stripped = CHOICE_LIST.sub("", html)
    assert stripped != html, "choice list not found -- the strip pattern is stale"
    return stripped


# ------------------------------------------------- §4.1 the key is not sent


def test_the_key_is_not_in_the_capture_page(client):
    token = start_drill(client)
    body = client.get(f"/drill/{token}").text
    assert SECRET_KEY in body, "sanity: the item should render at all"
    assert SECRET_KEY not in without_choices(body)


def test_the_key_is_not_in_the_answer_page_either(client):
    """Capture is locked but the answer is not in, so grading has not happened."""
    token = start_drill(client)
    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "2",
            "rationale": "Because the second option matches the stem.",
            "verification_method": "re-read the qualifier",
        },
    )
    body = client.get(f"/drill/{token}").text
    assert SECRET_KEY not in without_choices(body)


def test_the_key_arrives_with_the_verdict(client):
    token = start_drill(client)
    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "2",
            "rationale": "Because the second option matches the stem.",
            "verification_method": "re-read the qualifier",
        },
    )
    panel = client.post(f"/drill/{token}/answer", data={"answer": SECRET_KEY}).text
    assert SECRET_KEY in panel
    assert "Correct" in panel


# --------------------------------------------------- §4.2 observed hints


def test_a_rung_served_over_http_floors_the_recorded_level(client):
    token = start_drill(client)
    rung = client.post(f"/drill/{token}/hint", data={"level": "3"})
    assert "L3" in rung.text

    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "1",
            "rationale": "Guessing from the shape of the options.",
            "verification_method": "none",
        },
    )
    panel = client.post(f"/drill/{token}/answer", data={"answer": SECRET_KEY}).text
    assert "L3" in panel


def test_hints_are_refused_once_the_key_is_out(client):
    token = start_drill(client)
    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "2",
            "rationale": "The second option matches the stem exactly.",
            "verification_method": "re-read",
        },
    )
    client.post(f"/drill/{token}/answer", data={"answer": SECRET_KEY})
    with pytest.raises(Exception):
        client.post(f"/drill/{token}/hint", data={"level": "1"})


# ------------------------------------------------------- §4.3 the gate


def test_a_rejected_capture_re_renders_the_form_with_the_reason(client):
    token = start_drill(client)
    panel = client.post(
        f"/drill/{token}/capture",
        data={"confidence": "2", "rationale": "no", "verification_method": "x"},
    ).text
    assert "rationale must be a real sentence" in panel
    assert "Lock this in" in panel


def test_the_gate_must_pass_before_anything_is_recorded(client, tmp_path):
    token = start_drill(client)
    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "3",
            "rationale": "The second option restates the stem.",
            "verification_method": "re-read",
        },
    )
    client.post(f"/drill/{token}/answer", data={"answer": SECRET_KEY})

    panel = client.post(f"/drill/{token}/explain", data={"explanation": "dunno"}).text
    assert "under 15 words" in panel

    conn = db.connect(tmp_path / "web.db")
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    conn.close()


def test_the_gate_panel_offers_no_action_but_the_gate(client):
    """No skip button, no "remind me later", no way out but through.

    Checked structurally rather than by grepping for the word "skip" -- the
    page prose *says* there is no skip, which made the naive version of this
    test fail on its own explanation.
    """
    token = start_drill(client)
    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "3",
            "rationale": "The second option restates the stem.",
            "verification_method": "re-read",
        },
    )
    body = client.post(f"/drill/{token}/answer", data={"answer": SECRET_KEY}).text

    posts = set(re.findall(r'hx-post="([^"]+)"', body))
    assert posts == {f"/drill/{token}/explain"}
    # And nothing that navigates away from it either.
    assert not re.findall(r"<a\s[^>]*href=", body)


# --------------------------------------------------- the whole loop


def test_the_full_loop_records_an_attempt_and_a_diagnosis(client, tmp_path):
    token = start_drill(client)
    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "3",
            "rationale": "The second option restates the stem.",
            "verification_method": "re-read the qualifier",
        },
    )
    client.post(f"/drill/{token}/answer", data={"answer": SECRET_KEY})
    panel = client.post(
        f"/drill/{token}/explain",
        data={"explanation": " ".join(["matched the stem to the option and checked"] * 4)},
    ).text
    assert "Briefing" in panel
    assert "prompt_version" in panel

    conn = db.connect(tmp_path / "web.db")
    row = conn.execute("SELECT attempt_id, item_id FROM attempts").fetchone()
    assert row is not None

    good = (
        f'```json\n{{"item_id": "{row["item_id"]}", "error_code": "execution_error", '
        '"one_fix": "check the carry"}\n```'
    )
    result = client.post(f"/drill/{token}/record", data={"pasted": good}).text
    assert "execution_error" in result
    assert "check the carry" in result

    stored = conn.execute(
        "SELECT payload_json FROM exchanges WHERE attempt_id = ?", (row["attempt_id"],)
    ).fetchone()
    assert stored["payload_json"] is not None

    # Refreshing a finished drill shows what was recorded, and recording again
    # is refused -- otherwise a reload writes a second diagnosis for one
    # attempt, and the page claims nothing was written when something was.
    reloaded = client.get(f"/drill/{token}").text
    assert "check the carry" in reloaded
    assert "Record diagnosis" not in reloaded

    again = client.post(f"/drill/{token}/record", data={"pasted": good}).text
    assert "already has a diagnosis" in again
    assert conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0] == 1
    conn.close()


def test_a_mismatched_diagnosis_is_rejected_inline_and_names_both_ids(client):
    token = start_drill(client)
    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "3",
            "rationale": "The second option restates the stem.",
            "verification_method": "re-read",
        },
    )
    client.post(f"/drill/{token}/answer", data={"answer": SECRET_KEY})
    client.post(
        f"/drill/{token}/explain",
        data={"explanation": " ".join(["matched the stem to the option and checked"] * 4)},
    )

    bad = (
        '```json\n{"item_id": "a-stale-tab", "error_code": "execution_error", '
        '"one_fix": "check it"}\n```'
    )
    result = client.post(f"/drill/{token}/record", data={"pasted": bad}).text
    assert "mismatch" in result
    assert "a-stale-tab" in result       # both ids named, so the cause is obvious
    assert "Record diagnosis" in result  # and the form comes back to try again


# ----------------------------------------------------------- refreshing


def test_refreshing_redraws_the_phase_instead_of_restarting(client):
    token = start_drill(client)
    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "2",
            "rationale": "The second option restates the stem.",
            "verification_method": "re-read",
        },
    )
    first = client.get(f"/drill/{token}").text
    second = client.get(f"/drill/{token}").text
    assert "Your answer" in first
    assert "Before you answer" not in first
    assert first == second


def test_an_expired_token_is_a_page_not_a_traceback(client):
    response = client.get("/drill/not-a-real-token")
    assert response.status_code == 410
    assert "Nothing was recorded" in response.text
