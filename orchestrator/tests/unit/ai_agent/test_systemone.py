"""Flattening System One's per-type answers into one Judgement (issue #26).

A Choice carries a selected option and a confidence; a Noul carries only the probability of yes -- there
is no separate confidence, the probability *is* the answer. Getting that distinction wrong is how a
0.5 noul turns into a false "medium confidence yes", so it is pinned here.
"""

from __future__ import annotations

from opengrid.ai_agent.systemone import SystemOneClient, parse_answers, usage_tokens


def test_a_choice_keeps_its_option_confidence_and_distribution() -> None:
    answers = parse_answers(
        {
            "answers": {
                "intent": {
                    "type": "choice",
                    "choice": "explain_decision",
                    "confidence": 0.97,
                    "probabilities": {"explain_decision": 0.98, "draft_action": 0.0},
                }
            }
        }
    )

    assert answers["intent"].value == "explain_decision"
    assert answers["intent"].confidence == 0.97
    assert answers["intent"].probabilities["explain_decision"] == 0.98


def test_a_noul_is_a_probability_with_no_separate_confidence() -> None:
    answers = parse_answers({"answers": {"needs_trace": {"type": "noul", "noul": 0.81}}})

    assert answers["needs_trace"].value == 0.81
    assert answers["needs_trace"].confidence is None


def test_usage_tokens_sums_both_directions() -> None:
    assert usage_tokens({"usage": {"input_tokens": 550, "output_tokens": 78}}) == 628
    assert usage_tokens({}) == 0


def test_a_client_without_a_key_reports_itself_unconfigured() -> None:
    """No key is a normal state, not a crash: the console falls back to the deterministic tier."""
    assert SystemOneClient(api_key="").configured is False
    assert SystemOneClient(api_key="sk-test").configured is True
