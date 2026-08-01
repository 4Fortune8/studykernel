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


# ------------------------------------------------------ subject focus


def test_the_subject_picker_lists_the_products_sections(client):
    page = client.get("/").text
    assert "Quantitative" in page       # display_name from the pack, not a constant
    assert "/?section=quant" in page
    assert "/?section=" in page          # "everything"


def chip_classes(html: str, href: str) -> str:
    """The class attribute of the subject chip pointing at `href`.

    Matched structurally rather than as a literal substring: the anchor spans
    several lines, so a whitespace-sensitive assertion tests the template's
    formatting instead of its behaviour.
    """
    match = re.search(
        r'<a\s+class="([^"]*)"\s*\n?\s*href="' + re.escape(href) + r'"', html
    )
    return match.group(1) if match else ""


def test_choosing_a_subject_sticks_across_requests(client):
    client.get("/?section=quant")
    assert client.cookies.get("studykernel_subject") == "quant"
    # And is applied on the next visit, with no query param in sight.
    page = client.get("/").text
    assert "on" in chip_classes(page, "/?section=quant").split()
    assert "on" not in chip_classes(page, "/?section=").split()


def test_clearing_the_subject_goes_back_to_everything(client):
    client.get("/?section=quant")
    client.get("/?section=")
    assert not client.cookies.get("studykernel_subject")


def test_a_stale_subject_cookie_means_everything_not_a_crash(client):
    """A pack that renames a section leaves cookies pointing at nothing.

    The kernel rejects an unknown section outright, so the web layer has to
    validate before passing it on -- otherwise an old cookie is a 500 on the
    home page with no obvious cause.
    """
    client.cookies.set("studykernel_subject", "a-section-that-was-removed")
    response = client.get("/")
    assert response.status_code == 200


def test_the_start_button_carries_the_subject(client):
    page = client.get("/?section=quant").text
    assert '<input type="hidden" name="section" value="quant">' in page


# ------------------------------------- skipping the exchange, and history


def finish_a_drill(client, answer=SECRET_KEY):
    """Run one item all the way to the exchange panel. Returns the token."""
    token = start_drill(client)
    client.post(
        f"/drill/{token}/capture",
        data={
            "confidence": "3",
            "rationale": "The second option restates the stem.",
            "verification_method": "re-read the qualifier",
        },
    )
    client.post(f"/drill/{token}/answer", data={"answer": answer})
    client.post(
        f"/drill/{token}/explain",
        data={"explanation": " ".join(["matched the stem to the option and checked"] * 4)},
    )
    return token


def test_a_correct_answer_offers_to_skip_the_tutoring(client):
    """The ease-of-use complaint: a clean solve should not demand a tutor.

    DESIGN.md §10 makes the exchange optional -- "no API is required for the
    system to work" -- so this is surfacing an existing property, not relaxing
    one. The explain-back gate is upstream and has already passed.
    """
    token = finish_a_drill(client)
    panel = client.get(f"/drill/{token}").text
    assert f"/drill/{token}/waive" in panel
    assert "skip the tutoring" in panel


def test_skipping_keeps_the_attempt_and_the_briefing(client, tmp_path):
    token = finish_a_drill(client)
    response = client.post(f"/drill/{token}/waive", follow_redirects=False)
    assert response.status_code == 303

    conn = db.connect(tmp_path / "web.db")
    row = conn.execute(
        "SELECT attempt_id, exchange_waived_at FROM attempts"
    ).fetchone()
    assert row["exchange_waived_at"] is not None
    # Nothing thrown away: the briefing is still there to come back to.
    assert db.load_briefing(conn, row["attempt_id"]) is not None
    conn.close()


def test_a_skipped_attempt_can_be_picked_up_later(client, tmp_path):
    finish_a_drill(client)
    conn = db.connect(tmp_path / "web.db")
    row = conn.execute("SELECT attempt_id, item_id FROM attempts").fetchone()
    attempt_id, item_id = row["attempt_id"], row["item_id"]
    conn.close()

    page = client.get(f"/history/{attempt_id}").text
    assert "Exchange still open" in page or "Exchange skipped" in page
    assert "Copy briefing" in page

    good = (
        f'```json\n{{"item_id": "{item_id}", "error_code": "knowledge_gap", '
        '"one_fix": "review the definition"}\n```'
    )
    result = client.post(f"/history/{attempt_id}/record", data={"pasted": good}).text
    assert "knowledge_gap" in result
    assert "review the definition" in result


def test_recording_later_clears_the_waiver(client, tmp_path):
    token = finish_a_drill(client)
    client.post(f"/drill/{token}/waive", follow_redirects=False)

    conn = db.connect(tmp_path / "web.db")
    row = conn.execute(
        "SELECT attempt_id, item_id, exchange_waived_at FROM attempts"
    ).fetchone()
    assert row["exchange_waived_at"] is not None

    good = (
        f'```json\n{{"item_id": "{row["item_id"]}", "error_code": "knowledge_gap", '
        '"one_fix": "review the definition"}\n```'
    )
    client.post(f"/history/{row['attempt_id']}/record", data={"pasted": good})

    after = conn.execute(
        "SELECT exchange_waived_at FROM attempts WHERE attempt_id = ?",
        (row["attempt_id"],),
    ).fetchone()
    assert after["exchange_waived_at"] is None, "a diagnosis supersedes a skip"
    conn.close()


def test_history_groups_by_category_and_outcome(client):
    finish_a_drill(client)                      # correct
    finish_a_drill(client, answer="not-it")     # wrong

    page = client.get("/history").text
    assert "fractions" in page
    assert "correct" in page and "wrong" in page

    only_wrong = client.get("/history?outcome=wrong").text
    assert only_wrong.count("<tbody>") >= 1
    assert "not-it" not in only_wrong  # the table shows stems, not answers

    open_only = client.get("/history?state=open").text
    assert "Every attempt" in open_only


def test_an_attempt_from_another_profile_is_not_visible(client, tmp_path):
    finish_a_drill(client)
    conn = db.connect(tmp_path / "web.db")
    attempt_id = conn.execute("SELECT attempt_id FROM attempts").fetchone()[0]
    db.ensure_learner(conn, "someone-else", "Someone Else")
    conn.close()

    client.cookies.set("studykernel_profile", "someone-else")
    response = client.get(f"/history/{attempt_id}")
    assert response.status_code == 404


# --------------------------------------------------- starting up wrong


def test_a_missing_pack_stops_startup_and_names_the_paths(tmp_path, monkeypatch):
    """The failure that actually happened: launched from the wrong directory.

    `STUDY_PRODUCT` is relative, so it resolved against `web/` and the pack
    was not there. The server started anyway and 500ed on the home page,
    because startup only checked that the variable was *set*.
    """
    monkeypatch.setenv("STUDY_PRODUCT", "products/nope")
    monkeypatch.setenv("STUDY_DB", str(tmp_path / "x.db"))

    from web import deps

    with pytest.raises(deps.NoProductConfigured) as caught:
        deps.preflight()
    message = str(caught.value)
    assert "working directory" in message
    assert str(tmp_path.cwd()) in message or "products/nope" in message


def test_a_missing_database_is_refused_rather_than_created(tmp_path, monkeypatch):
    """The worse half of the same mistake.

    `db.connect` creates the file, so a wrong `STUDY_DB` silently produced an
    empty database -- a learner with no history, no items and no ratings, which
    looks like a real profile until you notice nothing is in it. One was
    created for real this way.
    """
    pack_dir = tmp_path / "pack"
    (pack_dir / "taxonomy").mkdir(parents=True)
    (pack_dir / "product.yaml").write_text(PRODUCT_YAML)
    (pack_dir / "objective.yaml").write_text(OBJECTIVE_YAML)
    (pack_dir / "taxonomy" / "tags.yaml").write_text(TAGS_YAML)

    missing = tmp_path / "nowhere" / "study.db"
    monkeypatch.setenv("STUDY_PRODUCT", str(pack_dir))
    monkeypatch.setenv("STUDY_DB", str(missing))

    from web import deps

    with pytest.raises(deps.NoDatabase) as caught:
        deps.preflight()
    assert "study init" in str(caught.value)
    assert not missing.exists(), "preflight must not create the database it rejects"


def test_preflight_passes_on_a_real_setup(client, tmp_path, monkeypatch):
    """And does not cry wolf: the fixture's own environment must satisfy it."""
    from web import deps

    deps.preflight()
