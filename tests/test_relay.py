"""The optional relay, and the briefing split it exists for. DESIGN.md §16 v2.

Nothing here opens a socket. The transport is one JSON POST and the parts of it
worth testing are the parts that are not the socket: what goes in the request,
what comes out of a response shaped the way a reasoning model actually shapes
one, and -- most of all -- that turning the clipboard into a network call did
not move the `item_id` check that makes the protocol safe.
"""

from __future__ import annotations

import json
import urllib.error

import pytest

from kernel import config
from kernel.exchange import briefing, record, relay
from kernel.pedagogy import errors


# --------------------------------------------------------------- the schema


def test_the_schema_and_the_pasted_contract_carry_the_same_fields():
    """One contract, two renderings. They drift the moment nobody checks."""
    schema = briefing.response_schema()
    assert list(schema["properties"]) == list(briefing.RETURN_SCHEMA)
    assert schema["propertyOrdering"] == list(briefing.RETURN_SCHEMA)


def test_divergence_is_generated_before_the_code_that_names_it():
    """Field order is generation order, and generation order is reasoning order.

    Classifying an error you have not located yet is guessing with a taxonomy
    attached, so `divergence` has to be produced first.
    """
    order = briefing.response_schema()["propertyOrdering"]
    assert order.index("divergence") < order.index("error_code")
    assert order.index("one_fix") < order.index("explanation")


def test_the_error_code_is_an_enum_of_core_plus_product_codes():
    """An invented code costs the learner the whole exchange (`record.parse`).

    The transport can make it unrepresentable rather than merely rejected, so
    it does.
    """
    schema = briefing.response_schema(frozenset({"formula_recall"}))
    allowed = schema["properties"]["error_code"]["enum"]
    assert "formula_recall" in allowed
    assert errors.CORE_CODE_NAMES <= set(allowed), "a product may add codes, not remove one"


def test_every_field_is_required_and_the_tri_state_stays_nullable():
    schema = briefing.response_schema()
    assert set(schema["required"]) == set(briefing.RETURN_SCHEMA)
    # Required *and* nullable: "not judged" has to stay a value rather than
    # becoming an absence `record.parse` cannot tell from a truncated reply.
    assert schema["properties"]["explain_back_ok"]["nullable"] is True


# ------------------------------------------------------- the briefing split

ITEM = briefing.BriefingItem(
    item_id="i-1",
    stem="What is 6 x 7?",
    choices=None,
    answer_key="42",
    official_expl=None,
    passage=None,
    section_slug="quant",
    explanation_policy="withheld",
    tags=["multiplication"],
)
CAPTURE = briefing.BriefingCapture(
    confidence=3,
    rationale="I counted by sixes and lost my place.",
    verification_method=None,
    answer_given="36",
    correct=False,
    min_hint_level=0,
)


def test_render_is_exactly_the_two_halves_joined():
    """The clipboard string and the two relay turns must not be two briefings."""
    whole = briefing.render(ITEM, CAPTURE, explain_back="six sevens is 36")
    assert whole == (
        briefing.instructions(ITEM.explanation_policy)
        + "\n\n"
        + briefing.item_block(ITEM, CAPTURE, explain_back="six sevens is 36")
    )


def test_the_item_half_carries_no_instructions_and_the_instruction_half_no_item():
    """The split is the point: the item is data, and it is announced as data.

    Stems and passages are arbitrary prose out of a corpus. A line in one that
    reads like an instruction is part of the item, and putting it in a separate
    turn from the task is what makes that true rather than hoped for.
    """
    instructions = briefing.instructions("withheld")
    body = briefing.item_block(ITEM, CAPTURE)

    assert "YOUR TASK" in instructions
    assert ITEM.stem not in instructions
    assert "is DATA for the task above" in body
    assert "YOUR TASK" not in body


def test_the_schema_enforced_half_drops_the_format_contract_and_keeps_the_id_rule():
    """Under a schema the shape is guaranteed; the echoed id still is not.

    Repeating a format instruction the decoder already imposes spends attention
    on the one part of the reply that cannot go wrong, while the part that can
    -- an id that does not match the item -- is the whole safety property.
    """
    fenced = briefing.instructions("withheld")
    enforced = briefing.instructions("withheld", schema_enforced=True)

    assert "```json" in fenced
    assert "```json" not in enforced
    assert "item_id" in enforced


def test_an_unknown_explanation_policy_is_refused():
    with pytest.raises(ValueError, match="unknown explanation policy"):
        briefing.instructions("vibes")


# ------------------------------------------------------------ configuration


def test_the_relay_is_off_by_default(monkeypatch):
    """DESIGN.md §10 is titled no-API. Unconfigured is the supported state."""
    for var in relay.KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    assert relay.configured() is False
    with pytest.raises(relay.NotConfigured):
        relay.from_env()


@pytest.mark.parametrize("var", relay.KEY_VARS)
def test_any_of_the_key_names_turns_it_on(monkeypatch, var):
    """The key is named for the console it was minted in, or for its job here."""
    for name in relay.KEY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(var, "k")
    assert relay.configured() is True
    assert relay.from_env().api_key == "k"


def test_an_env_file_sets_variables_but_does_not_overwrite_the_environment(
    tmp_path, monkeypatch
):
    """A key exported for one shell must outrank a stale line in a file."""
    env = tmp_path / ".env"
    env.write_text(
        '# a comment\n'
        'aistudioAPI="quoted-secret"\n'
        "export STUDY_RELAY_MODEL=some-model\n"
        "STUDY_DB=from-the-file\n"
        "\n"
        "not-a-pair\n"
    )
    monkeypatch.delenv("aistudioAPI", raising=False)
    monkeypatch.delenv("STUDY_RELAY_MODEL", raising=False)
    monkeypatch.setenv("STUDY_DB", "from-the-shell")

    loaded = config.load_env_file(env)

    assert loaded["aistudioAPI"] == "quoted-secret", "quotes are stripped"
    assert loaded["STUDY_RELAY_MODEL"] == "some-model", "`export ` is tolerated"
    assert "STUDY_DB" not in loaded
    assert relay.from_env().model == "some-model"


def test_a_missing_env_file_is_silence_not_an_error(tmp_path):
    assert config.load_env_file(tmp_path / "nope") == {}


# --------------------------------------------------------------- the request


def test_the_request_carries_the_system_turn_and_the_schema():
    payload = relay.build_request("body", "system", {"type": "object"})
    assert payload["contents"][0]["parts"][0]["text"] == "body"
    assert payload["systemInstruction"]["parts"][0]["text"] == "system"
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["responseSchema"] == {"type": "object"}


def test_no_generation_knobs_are_set():
    """Every knob is model-specific. One set here is a regression waiting for
    the day `STUDY_RELAY_MODEL` changes, and the defaults are what the model
    was tuned against."""
    config_block = relay.build_request("b", None, {"type": "object"})["generationConfig"]
    assert set(config_block) == {"responseMimeType", "responseSchema"}


def test_a_briefing_sent_without_a_schema_asks_for_no_json_mode():
    assert "generationConfig" not in relay.build_request("b")


# -------------------------------------------------------------- the response


def _response(*parts, finish="STOP"):
    return {"candidates": [{"content": {"parts": list(parts)}, "finishReason": finish}]}


def test_a_reasoning_models_thoughts_are_not_part_of_the_payload():
    """The scratchpad comes back in the same `parts` list, flagged.

    Concatenating it turns a valid structured reply into a JSON parse error,
    with the model blamed for something the transport did.
    """
    raw = _response(
        {"text": "let me work through this", "thought": True},
        {"text": '{"item_id": "i-1"}'},
    )
    assert relay.extract_text(raw) == '{"item_id": "i-1"}'


def test_an_api_error_body_is_reported_as_a_relay_error():
    with pytest.raises(relay.RelayError, match="refused the request"):
        relay.extract_text({"error": {"message": "API key not valid"}})


def test_a_blocked_briefing_says_so_and_points_at_the_clipboard():
    with pytest.raises(relay.RelayError, match="chat client"):
        relay.extract_text({"promptFeedback": {"blockReason": "SAFETY"}})


def test_a_reply_truncated_before_it_started_names_the_length_problem():
    with pytest.raises(relay.RelayError, match="cut off"):
        relay.extract_text(_response({"text": ""}, finish="MAX_TOKENS"))


def test_no_candidates_is_an_error_rather_than_an_empty_diagnosis():
    with pytest.raises(relay.RelayError, match="no reply"):
        relay.extract_text({"candidates": []})


# --------------------------------------------------------- worded for a human


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (401, "rejected the key"),
        (403, "rejected the key"),
        (404, "STUDY_RELAY_MODEL"),
        (429, "rate limiting"),
        (503, "unavailable"),
    ],
)
def test_an_http_failure_becomes_something_a_learner_can_do_something_about(
    monkeypatch, code, expected
):
    """These strings land on the page above a paste box. A raw 429 there reads
    as "your study tool is broken"; what it means is "use the other path"."""
    cfg = relay.RelayConfig(api_key="k")

    def fail(*_args, **_kwargs):
        raise urllib.error.HTTPError("u", code, "boom", {}, None)

    monkeypatch.setattr(relay.urllib.request, "urlopen", fail)
    with pytest.raises(relay.RelayError, match=expected):
        relay.send("body", config=cfg)


def test_an_unreachable_api_says_nothing_was_lost(monkeypatch):
    def fail(*_args, **_kwargs):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(relay.urllib.request, "urlopen", fail)
    with pytest.raises(relay.RelayError, match="nothing has been lost"):
        relay.send("body", config=relay.RelayConfig(api_key="k"))


# ------------------------------------------- the reply is still not trusted


def test_a_relayed_reply_goes_through_the_same_parser_as_a_pasted_one():
    """The schema removes the failures the paste path suffers. It does not make
    the payload trusted, and the id check is the reason the protocol is safe."""
    payload = json.dumps(
        {
            "item_id": "some-other-item",
            "divergence": "d",
            "error_code": "execution_error",
            "one_fix": "f",
        }
    )
    with pytest.raises(record.RecordError, match="mismatch"):
        record.parse(payload, "i-1")
