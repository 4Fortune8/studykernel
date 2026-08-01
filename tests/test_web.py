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
from kernel.pedagogy import explain_back  # noqa: E402
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


# What varies between the two kinds of item the answer control has to serve.
# The fixture takes one of these via `indirect=True` so a test about the
# numeric box is one parametrize line rather than a second copy of the pack.
MC_ITEM = {
    "item_type": "mc",
    "choices": ["alpha-1", SECRET_KEY, "gamma-3", "delta-4"],
    "answer_key": SECRET_KEY,
}
NUMERIC_ITEM = {
    "item_type": "numeric",
    "choices": None,
    "answer_key": "13",
}


@pytest.fixture
def client(request, tmp_path, monkeypatch):
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
                "stem": "Pick the right one.",
                "source": "test",
                "license": "test",
                "tags": [{"slug": "fractions", "label_source": "official", "reviewed": 1}],
                **getattr(request, "param", MC_ITEM),
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


# Distinctive enough to assert on: the gate hands the blind rationale back
# after a hit, so a test needs to recognise it in the page.
RATIONALE = "The second option restates the stem."


def work_item(client, token, answer=SECRET_KEY, **overrides):
    """Capture and answer in one post, because they are one form now (§4.1).

    Defaults are a valid submission so that a test overriding one field is
    exercising that field and nothing else -- `work_item(client, token,
    rationale="no")` reads as "a bad rationale", which is what it tests.
    """
    data = {
        "confidence": "3",
        "rationale": RATIONALE,
        "verification_method": "re-read the qualifier",
        "answer": answer,
    }
    data.update(overrides)
    return client.post(f"/drill/{token}/capture", data=data)


CHOICE_LIST = re.compile(r'<ol[^>]*\bclass="[^"]*\bchoices\b[^"]*"[^>]*>.*?</ol>', re.S)
# The same four options rendered as the answer control instead. A selectable
# item shows one or the other, never both -- see drill/page.html.
CHOICE_RADIOS = re.compile(
    r'<fieldset[^>]*\bclass="[^"]*\banswer\b[^"]*"[^>]*>.*?</fieldset>', re.S
)


def without_choices(html: str) -> str:
    """The page minus the rendered choice list, in whichever form it took.

    The keyed string legitimately appears there, as one option among four --
    that is the item. Everywhere else is a leak.

    Matched loosely on the class attribute on purpose: an earlier version
    anchored on `class="choices"` exactly and silently stopped stripping
    anything the moment a second class was added, turning the leak test into
    a test that fails on markup changes. A safety test that cries wolf gets
    muted, which is the one outcome this test cannot afford.
    """
    stripped = CHOICE_RADIOS.sub("", CHOICE_LIST.sub("", html))
    assert stripped != html, "no choice rendering found -- the strip patterns are stale"
    return stripped


# ------------------------------------------------- §4.1 the key is not sent


def test_the_key_is_not_in_the_capture_page(client):
    token = start_drill(client)
    body = client.get(f"/drill/{token}").text
    assert SECRET_KEY in body, "sanity: the item should render at all"
    assert SECRET_KEY not in without_choices(body)


def test_a_rejected_submission_does_not_leak_the_key_either(client):
    """The re-rendered form is a pre-verdict page and has to stay one.

    This replaces a test of the old standalone answer page. Now that capture
    and answer post together, the moment worth guarding is the *rejected*
    submission -- the one path that redraws a pre-grading panel after the
    learner has already typed an answer.
    """
    token = start_drill(client)
    panel = work_item(client, token, answer="").text
    assert "an answer is required" in panel
    assert SECRET_KEY not in without_choices(panel)


def test_the_key_arrives_with_the_verdict(client):
    token = start_drill(client)
    panel = work_item(client, token).text
    assert SECRET_KEY in panel
    assert "Correct" in panel


def test_an_answer_is_required(client, tmp_path):
    """A slipped return key must not cost an item. See `session.BlankAnswer`.

    Both halves matter: the form comes back with the capture still in it, and
    nothing at all is recorded -- a blank answer used to grade as wrong and
    persist as a real attempt at whatever hint level had been served.
    """
    token = start_drill(client)
    panel = work_item(client, token, answer="   ").text
    assert "an answer is required" in panel
    assert "Lock this in" in panel, "the form comes back, not a dead end"
    assert "The second option restates the stem." in panel, "capture is preserved"
    assert "Correct" not in panel and "Wrong" not in panel

    conn = db.connect(tmp_path / "web.db")
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    conn.close()


def test_capture_and_answer_are_one_page(client):
    """One form, one round trip -- the answer box is not on a page of its own."""
    body = client.get(f"/drill/{start_drill(client)}").text
    assert "Before you answer" in body
    assert 'name="answer"' in body
    assert 'name="rationale"' in body
    # One place to answer, and one form to post it in. Two of either means the
    # split page came back, which is the bug this replaced.
    assert len(CHOICE_RADIOS.findall(body)) == 1
    assert len(re.findall(r"<form[^>]*hx-post", body)) == 2  # the form, plus hints


def test_a_multiple_choice_item_is_answered_by_selection(client):
    """Options are picked, not transcribed. `grading.choice_values` decides
    what each one submits, because packs disagree about what the key is."""
    body = client.get(f"/drill/{start_drill(client)}").text
    radios = re.findall(r'<input type="radio" name="answer" value="([^"]*)"', body)
    assert len(radios) == 4, "one per option"
    assert SECRET_KEY in radios, "this pack keys by option text"

    # And the options are shown once, not once as a list and again as radios.
    assert not CHOICE_LIST.findall(body)


def test_the_options_come_back_marked_in_the_answer_swap(client):
    """"Key: C -- you answered B" is two bare letters without the options.

    They live above `#panel` and every answer posts with `hx-target="#panel"`,
    so the swap has to carry them out of band or they stay gone until a manual
    refresh -- which is precisely when a wrong answer needs them most.
    """
    token = start_drill(client)
    body = client.get(f"/drill/{token}").text
    radios = re.findall(r'<input type="radio" name="answer" value="([^"]*)"', body)
    assert not CHOICE_LIST.findall(body), "sanity: the radios are the only copy"

    panel = work_item(client, token, answer=radios[0]).text  # B is the key, so A is wrong
    assert 'id="choices"' in panel and 'hx-swap-oob="true"' in panel
    assert CHOICE_LIST.findall(panel), "the options are back"
    assert 'class="opt given"' in panel and 'class="opt key"' in panel


def test_a_correct_answer_marks_one_option_as_both(client):
    """The key and the answer given are the same option; it must not print twice."""
    token = start_drill(client)
    panel = work_item(client, token).text
    assert 'class="opt both"' in panel
    assert 'class="opt given"' not in panel and 'class="opt key"' not in panel


def test_the_marked_options_survive_a_refresh(client):
    """Swap and refresh render the same partial, so they cannot disagree."""
    token = start_drill(client)
    work_item(client, token, answer="not-it")
    page = client.get(f"/drill/{token}").text
    assert CHOICE_LIST.findall(page)
    assert 'class="opt key"' in page
    # The full page is not a swap target, so it must not claim to be one.
    assert 'hx-swap-oob' not in page


def test_selecting_the_keyed_option_grades_correct(client):
    """The end of the round trip: what a radio submits is what `grade` reads."""
    token = start_drill(client)
    body = client.get(f"/drill/{token}").text
    radios = re.findall(r'<input type="radio" name="answer" value="([^"]*)"', body)
    panel = work_item(client, token, answer=radios[1]).text  # SECRET_KEY is option B
    assert "Correct" in panel


@pytest.mark.parametrize("client", [NUMERIC_ITEM], indirect=True)
def test_a_numeric_item_gets_a_box_that_will_not_take_letters(client):
    """The other half of "answered by selection, not transcription".

    A radio cannot be mistyped; a text box can, and the way it gets mistyped is
    that the learner solves for N, gets 13, and submits `N`. The box is
    constrained to what `grade` can read so that mistake stops being a wrong
    answer in the data.
    """
    from kernel.pedagogy import grading

    body = client.get(f"/drill/{start_drill(client)}").text
    assert 'type="radio"' not in body, "nothing to select on a numeric item"
    box = re.search(r'<input name="answer"[^>]*>', body)
    assert box, "a numeric item is answered in a text box"
    assert 'pattern="' in box.group(0)

    # The pattern is checked as the constant rather than as the rendered
    # attribute, so an escaping change in the template cannot make this pass on
    # markup no browser would enforce.
    pattern = re.compile(grading.NUMERIC_INPUT_PATTERN)
    for good in ("13", "-13", "0.5", ".50", "1/2", "1,000", "13.", " 13 ", r"\frac{1}{2}"):
        assert pattern.fullmatch(good), f"the grader reads {good!r}; the box must take it"
    for bad in ("N", "N = 13", "thirteen", "13 apples", "x"):
        assert not pattern.fullmatch(bad), bad

    # And the two agree on the forms of 13 that are 13: a guard rail that let
    # through something the grader then marked wrong would be worse than none.
    assert all(grading.grade(form, "13") for form in ("13", "13.", " 13 ", "13.0"))


@pytest.mark.parametrize("client", [NUMERIC_ITEM], indirect=True)
def test_the_numeric_box_says_nothing_about_which_number(client):
    """The shape is a constant, not a projection of the key (§4.1).

    A box narrowed to `^\\d{2}$` would say "two digits" and a box that took no
    sign would say "positive". This one is the same string for every numeric
    key, so the only thing on the page is "a number goes here".
    """
    from kernel.pedagogy import grading

    body = client.get(f"/drill/{start_drill(client)}").text
    assert "13" not in body
    assert grading.input_shape("13") == grading.input_shape("-0.5") == grading.NUMERIC
    assert grading.input_shape("7x") is None, "an expression keeps a plain box"
    assert grading.input_shape("no") is None
    assert grading.input_shape(None) is None


@pytest.mark.parametrize("client", [NUMERIC_ITEM], indirect=True)
def test_the_numeric_box_still_grades_what_it_accepts(client):
    """The guard rail is in front of the grader, not in place of it."""
    token = start_drill(client)
    assert "Correct" in work_item(client, token, answer="13").text


def test_a_numeric_item_still_gets_a_text_box(client):
    """No choices, nothing to select -- `choice_values` is None and says so."""
    from kernel.pedagogy import grading

    assert grading.choice_values(None, "42") is None
    assert grading.choice_values([], "42") is None
    # A key matching neither a letter nor an option cannot be offered as one.
    assert grading.choice_values(["alpha", "beta"], "gamma") is None
    assert grading.choice_values(["alpha", "beta"], "C") is None, "out of range"
    assert grading.choice_values(["alpha", "beta"], "B") == ["A", "B"]
    assert grading.choice_values(["alpha", "beta"], "beta") == ["alpha", "beta"]


# --------------------------------------------------- §4.2 observed hints


def test_a_rung_served_over_http_floors_the_recorded_level(client):
    token = start_drill(client)
    rung = client.post(f"/drill/{token}/hint", data={"level": "3"})
    assert "L3" in rung.text

    panel = work_item(
        client, token, confidence="1", rationale="Guessing from the shape of the options."
    ).text
    assert "L3" in panel


def test_hints_are_refused_once_the_key_is_out(client):
    token = start_drill(client)
    work_item(client, token)
    with pytest.raises(Exception):
        client.post(f"/drill/{token}/hint", data={"level": "1"})


# ------------------------------------------------------- §4.3 the gate


def test_a_rejected_capture_re_renders_the_form_with_the_reason(client):
    token = start_drill(client)
    panel = work_item(client, token, rationale="no").text
    assert "rationale must be a real sentence" in panel
    assert "Lock this in" in panel


def test_the_gate_must_pass_before_anything_is_recorded(client, tmp_path):
    token = start_drill(client)
    work_item(client, token)

    panel = client.post(f"/drill/{token}/explain", data={"explanation": "dunno"}).text
    assert "under 3 words" in panel

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
    body = work_item(client, token).text

    posts = set(re.findall(r'hx-post="([^"]+)"', body))
    assert posts == {f"/drill/{token}/explain"}
    # And nothing that navigates away from it either.
    assert not re.findall(r"<a\s[^>]*href=", body)


def test_the_gate_still_has_no_skip_when_the_answer_was_right(client):
    """The framing changes on a hit; the gate does not.

    Worth its own test because "I got it right" is exactly the case where a
    skip button gets asked for, and the panel is rendered by a different
    branch of the template than the one the test above covers.
    """
    token = start_drill(client)
    body = work_item(client, token).text
    assert "Correct" in body, "sanity: this is the correct-answer branch"

    posts = set(re.findall(r'hx-post="([^"]+)"', body))
    assert posts == {f"/drill/{token}/explain"}
    assert not re.findall(r"<a\s[^>]*href=", body)


def test_the_gate_asks_a_different_question_when_the_answer_was_right(client):
    """A hit must not re-ask the question the blind capture already asked.

    The two used to share wording verbatim, so a correct answer was asked "why
    is your answer right?" twice with nothing new to say the second time. On a
    hit the gate now hands the rationale back and asks whether it *held*, which
    is the question the verdict just created.
    """
    right = work_item(client, start_drill(client)).text
    wrong = work_item(client, start_drill(client), answer="not-it").text

    assert "Did it hold for the reason you gave?" in right
    assert RATIONALE in right, "the blind rationale is handed back, not re-asked"
    assert "Explain it back" not in right

    assert "Explain it back" in wrong
    assert "Did it hold for the reason" not in wrong
    assert RATIONALE not in wrong, "a miss must not be pre-filled with wrong reasoning"


def test_a_short_justification_passes_the_gate_after_a_correct_answer(client, tmp_path):
    """Five words is a reason, not a dodge -- pedagogy/explain_back."""
    token = start_drill(client)
    work_item(client, token)
    panel = client.post(
        f"/drill/{token}/explain",
        data={"explanation": "both sides divide by three"},
    ).text
    assert "Briefing" in panel

    conn = db.connect(tmp_path / "web.db")
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    conn.close()


def test_a_miss_can_be_answered_with_not_knowing_where_to_start(client, tmp_path):
    """The declaration is an answer to the gate, not a way around it.

    It records an attempt like any other path would -- that is what separates
    it from the skip `explain_back` still refuses to grow -- and it reaches the
    briefing as a declaration rather than as an empty explanation.
    """
    token = start_drill(client)
    work_item(client, token, answer="not-it")
    panel = client.post(
        f"/drill/{token}/explain", data={"explanation": "", "stuck": "1"}
    ).text
    assert "Briefing" in panel
    # Substring rather than the constant: the briefing sits in a textarea, so
    # the apostrophe in the declaration arrives HTML-escaped.
    assert explain_back.STUCK_DECLARATION.split("'")[-1] in panel

    conn = db.connect(tmp_path / "web.db")
    row = conn.execute("SELECT correct, stuck FROM attempts").fetchone()
    conn.close()
    assert (row["correct"], row["stuck"]) == (0, 1)


def test_the_stuck_declaration_tells_the_reader_to_teach_not_to_correct(client):
    """A reader with no path to correct must not invent one to diagnose."""
    token = start_drill(client)
    work_item(client, token, answer="not-it")
    panel = client.post(
        f"/drill/{token}/explain", data={"explanation": "", "stuck": "1"}
    ).text
    assert "did not know where to start" in panel
    assert "Teach this item from the beginning" in panel


# --------------------------------------------------- the whole loop


def test_the_full_loop_records_an_attempt_and_a_diagnosis(client, tmp_path):
    token = start_drill(client)
    work_item(client, token)
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
    work_item(client, token)
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
    work_item(client, token)
    first = client.get(f"/drill/{token}").text
    second = client.get(f"/drill/{token}").text
    assert "Did it hold for the reason" in first  # the gate, not the capture form
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
    work_item(client, token, answer=answer)
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
