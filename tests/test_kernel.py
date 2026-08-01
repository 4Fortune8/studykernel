"""Tests for the load-bearing kernel behaviors.

Each test here corresponds to a claim DESIGN.md makes. The point is not
coverage; it is that the specific properties the design argues for are the
ones actually implemented.
"""

from __future__ import annotations

import pytest

from kernel import allocator, config
from kernel.objectives import base as objective_base
from kernel.objectives import routes
from kernel.objectives.threshold import ThresholdObjective
from kernel.pedagogy import dag, errors, grading, hints
from kernel.exchange import briefing, record
from kernel.state import glicko2, reliability, variables
from kernel.state.view import Estimate, StateView, TagState


def make_state(**overrides) -> StateView:
    tags = {
        "alg-l1": TagState("alg-l1", "math", 1400, 80, n_attempts=10, successes=9,
                           reliability_lo=0.62, reliability_point=0.9, level=1,
                           items_in_band=20),
        "alg-l2": TagState("alg-l2", "math", 1300, 120, n_attempts=4, successes=2,
                           reliability_lo=0.15, reliability_point=0.5, level=2,
                           parent_slug="alg-l1", items_in_band=20),
    }
    base = dict(learner_id="me", product_id="p", tags=tags, mastery_bar=0.8)
    base.update(overrides)
    return StateView(**base)


# ------------------------------------------------------------------ ratings


def test_correct_answer_raises_learner_and_lowers_item():
    """Shared scale: the item's difficulty calibrates from attempt data."""
    learner = glicko2.Rating(1500, 200)
    item = glicko2.Rating(1500, 200)
    new_learner, new_item = glicko2.apply_attempt(learner, item, correct=True)

    assert new_learner.rating > learner.rating
    assert new_item.rating < item.rating


def test_rating_deviation_shrinks_with_evidence():
    r = glicko2.Rating(1500, 350)
    item = glicko2.Rating(1500, 50)
    for _ in range(10):
        r = glicko2.update(r, item, 1.0)
    assert r.rd < 350


def test_inactivity_widens_uncertainty():
    r = glicko2.Rating(1500, 60)
    assert glicko2.decay(r, periods=20).rd > r.rd


# -------------------------------------------------------------- reliability


def test_two_in_a_row_does_not_clear_the_mastery_bar():
    """DESIGN.md §6.2: two-in-a-row means nothing, structurally."""
    assert reliability.wilson(2, 2).point == 1.0
    assert not reliability.meets_bar(2, 2, bar=0.8)


def test_a_long_clean_run_does_clear_it():
    assert reliability.meets_bar(30, 30, bar=0.8)


def test_lower_bound_is_below_point_estimate():
    interval = reliability.wilson(8, 10)
    assert interval.lo < interval.point < interval.hi


# ------------------------------------------------------------------ routes


def test_and_or_keywords_parse():
    assert routes.variables_used("a >= 1 AND b >= 2") == {"a", "b"}
    assert routes.variables_used("a >= 1 OR b >= 2") == {"a", "b"}


def test_route_probability_reflects_uncertainty():
    state = make_state(variables={"score": Estimate(950, 20)})
    # Exactly at the cut with symmetric uncertainty -> a coin flip.
    assert routes.probability("score >= 950", state) == pytest.approx(0.5, abs=0.01)
    assert routes.probability("score >= 900", state) > 0.99


def test_routes_reject_arbitrary_code():
    state = make_state(variables={"score": Estimate(950, 20)})
    with pytest.raises(routes.RouteError):
        routes.probability("__import__('os').system('echo pwned')", state)


def test_routes_reject_unknown_function():
    state = make_state(variables={"score": Estimate(950, 20)})
    with pytest.raises(routes.RouteError):
        routes.probability("some_function(score >= 950)", state)


def test_unknown_variable_is_an_error_not_a_zero():
    state = make_state(variables={"score": Estimate(950, 20)})
    with pytest.raises(routes.RouteError):
        routes.probability("typo_estimate >= 950", state)


# --------------------------------------------------------------- threshold


def make_threshold(**cfg) -> ThresholdObjective:
    base = {
        "type": "threshold",
        "margin_policy": "rating_deviation",
        "routes": {"main": ["score >= 950"]},
    }
    base.update(cfg)
    return ThresholdObjective(base)


def test_satisfied_requires_a_margin_not_just_the_cut():
    """DESIGN.md §7.1: aiming at exactly the line is a coin flip."""
    objective = make_threshold()
    at_the_line = make_state(variables={"score": Estimate(950, 30)})
    assert not objective.satisfied(at_the_line)

    clear = make_state(variables={"score": Estimate(1000, 30)})
    assert objective.satisfied(clear)


def test_a_satisfied_objective_reads_zero_gradient():
    """DESIGN.md principle 9 -- the tool must be able to say stop."""
    objective = make_threshold()
    state = make_state(
        variables={"score": Estimate(1000, 5)},
        var_declarations={"score": {"kind": "manual"}},
        manual_values={"score": 1000},
    )
    assert objective.satisfied(state)
    assert objective.gradient(state, "alg-l1") == 0.0


def test_gradient_is_positive_below_the_cut():
    objective = make_threshold()
    state = variables.recompute(
        make_state(
            var_declarations={
                "score": {
                    "kind": "scaled_ability",
                    "section": "math",
                    "rating_range": [1200, 1750],
                    "scale_range": [910, 990],
                }
            }
        )
    )
    assert objective.gradient(state, "alg-l1") > 0


def test_a_zeroed_group_does_not_kill_gradients_elsewhere():
    """Regression: a route group at exactly zero once zeroed every gradient.

    `progress` multiplies across groups, so an ELAR route gated on an unset
    essay score made the product zero and every numeric partial derivative
    with it -- the tool reported "nothing to study" because of one missing
    data point. The gradient is per-route for exactly this reason.
    """
    objective = ThresholdObjective(
        {
            "type": "threshold",
            "routes": {
                "math": ["score >= 950"],
                # Unsatisfiable and insensitive to any amount of study.
                "gate": ["essay >= 5"],
            },
            "variables": {},
        }
    )
    state = variables.recompute(
        make_state(
            var_declarations={
                "score": {
                    "kind": "scaled_ability",
                    "section": "math",
                    "rating_range": [1200, 1750],
                    "scale_range": [910, 990],
                },
                "essay": {"kind": "manual", "default": 0},
            }
        )
    )
    assert objective.progress(state) == 0.0
    assert objective.gradient(state, "alg-l1") > 0
    assert objective.unmeasured_variables(state) == ["essay"]


def test_combined_uncertainty_does_not_collapse_with_many_tags():
    """Regression: averaging many untouched tags implied false certainty.

    Under an independence assumption, 28 tags at RD 350 combine to a standard
    error of ~66 -- near-certainty about a learner who has answered nothing.
    Tag abilities share a common cause and must not average out that way.
    """
    many = {
        f"t{i}": TagState(f"t{i}", "math", 1500, 350) for i in range(28)
    }
    _mean, sd = variables._weighted_rating(list(many.values()))
    assert sd > 250, "combined RD collapsed -- correlation term missing"


def test_target_band_widens_when_position_is_unknown():
    """A fresh tag's band must be wide, or availability reports false starvation."""
    fresh = TagState("t", "math", 1500, glicko2.DEFAULT_RD)
    settled = TagState("t", "math", 1500, 50)
    fresh_lo, fresh_hi = allocator.target_band(fresh)
    settled_lo, settled_hi = allocator.target_band(settled)
    assert (fresh_hi - fresh_lo) > (settled_hi - settled_lo) * 3


def test_multiple_routes_report_separately():
    objective = make_threshold(
        routes={"main": ["score >= 950", "score >= 930"]}
    )
    state = make_state(variables={"score": Estimate(935, 10)})
    report = objective.report(state)
    assert len(report.routes) == 2
    # Steepest first: the easier route should lead.
    assert report.routes[0].expression == "score >= 930"


# --------------------------------------------------------------- allocator


def test_allocator_skips_tags_with_no_items_and_flags_them():
    """Availability is honest: a starved tag surfaces, never silently skips."""
    tags = {
        "served": TagState("served", "math", 1400, 80, reliability_lo=0.3, items_in_band=10),
        "starved": TagState("starved", "math", 1400, 80, reliability_lo=0.3, items_in_band=0),
    }
    state = variables.recompute(
        StateView(
            learner_id="me",
            product_id="p",
            tags=tags,
            var_declarations={
                "score": {
                    "kind": "scaled_ability",
                    "section": "math",
                    "rating_range": [1200, 1750],
                    "scale_range": [910, 990],
                }
            },
        )
    )
    objective = make_threshold()
    ranked = allocator.rank(state, objective)
    by_slug = {a.tag_slug: a for a in ranked}

    assert by_slug["starved"].priority == 0.0
    assert by_slug["served"].priority > 0.0
    assert allocator.starved_tags(ranked)


def test_learnability_peaks_in_the_teachable_band():
    tag = TagState("t", "math", 1400, 80, items_in_band=10)
    peak, reason = allocator.learnability(tag)
    assert peak == 1.0 and reason is None


# --------------------------------------------------------------------- DAG


def test_unreviewed_model_labels_cannot_reinforce_the_dag():
    """DESIGN.md principle 7. They may still serve practice."""
    with pytest.raises(dag.ProvenanceError):
        dag.reinforce(None, "a", "b", label_source="model", reviewed=False)


def test_reviewed_model_labels_may_reinforce():
    edge = dag.reinforce(None, "a", "b", label_source="model", reviewed=True)
    assert edge.source == "learned" and edge.confidence > 0


def test_learned_edges_never_reach_seed_confidence():
    edge = None
    for _ in range(50):
        edge = dag.reinforce(edge, "a", "b", "human", True)
    assert edge.confidence <= dag.LEARNED_CEILING < 1.0


def test_seed_edges_outrank_inference():
    seed = dag.Edge("a", "b", 1.0, "seed", 0)
    assert dag.reinforce(seed, "a", "b", "human", True) is seed


def test_cycles_are_detected():
    edges = [dag.Edge("a", "b", 0.5, "learned"), dag.Edge("b", "a", 0.5, "learned")]
    assert dag.has_cycle(edges)
    assert dag.has_cycle([dag.Edge("a", "b", 1.0, "seed")]) is None


def test_routing_triggers_on_repeated_deep_failures():
    attempts = [{"correct": False, "min_hint_level": 4}] * 3
    assert dag.should_route_to_prerequisites(dag.count_deep_failures(attempts))
    shallow = [{"correct": False, "min_hint_level": 0}] * 5
    assert not dag.should_route_to_prerequisites(dag.count_deep_failures(shallow))


# ---------------------------------------------------------------- grading


@pytest.mark.parametrize(
    "given,key,expected",
    [
        ("1200", "1,200", True),
        ("0.5", "\\frac{1}{2}", True),
        ("$45$", "45", True),
        ("20/3", "\\dfrac{20}{3}", True),
        ("46", "45", False),
        ("x = 2", "2", False),
    ],
)
def test_grading_is_exact_after_normalization(given, key, expected):
    assert grading.grade(given, key) is expected


def test_an_item_without_a_key_cannot_be_graded():
    with pytest.raises(ValueError):
        grading.grade("5", None)
    assert not grading.is_gradable("")


def test_marking_the_options_follows_the_pack_the_key_uses():
    """Letter-keyed and text-keyed packs both have to mark the same option.

    `choice_values` already decides which form this item grades in; marking
    reuses it rather than comparing the raw strings, because a template doing
    its own matching would disagree with the verdict printed beside it.
    """
    choices = ["alpha", "beta", "gamma", "delta"]

    by_letter = grading.choice_values(choices, "C")
    assert grading.mark_choices(by_letter, "C", "A") == [
        grading.GIVEN, None, grading.KEY, None
    ]

    by_text = grading.choice_values(choices, "gamma")
    assert grading.mark_choices(by_text, "gamma", "alpha") == [
        grading.GIVEN, None, grading.KEY, None
    ]


def test_a_correct_answer_marks_one_option_both_not_two_options_once():
    assert grading.mark_choices(["A", "B"], "b", "B") == [None, grading.BOTH]


def test_marking_survives_an_answer_that_matches_no_option():
    """A free-text answer on a selectable item still has to render a key.

    Returning early or raising would blank the whole list, which is the case
    it exists to serve.
    """
    assert grading.mark_choices(["A", "B"], "A", "banana") == [grading.KEY, None]
    assert grading.mark_choices(None, "A", "A") == []


# ---------------------------------------------------------------- exchange


def test_item_id_mismatch_is_rejected():
    """DESIGN.md §10: reject on mismatch. Nothing recorded."""
    payload = '```json\n{"item_id": "wrong", "error_code": "misread", "one_fix": "read the qualifier"}\n```'
    with pytest.raises(record.RecordError, match="mismatch"):
        record.parse(payload, expected_item_id="right")


def test_valid_payload_parses():
    payload = (
        '```json\n{"item_id": "abc", "error_code": "execution_error", '
        '"one_fix": "check the sign when distributing", "trigger_miss": false}\n```'
    )
    diagnosis = record.parse(payload, "abc")
    assert diagnosis.error_code == "execution_error"
    assert diagnosis.explain_back_ok is None  # not judged is not rejected


def _briefing_item(**overrides):
    fields = dict(
        item_id="abc",
        stem="Solve for x.",
        choices=None,
        answer_key="-4",
        official_expl=None,
        passage=None,
        section_slug="math",
        explanation_policy="withheld",
        tags=["signed-arithmetic"],
    )
    fields.update(overrides)
    return briefing.BriefingItem(**fields)


def _briefing_capture(**overrides):
    fields = dict(
        confidence=2,
        rationale="Subtracted the smaller from the larger.",
        verification_method=None,
        answer_given="4",
        correct=False,
        min_hint_level=0,
    )
    fields.update(overrides)
    return briefing.BriefingCapture(**fields)


def test_the_divergence_survives_the_round_trip():
    """The briefing already asked for it; before v0.2 there was nowhere to put it.

    `one_fix` is the habit to change and generalises past this item. The
    divergence is the line the learner actually wrote and got wrong, and it is
    the thing they came back to find out -- so it needs its own field rather
    than being folded into the fix or discarded.
    """
    payload = (
        '{"item_id": "abc", "error_code": "execution_error", '
        '"divergence": "You wrote 17 - 21 = 4; it is -4.", '
        '"one_fix": "keep track of negative signs"}'
    )
    diagnosis = record.parse(payload, "abc")
    assert diagnosis.divergence == "You wrote 17 - 21 = 4; it is -4."


def test_the_worked_explanation_survives_the_round_trip():
    payload = (
        '{"item_id": "abc", "error_code": "knowledge_gap", '
        '"one_fix": "distinguish domain from range", '
        '"explanation": "Multiplying the outside by 3 triples the range to [-3, 3]; '
        'the domain is unchanged because any real number is still a valid input."}'
    )
    diagnosis = record.parse(payload, "abc")
    assert diagnosis.explanation.startswith("Multiplying the outside by 3")


def test_the_explanation_is_asked_for_as_a_bridge_not_a_worked_solution():
    """It has to replace the "explain this" follow-up, in one pass.

    A detached worked solution does not: it answers the item while leaving the
    learner to map it onto what they actually thought, which is the step they
    were sending a second prompt to get. So the field is specified as the flaw
    in their reasoning plus the route from it to the key.
    """
    text = briefing.render(_briefing_item(), _briefing_capture())
    assert "built on the reasoning they actually gave" in text
    assert "What is flawed in their thinking" in text
    assert "bridge from what they did to what they should have done" in text
    assert "nothing is left for a follow-up question" in text


def test_the_three_diagnosis_fields_are_told_apart_in_the_prompt():
    """`divergence`, `one_fix` and `explanation` must not restate each other.

    They are adjacent by design -- where it broke, what habit to change, how
    the item is done -- and adjacent fields are how a return payload turns into
    the same paragraph three times.
    """
    text = briefing.render(_briefing_item(), _briefing_capture())
    assert "write each of the three once, in its own field" in text
    assert "Where 3 above says what habit to change, this says how the item" in text


def test_the_worked_explanation_is_bounded_by_the_explanation_policy():
    """The new field must not become a hole in the ELAR guardrail.

    DATA_SOURCING_ELAR.md §2.4: on reading comprehension, fluent-but-false
    justification teaches false reasoning, so `anchored` and `pinned_strict`
    forbid derived reasoning. An instruction to "explain step by step" that did
    not say so would license exactly that, and would read as the later, more
    specific instruction while doing it.
    """
    strict = briefing.render(
        _briefing_item(explanation_policy="pinned_strict", official_expl="Because."),
        _briefing_capture(),
    )
    assert "bounded by the EXPLANATION POLICY" in strict
    assert "do not derive your own" in strict
    assert "Under anchored, every step is a verbatim quote" in strict


def test_a_payload_without_a_divergence_still_records():
    """A reply from a pre-v0.2 prompt is stale, not invalid.

    Rejecting it would make a prompt revision retroactively invalidate replies
    already sitting in the learner's chat window.
    """
    payload = '{"item_id": "abc", "error_code": "misread", "one_fix": "read the qualifier"}'
    assert record.parse(payload, "abc").divergence is None


def test_the_briefing_names_the_no_work_sentence_it_expects_back():
    """The "show your steps" instruction has to reach the learner verbatim.

    It is the one output that tells them what to do differently *before* the
    next attempt, so the reader is given the exact sentence rather than asked
    to improvise one -- and the prompt and the display must not drift apart.
    """
    text = briefing.render(_briefing_item(), _briefing_capture())
    assert briefing.NO_WORK_SHOWN in text
    assert '"divergence"' in text


def test_a_stuck_briefing_asks_for_a_lesson_not_a_correction():
    """With no path of the learner's to correct, there is nothing to diverge from.

    Left unsaid, a reader would mine the blind rationale -- which on a stuck
    attempt was a guess -- and diagnose reasoning the learner never used.
    """
    text = briefing.render(_briefing_item(), _briefing_capture(stuck=True))
    assert "did not know where to start" in text
    assert "Teach this item from the beginning" in text


def test_a_list_of_fixes_is_rejected():
    """One fix, not five -- singular by prompt contract."""
    payload = (
        '{"item_id": "abc", "error_code": "misread", '
        '"one_fix": "1. slow down\\n2. re-read\\n3. check units"}'
    )
    with pytest.raises(record.RecordError, match="singular"):
        record.parse(payload, "abc")


def test_unknown_error_code_is_rejected():
    payload = '{"item_id": "abc", "error_code": "vibes", "one_fix": "try harder"}'
    with pytest.raises(ValueError):
        record.parse(payload, "abc")


def test_error_weights_are_product_data_not_kernel_constants():
    assert errors.weight("execution_error", {"execution_error": 2.0}) == 2.0
    assert errors.weight("execution_error", {"execution_error": 0.8}) == 0.8


# ------------------------------------------------------------------- hints


def test_hint_level_is_continuous_where_correctness_is_binary():
    assert hints.competence_score(0, True) > hints.competence_score(3, True)
    assert hints.competence_score(0, False) == 0.0
    assert not hints.solved_independently(5)


# -------------------------------------------------------------- pack loader


def test_real_product_packs_load_and_validate(tmp_path):
    from pathlib import Path

    for product in ("products/tsi-ready", "products/gre-forge"):
        loaded = config.load_product(Path(product))
        objective = objective_base.build(loaded["objective"])
        assert objective.type_name in {"threshold", "maximize", "deadline"}


def test_a_route_naming_an_undeclared_variable_fails_at_load(tmp_path):
    (tmp_path / "product.yaml").write_text(
        "product_id: t\nsections:\n  math:\n    explanation_policy: withheld\n"
    )
    (tmp_path / "objective.yaml").write_text(
        "objective:\n  type: threshold\n  variables:\n    a:\n      kind: manual\n"
        "  routes:\n    main:\n      - \"typo >= 5\"\n"
    )
    with pytest.raises(config.PackError, match="undeclared variable"):
        config.load_product(tmp_path)


def test_an_unknown_explanation_policy_fails_at_load(tmp_path):
    (tmp_path / "product.yaml").write_text(
        "product_id: t\nsections:\n  math:\n    explanation_policy: vibes\n"
    )
    (tmp_path / "objective.yaml").write_text(
        "objective:\n  type: threshold\n  variables:\n    a:\n      kind: manual\n"
        "  routes:\n    main:\n      - \"a >= 5\"\n"
    )
    with pytest.raises(config.PackError, match="explanation_policy"):
        config.load_product(tmp_path)
