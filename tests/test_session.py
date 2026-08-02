"""`kernel.session` -- the drill loop as a service. WEB_UI.md §2.

These invariants were previously enforced by `cmd_drill`'s control flow and
were therefore untestable: the sequence only existed interleaved with
`input()`. That is exactly the risk WEB_UI.md §8 names -- two front ends, one
convention, silent drift -- so the tests live here now, next to the service
both front ends will call.

The blind-capture test (§4.1) is the one that matters most. It asserts on the
*type*: `Served` has no key field, so a template cannot leak one.
"""

from __future__ import annotations

import dataclasses
import inspect
import json

import pytest

from kernel import config, session
from kernel.pedagogy import capture as capture_mod
from kernel.pedagogy import explain_back, hints
from kernel.storage import db

PRODUCT_YAML = """
product_id: practice-pack
display_name: "A pack that exists only in this test"
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
      # Above where a default-rated learner starts, so the gradient is real
      # and `start()` has something to serve.
      - "ability >= 980"
"""

TAGS_YAML = """
tags:
  - slug: fractions
    display_name: "Fractions"
    section: quant
edges: []
"""

# Deliberately not a bare integer: the target band and the item ids are full
# of small numbers, and a leak test that greps for "17" would pass on "1780".
SECRET_KEY = "17/3"


@pytest.fixture
def pack(tmp_path):
    (tmp_path / "product.yaml").write_text(PRODUCT_YAML)
    (tmp_path / "objective.yaml").write_text(OBJECTIVE_YAML)
    (tmp_path / "taxonomy").mkdir()
    (tmp_path / "taxonomy" / "tags.yaml").write_text(TAGS_YAML)
    return config.load_product(tmp_path)


@pytest.fixture
def conn(tmp_path, pack):
    connection = db.connect(tmp_path / "t.db")
    db.migrate(connection)
    db.upsert_product(connection, pack, "digest")
    db.upsert_taxonomy(connection, pack["product_id"], pack["_tags"], pack["_edges"])
    db.ensure_learner(connection, "learner")
    db.insert_passage(
        connection,
        {
            "passage_id": "p1",
            "text": "A passage the item hangs off.",
            "source": "test",
            "license": "test",
        },
    )
    db.insert_items(
        connection,
        [
            {
                "item_id": f"i{n}",
                "product_id": pack["product_id"],
                "section": "quant",
                "item_type": "mc",
                "stem": "Write 34/6 in lowest terms.",
                "choices": ["16/3", SECRET_KEY, "18/3", "19/3"],
                "answer_key": SECRET_KEY,
                "official_expl": None,
                "passage_id": "p1",
                "source": "test",
                "license": "test",
                "tags": [{"slug": "fractions", "label_source": "official", "reviewed": 1}],
            }
            for n in range(6)
        ],
    )
    return connection


@pytest.fixture
def drill(conn, pack):
    """A fresh service with a private store, so tests cannot see each other."""
    return session.DrillSession(conn, pack, "learner", store=session.DrillStore())


@pytest.fixture
def drill_store_session(conn, pack):
    """A session plus a handle on its store, for the expiry tests."""
    store = session.DrillStore()
    return session.DrillSession(conn, pack, "learner", store=store), store


def good_capture():
    return capture_mod.Capture(
        confidence=2,
        rationale="Adding eight and nine carries once past ten.",
        verification_method="back_substitution,unit_check",
    )


def advance_to_gate(drill, answer=SECRET_KEY):
    """Run a drill up to the explain-back gate. Returns (served, verdict)."""
    served = drill.start()
    drill.submit_capture(served.token, good_capture())
    verdict = drill.submit_answer(served.token, answer)
    return served, verdict


# ------------------------------------------------- §4.1 the key is not sent


def test_the_served_view_has_no_answer_key_field():
    """Structural, not behavioral. There is nothing for a template to ask for."""
    names = {f.name for f in dataclasses.fields(session.Served)}
    assert "answer_key" not in names
    assert not any("key" in n or "answer" in n for n in names)


def test_the_key_is_absent_from_everything_served_before_the_answer(drill):
    """The one test WEB_UI.md §4.1 asks for, at the layer that owns the data.

    The keyed string legitimately appears in `choices` -- as one option among
    four, which is exactly what the learner is meant to see. What must not
    escape is *which* one, and no other field carries it.
    """
    served = drill.start()
    payload = dataclasses.asdict(served)
    choices = payload.pop("choices")
    values = payload.pop("choice_values")

    assert choices.count(SECRET_KEY) == 1
    assert SECRET_KEY not in repr(payload)

    # `choice_values` is popped alongside `choices` because it is parallel to
    # it, not because it is exempt: it says what *each* option submits, one
    # entry per option, and is either the letters or the options themselves.
    # A short list -- one entry, or one marked out from the rest -- would be
    # the leak this test exists to catch, so length is asserted, not waived.
    assert len(values) == len(choices)
    assert values == choices or values == list("ABCDEFGH"[: len(choices)])


def test_the_key_arrives_only_with_the_verdict(drill):
    served = drill.start()
    drill.submit_capture(served.token, good_capture())
    verdict = drill.submit_answer(served.token, SECRET_KEY)
    assert verdict.answer_key == SECRET_KEY
    assert verdict.correct is True


# ------------------------------------------------------ phase ordering


def test_answering_before_capturing_is_refused(drill):
    served = drill.start()
    with pytest.raises(session.PhaseError):
        drill.submit_answer(served.token, SECRET_KEY)


def test_the_gate_cannot_be_reached_before_the_answer(drill):
    served = drill.start()
    drill.submit_capture(served.token, good_capture())
    with pytest.raises(session.PhaseError):
        drill.submit_explain_back(served.token, "word " * 30)


def test_a_briefing_cannot_be_had_before_the_gate_passes(drill):
    served, _ = advance_to_gate(drill)
    with pytest.raises(session.PhaseError):
        drill.briefing(served.token)


def test_a_rejected_capture_does_not_advance_the_phase(drill):
    served = drill.start()
    with pytest.raises(capture_mod.CaptureError):
        drill.submit_capture(served.token, capture_mod.Capture(confidence=9, rationale="x"))
    with pytest.raises(session.PhaseError):
        drill.submit_answer(served.token, SECRET_KEY)


def test_an_unknown_token_is_a_clean_error_not_a_key_error(drill):
    with pytest.raises(session.UnknownDrill):
        drill.submit_answer("not-a-token", SECRET_KEY)


# ------------------------------------------------------- §4.3 the gate


def test_a_failed_gate_persists_nothing(drill, conn):
    served, _ = advance_to_gate(drill)
    gate = drill.submit_explain_back(served.token, "dunno")
    assert gate.passed is False
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_the_gate_can_be_retried_and_then_records(drill, conn):
    served, _ = advance_to_gate(drill)
    assert drill.submit_explain_back(served.token, "too short").passed is False
    long_enough = " ".join(["carried", "the", "ten", "then", "added"] * 4)
    assert drill.submit_explain_back(served.token, long_enough).passed is True
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1


def test_not_knowing_where_to_start_passes_the_gate_and_is_recorded(drill, conn):
    """Escalation, not evasion. See `pedagogy/explain_back.check`.

    The learner who had no method has nothing to articulate, and demanding a
    path from them yields a fabricated one -- which the briefing then reads as
    their actual reasoning and diagnoses a divergence that never happened. So
    the declaration passes, and it is stored as a declaration.
    """
    served, _ = advance_to_gate(drill, answer="wrong")
    assert drill.submit_explain_back(served.token, "", stuck=True).passed is True

    row = conn.execute("SELECT stuck FROM attempts").fetchone()
    assert row["stuck"] == 1
    assert explain_back.STUCK_DECLARATION in drill.briefing(served.token).text


def test_the_gate_still_refuses_an_empty_explanation_without_the_declaration(drill, conn):
    """The declaration has to be made, not merely implied by leaving it blank."""
    served, _ = advance_to_gate(drill, answer="wrong")
    assert drill.submit_explain_back(served.token, "   ").passed is False
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_no_method_takes_a_skip_flag():
    """DESIGN.md principle 10's sole exception stays an exception.

    `explain_back` deliberately offers no way to add a skip; a service that
    quietly grew one would move the decision out of the module that refused
    to make it. So the absence is asserted rather than assumed.
    """
    forbidden = {"skip", "skip_gate", "bypass", "force", "resolved"}
    for name in dir(session.DrillSession):
        if name.startswith("_"):
            continue
        method = getattr(session.DrillSession, name)
        params = set(inspect.signature(method).parameters)
        assert not params & forbidden, f"DrillSession.{name} grew a skip path"


# --------------------------------------------- §4.2 hints are observed


def test_a_served_rung_floors_the_recorded_hint_level(drill):
    served = drill.start()
    drill.request_hint(served.token, 3)
    drill.submit_capture(served.token, good_capture())
    # The learner reports L0; three rungs were actually handed over.
    verdict = drill.submit_answer(served.token, SECRET_KEY, reported_hint_level=0)
    assert verdict.min_hint_level == 3


def test_a_higher_self_report_is_taken_at_its_word(drill):
    served = drill.start()
    drill.request_hint(served.token, 1)
    drill.submit_capture(served.token, good_capture())
    verdict = drill.submit_answer(served.token, SECRET_KEY, reported_hint_level=4)
    assert verdict.min_hint_level == 4


def test_hint_levels_are_not_requestable_after_the_verdict(drill):
    served, _ = advance_to_gate(drill)
    with pytest.raises(session.PhaseError):
        drill.request_hint(served.token, 2)


def test_the_ladder_does_not_unroll(drill):
    """One call, one rung -- never the whole ladder in one response."""
    served = drill.start()
    rung = drill.request_hint(served.token, 2)
    assert isinstance(rung, hints.Rung)
    assert rung.level == 2


def test_an_out_of_range_rung_is_refused(drill):
    served = drill.start()
    with pytest.raises(ValueError):
        drill.request_hint(served.token, hints.MAX_LEVEL + 1)


# ------------------------------------------------------- the exchange


def test_the_full_loop_records_an_attempt_and_an_exchange(drill, conn):
    served, _ = advance_to_gate(drill)
    drill.submit_explain_back(served.token, " ".join(["step"] * 20))
    briefing = drill.briefing(served.token)

    assert briefing.attempt_id > 0
    assert briefing.prompt_version
    assert "LEARNER'S EXPLAIN-BACK" in briefing.text
    row = conn.execute(
        "SELECT prompt_version FROM exchanges WHERE attempt_id = ?", (briefing.attempt_id,)
    ).fetchone()
    assert row["prompt_version"] == briefing.prompt_version


def test_the_briefing_is_logged_once_even_if_fetched_twice(drill, conn):
    served, _ = advance_to_gate(drill)
    drill.submit_explain_back(served.token, " ".join(["step"] * 20))
    first = drill.briefing(served.token)
    second = drill.briefing(served.token)
    assert first.text == second.text
    assert conn.execute("SELECT COUNT(*) FROM exchanges").fetchone()[0] == 1


def test_a_diagnosis_for_another_item_is_rejected(drill):
    served, _ = advance_to_gate(drill)
    drill.submit_explain_back(served.token, " ".join(["step"] * 20))
    briefing = drill.briefing(served.token)

    payload = (
        '```json\n{"item_id": "a-different-item", "error_code": "knowledge_gap", '
        '"one_fix": "practice this"}\n```'
    )
    with pytest.raises(session.record_mod.RecordError, match="mismatch"):
        drill.record(briefing.attempt_id, payload)


def test_a_well_formed_diagnosis_records(drill, conn):
    served, _ = advance_to_gate(drill)
    drill.submit_explain_back(served.token, " ".join(["step"] * 20))
    briefing = drill.briefing(served.token)

    payload = (
        f'```json\n{{"item_id": "{briefing.item_id}", "error_code": "execution_error", '
        '"one_fix": "check the carry"}\n```'
    )
    diagnosis = drill.record(briefing.attempt_id, payload)
    assert diagnosis.error_code == "execution_error"
    row = conn.execute(
        "SELECT payload_json FROM exchanges WHERE attempt_id = ?", (briefing.attempt_id,)
    ).fetchone()
    assert row["payload_json"] is not None


def test_recording_against_a_missing_attempt_is_refused(drill):
    with pytest.raises(session.record_mod.RecordError):
        drill.record(9999, "{}")


# ------------------------------------------ the relay: the same loop, sent
#
# DESIGN.md §16 v2. Every test here injects the sender, because what is worth
# checking about auto-send is not the socket -- it is that the sequence the
# clipboard path goes through is the sequence the relay path goes through.


def _stub(payload: dict, model: str = "test-model"):
    """A sender that returns `payload` and records what it was asked."""
    calls: list[tuple] = []

    def send(body, system=None, schema=None):
        calls.append((body, system, schema))
        return session.relay.RelayReply(
            text=json.dumps(payload), model=model, responder=f"api:{model}"
        )

    send.calls = calls  # type: ignore[attr-defined]
    return send


def _good_payload(item_id: str) -> dict:
    return {
        "item_id": item_id,
        "divergence": "You wrote 6 x 7 = 36.",
        "error_code": "execution_error",
        "prerequisite_gaps": [],
        "one_fix": "check the carry",
        "explanation": "Six sevens is 42, not 36.",
        "trigger_miss": False,
        "explain_back_ok": True,
        "explain_back_feedback": "",
        "disputed_key": False,
    }


def _brief(drill):
    served, _ = advance_to_gate(drill)
    drill.submit_explain_back(served.token, " ".join(["step"] * 20))
    return served.token, drill.briefing(served.token)


def test_a_relayed_reply_records_exactly_as_a_pasted_one_would(drill, conn):
    token, briefing = _brief(drill)
    diagnosis = drill.tutor(token, send=_stub(_good_payload(briefing.item_id)))

    assert diagnosis.error_code == "execution_error"
    assert diagnosis.divergence == "You wrote 6 x 7 = 36."
    row = conn.execute(
        "SELECT payload_json, responder FROM exchanges WHERE attempt_id = ?",
        (briefing.attempt_id,),
    ).fetchone()
    assert row["payload_json"] is not None
    # Who answered, not which prompt asked. The two are different questions and
    # `prompt_version` already answers the other one.
    assert row["responder"] == "api:test-model"


def test_a_pasted_reply_is_still_marked_as_one(drill, conn):
    _, briefing = _brief(drill)
    drill.record(
        briefing.attempt_id,
        json.dumps(_good_payload(briefing.item_id)),
    )
    row = conn.execute(
        "SELECT responder FROM exchanges WHERE attempt_id = ?", (briefing.attempt_id,)
    ).fetchone()
    assert row["responder"] == db.RESPONDER_PASTE


def test_the_relay_sends_the_item_as_data_and_the_task_as_a_system_turn(drill):
    token, briefing = _brief(drill)
    send = _stub(_good_payload(briefing.item_id))
    drill.tutor(token, send=send)

    body, system, schema = send.calls[0]  # type: ignore[attr-defined]
    assert briefing.item_id in body, "the item goes in the data turn"
    assert "YOUR TASK" in system, "the task goes in the system turn"
    assert "YOUR TASK" not in body
    # Under a schema the format contract is the decoder's job, not the prose's.
    assert "```json" not in system
    assert schema["propertyOrdering"], "sent under a response schema or not at all"


def test_the_relay_cannot_land_a_diagnosis_on_the_wrong_item(drill, conn):
    """The check that makes the protocol safe is on the inbound side, and
    replacing the clipboard with a socket must not have moved it."""
    token, briefing = _brief(drill)
    send = _stub(_good_payload("some-other-item"))

    with pytest.raises(session.record_mod.RecordError, match="mismatch"):
        drill.tutor(token, send=send)

    assert conn.execute("SELECT COUNT(*) FROM diagnoses").fetchone()[0] == 0


def test_a_relay_failure_leaves_the_attempt_and_the_briefing_intact(drill, conn):
    """A failed send is not a failed drill. The gate already passed, the
    attempt is durable, and the briefing is stored -- so the fallback is the
    ordinary copy/paste path rather than a lost item."""
    token, briefing = _brief(drill)

    def explode(*_args, **_kwargs):
        raise session.relay.RelayError("the tutoring API is unavailable right now")

    with pytest.raises(session.relay.RelayError):
        drill.tutor(token, send=explode)

    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 1
    assert db.load_briefing(conn, briefing.attempt_id) is not None
    # And the paste path still works afterwards, on the same attempt.
    diagnosis = drill.record_for(token, json.dumps(_good_payload(briefing.item_id)))
    assert diagnosis.error_code == "execution_error"


def test_an_exchange_picked_back_up_later_sends_the_stored_briefing(drill, conn):
    """No drill in hand, so no halves -- the stored briefing goes as one turn,
    which is the same request the learner would have pasted by hand."""
    token, briefing = _brief(drill)
    drill.finish(token)

    send = _stub(_good_payload(briefing.item_id))
    diagnosis = drill.tutor_attempt(briefing.attempt_id, send=send)

    assert diagnosis.error_code == "execution_error"
    body, system, _ = send.calls[0]  # type: ignore[attr-defined]
    assert system is None
    assert body == briefing.text


def test_sending_a_briefing_that_was_never_stored_is_refused(drill):
    with pytest.raises(session.record_mod.RecordError, match="nothing to send"):
        drill.tutor_attempt(9999, send=_stub({}))


# -------------------------------------------------------- store expiry


def test_an_abandoned_drill_expires(conn, pack):
    """Closing a tab is the normal way a drill ends, not an edge case.

    Without expiry the store grows for the life of the process, each entry
    holding an item row and whatever passage it hangs off.
    """
    drill = session.DrillSession(conn, pack, "learner", store=session.DrillStore(ttl_seconds=0))
    served = drill.start()
    with pytest.raises(session.UnknownDrill):
        drill.submit_capture(served.token, good_capture())


def test_sweeping_reclaims_abandoned_drills(conn, pack):
    store = session.DrillStore(ttl_seconds=0)
    drill = session.DrillSession(conn, pack, "learner", store=store)
    drill.start()
    drill.start()
    assert len(store) == 1  # `put` sweeps, so the second start already reclaimed the first
    assert store.sweep() == 1
    assert len(store) == 0


def test_working_on_a_drill_keeps_it_alive(conn, pack):
    """Expiry is idle time, not wall time since the item was served."""
    store = session.DrillStore(ttl_seconds=30)
    drill = session.DrillSession(conn, pack, "learner", store=store)
    served = drill.start()
    before = store.get(served.token).touched_at
    drill.request_hint(served.token, 1)
    assert store.get(served.token).touched_at >= before


def test_a_live_drill_is_not_swept(drill_store_session):
    drill, store = drill_store_session
    served = drill.start()
    assert store.sweep() == 0
    assert drill.submit_capture(served.token, good_capture()).token == served.token


# ------------------------------------------------ recommend() vs start()


def test_recommend_reserves_nothing(drill_store_session):
    """The whole reason it is separate from `start()`.

    A home page renders on every visit. If asking what to study also minted a
    token and reserved an item, refreshing would churn item selection and fill
    the store with drills nobody opened.
    """
    drill, store = drill_store_session
    for _ in range(5):
        assert isinstance(drill.recommend(), session.Recommendation)
    assert len(store) == 0


def test_recommend_and_start_agree_on_the_tag(drill):
    choice = drill.recommend()
    served = drill.start()
    assert served.tag_slug == choice.tag_slug
    assert served.target_band == choice.target_band


def test_recommend_carries_the_three_multiplicands_separately(drill):
    """Collapsing them to `priority` would hide which one is the blocker."""
    choice = drill.recommend()
    assert choice.priority == pytest.approx(
        choice.gradient * choice.learnability * choice.availability
    )


def test_recommend_reports_starvation_for_a_tag_with_no_items(drill):
    assert isinstance(drill.recommend(tag="no-such-tag"), session.Starved)


SATISFIED_OBJECTIVE_YAML = """
objective:
  type: threshold
  margin_policy: rating_deviation
  margin_multiplier: 1.0
  variables:
    self_score:
      kind: manual
      default: 0
  routes:
    only:
      - "self_score >= 5"
"""


def test_a_met_objective_recommends_nothing(tmp_path, conn, pack):
    """DESIGN.md principle 9. This is the state the whole design turns on.

    A tool that cannot say *stop studying* is an engagement product, so the
    satisfied case is a distinct type -- a front end cannot render a start
    button by forgetting to check a flag.
    """
    (tmp_path / "objective.yaml").write_text(SATISFIED_OBJECTIVE_YAML)
    met = config.load_product(tmp_path)
    db.upsert_product(conn, met, "digest")
    db.set_manual_value(conn, "learner", met["product_id"], "self_score", 9.0)

    drill = session.DrillSession(conn, met, "learner", store=session.DrillStore())
    assert isinstance(drill.recommend(), session.Satisfied)
    assert isinstance(drill.start(), session.Satisfied)


TWO_SECTION_PRODUCT_YAML = """
product_id: practice-pack
display_name: "A pack with two subjects"
mastery_bar: 0.80
sections:
  quant:
    display_name: "Quantitative"
    item_count: 20
    explanation_policy: withheld
    blueprint_weight: 1.0
  verbal:
    display_name: "Verbal"
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

TWO_SECTION_TAGS_YAML = """
tags:
  - slug: fractions
    display_name: "Fractions"
    section: quant
  - slug: inference
    display_name: "Inference"
    section: verbal
edges: []
"""


@pytest.fixture
def two_subject(tmp_path, conn, pack):
    """The same database, re-read through a pack that declares two sections.

    Only `quant` has items -- `inference` is declared and empty, which is the
    ordinary state of a real pack and the case a subject picker has to handle
    without offering a dead click.
    """
    (tmp_path / "product.yaml").write_text(TWO_SECTION_PRODUCT_YAML)
    (tmp_path / "taxonomy" / "tags.yaml").write_text(TWO_SECTION_TAGS_YAML)
    product = config.load_product(tmp_path)
    db.upsert_taxonomy(conn, product["product_id"], product["_tags"], product["_edges"])
    return session.DrillSession(conn, product, "learner", store=session.DrillStore())


def test_a_subject_filter_restricts_the_recommendation(two_subject):
    everything = two_subject.recommend()
    focused = two_subject.recommend(section="quant")
    assert isinstance(focused, session.Recommendation)
    assert focused.section == "quant"
    assert focused.tag_slug == everything.tag_slug


def test_a_subject_with_no_items_says_so_and_points_elsewhere(two_subject):
    """Not "nothing to study" -- that would be false and discouraging."""
    result = two_subject.recommend(section="verbal")
    assert isinstance(result, session.Starved)
    assert result.section == "verbal"
    assert "quant" in result.reason


def test_servable_counts_are_reported_per_subject(two_subject):
    counts = two_subject.servable_by_section()
    assert counts.get("quant", 0) >= 1
    assert "verbal" not in counts


def test_an_unknown_subject_is_rejected_rather_than_silently_ignored(two_subject):
    """Silently widening the scope would study the wrong thing without saying."""
    with pytest.raises(ValueError, match="unknown section"):
        two_subject.recommend(section="astrology")


def test_starting_honours_the_subject(two_subject):
    served = two_subject.start(section="quant")
    assert isinstance(served, session.Served)
    assert served.section_slug == "quant"


def test_a_subject_filter_cannot_dodge_a_satisfied_objective(tmp_path, conn, pack):
    """Principle 9 outranks the filter.

    Otherwise "study only maths" becomes a way to keep a finished tool
    serving items, which is the engagement product this design refuses to be.
    """
    (tmp_path / "objective.yaml").write_text(SATISFIED_OBJECTIVE_YAML)
    met = config.load_product(tmp_path)
    db.upsert_product(conn, met, "digest")
    db.set_manual_value(conn, "learner", met["product_id"], "self_score", 9.0)

    drill = session.DrillSession(conn, met, "learner", store=session.DrillStore())
    assert isinstance(drill.recommend(section="quant"), session.Satisfied)
    assert isinstance(drill.start(section="quant"), session.Satisfied)


def test_position_is_read_only(drill, conn):
    """`study report` snapshots to objective_state; a page render must not.

    Otherwise the position trajectory measures how often a tab was opened.
    """
    before = conn.execute("SELECT COUNT(*) FROM objective_state").fetchone()[0]
    report = drill.position()
    after = conn.execute("SELECT COUNT(*) FROM objective_state").fetchone()[0]
    assert report.routes
    assert after == before


# ------------------------------------------- declining an item, unattempted


def test_skipping_records_the_item_without_an_attempt(drill, conn):
    """An item declined is not an item failed. No answer exists, so there is no
    evidence about the learner or the item, and neither rating may move."""
    served = drill.start()
    before = conn.execute(
        "SELECT rating, n_attempts FROM items WHERE item_id = ?", (served.item_id,)
    ).fetchone()

    drill.skip(served.token)

    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM learner_state").fetchone()[0] == 0
    after = conn.execute(
        "SELECT rating, n_attempts FROM items WHERE item_id = ?", (served.item_id,)
    ).fetchone()
    assert after["rating"] == before["rating"]
    assert after["n_attempts"] == before["n_attempts"]

    row = conn.execute("SELECT * FROM skips").fetchone()
    assert row["item_id"] == served.item_id
    assert row["tag_slug"] == served.tag_slug
    assert row["item_rating"] == before["rating"], "the difficulty that was refused"


def test_a_skip_records_the_hints_taken_first(drill, conn):
    """"Too hard on sight" and "too hard after four hints" are different
    findings -- the first is a band error, the second is a hard item."""
    served = drill.start()
    drill.request_hint(served.token, 3)
    drill.skip(served.token)

    assert conn.execute("SELECT rungs_seen FROM skips").fetchone()[0] == 3


def test_a_skip_after_the_capture_is_still_a_skip(drill, conn):
    """Nothing is in the log until an answer is graded, so a locked capture
    does not make declining into an attempt."""
    served = drill.start()
    drill.submit_capture(served.token, good_capture())
    drill.skip(served.token)

    assert conn.execute("SELECT COUNT(*) FROM skips").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 0


def test_an_answered_item_cannot_be_skipped(drill, conn):
    """The one that matters. After the verdict this would be an escape from the
    explain-back gate, which WEB_UI.md §4.3 does not have -- and it would let a
    learner erase a miss by relabelling it as never attempted."""
    served, _ = advance_to_gate(drill, answer="16/3")

    with pytest.raises(session.PhaseError):
        drill.skip(served.token)

    assert conn.execute("SELECT COUNT(*) FROM skips").fetchone()[0] == 0
    # And the drill is still live, so the gate is still the way out.
    assert drill.view(served.token).phase is session.Phase.ANSWERED


def test_a_skipped_item_is_not_served_again_immediately(drill, conn):
    """Without this the button does nothing: `n_attempts` is unchanged by a
    skip, so the item just declined is still the least-attempted one in band."""
    first = drill.start()
    drill.skip(first.token)

    served = [drill.start() for _ in range(5)]
    assert first.item_id not in {s.item_id for s in served}, "not while others remain"

    # But not deleted from the corpus either: when the band holds nothing else,
    # it comes back rather than starving the tag.
    for s in served:
        drill.skip(s.token)
    assert drill.start().item_id is not None


# ---------------------------------------------------- starvation and stop


def test_a_forced_tag_with_no_items_reports_starvation(drill):
    result = drill.start(tag="no-such-tag")
    assert isinstance(result, session.Starved)


def test_capture_fields_come_from_the_product_not_the_kernel(drill):
    served = drill.start()
    assert served.capture_fields == ["confidence", "rationale", "verification_method"]


def test_build_capture_coerces_front_end_strings(drill):
    served = drill.start()
    cap = session.build_capture(
        {
            "confidence": " 3 ",
            "rationale": "  eight plus nine carries once  ",
            "verification_method": ["unit_check", "back_substitution"],
        },
        served.capture_fields,
    )
    assert cap.confidence == 3
    assert cap.rationale == "eight plus nine carries once"


def test_build_capture_leaves_a_non_numeric_confidence_unset(drill):
    served = drill.start()
    cap = session.build_capture({"confidence": "three", "rationale": "x" * 20}, served.capture_fields)
    assert cap.confidence is None
    with pytest.raises(capture_mod.CaptureError):
        capture_mod.validate(cap, served.capture_fields)


def test_ticked_boxes_arrive_as_a_list_and_store_in_a_canonical_order(drill):
    """Two learners who ticked the same boxes must produce the same string.

    Otherwise the field is countable in principle and ungroupable in practice,
    which is the free-text failure with extra steps.
    """
    served = drill.start()
    one = session.build_capture(
        {"verification_method": ["unit_check", "back_substitution"]},
        served.capture_fields,
    )
    two = session.build_capture(
        {"verification_method": ["back_substitution", "unit_check"]},
        served.capture_fields,
    )
    assert one.verification_method == two.verification_method == "back_substitution,unit_check"


def test_a_verification_method_off_the_list_is_rejected(drill):
    served = drill.start()
    cap = good_capture()
    cap.verification_method = "vibes"
    with pytest.raises(capture_mod.CaptureError, match="vibes"):
        capture_mod.validate(cap, served.capture_fields)


def test_not_checking_cannot_be_claimed_alongside_checking(drill):
    served = drill.start()
    cap = good_capture()
    cap.verification_method = "none,unit_check"
    with pytest.raises(capture_mod.CaptureError):
        capture_mod.validate(cap, served.capture_fields)


def test_not_checking_is_a_valid_answer_on_its_own(drill):
    """The point of the field. If admitting it is harder than lying, it lies."""
    served = drill.start()
    cap = good_capture()
    cap.verification_method = capture_mod.NO_CHECK
    capture_mod.validate(cap, served.capture_fields)
    verdict = drill.submit_capture(served.token, cap)
    assert not isinstance(verdict, session.Starved)


def test_an_untouched_checkbox_group_still_rejects_the_capture(drill):
    """No box ticked posts no key at all -- it must not read as 'didn't check'."""
    served = drill.start()
    cap = session.build_capture(
        {"confidence": "2", "rationale": "x" * 20}, served.capture_fields
    )
    assert cap.verification_method is None
    with pytest.raises(capture_mod.CaptureError, match="didn't"):
        capture_mod.validate(cap, served.capture_fields)


def test_every_verification_method_carries_an_explanation():
    """The detail text is load-bearing: unread options get ticked at random."""
    for method in capture_mod.VERIFICATION_METHODS:
        assert method.label and len(method.detail) > 30


def test_the_passage_rides_along_with_the_item(drill):
    served = drill.start()
    assert served.passage == "A passage the item hangs off."


def test_gate_result_comes_from_the_pedagogy_module(drill):
    """The service does not reimplement the check, it calls it."""
    served, _ = advance_to_gate(drill)
    gate = drill.submit_explain_back(served.token, "no")
    assert isinstance(gate, explain_back.GateResult)
