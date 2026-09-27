"""TypeSafe System One, the opt-in fallback screening provider (issue #26 review item 1).

A Choice carries a selected option and a confidence; a Noul carries only the probability of yes -- there
is no separate confidence, the probability *is* the answer. Getting that distinction wrong is how a
0.5 noul turns into a false "medium confidence yes", so it is pinned here.
"""

from __future__ import annotations

from typing import Any

import pytest

from opengrid.ai_agent.providers import ModelRequest, ProviderError, TokenUsage
from opengrid.ai_agent.systemone import (
    SCREEN_QUESTIONS,
    SystemOneClient,
    SystemOneUnavailable,
    TypeSafeProvider,
    parse_answers,
    usage_tokens,
)
from opengrid.ai_agent.types import Judgement

from .fakes import CONTEXT


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
    """No key is a normal state: the fallback then simply never runs."""
    assert TypeSafeProvider(SystemOneClient(api_key="")).available is False
    assert TypeSafeProvider(SystemOneClient(api_key="sk-test")).available is True


class _ScriptedClient(SystemOneClient):
    def __init__(self, answers: dict[str, Judgement] | None = None, *, fail: bool = False) -> None:
        super().__init__(api_key="fake-key")
        self._answers = answers or {}
        self._fail = fail
        self.states: list[Any] = []

    async def ask(
        self, state: Any, questions: dict[str, dict[str, Any]], *, timeout_s: float | None = None
    ) -> tuple[dict[str, Judgement], TokenUsage]:
        self.states.append(state)
        if self._fail:
            raise SystemOneUnavailable("stubbed outage")
        return self._answers, TokenUsage(input_tokens=550, output_tokens=78)


async def test_the_fallback_screens_over_the_same_redacted_request() -> None:
    client = _ScriptedClient(
        {
            "intent": Judgement(value="deterministic_query", confidence=0.9),
            "needs_trace": Judgement(value=0.1),
            "injection_risk": Judgement(value=0.05),
        }
    )
    request = ModelRequest.build("anything at risk? my number is (512) 555-0147", CONTEXT)

    verdict, usage = await TypeSafeProvider(client).screen(request, timeout_s=5.0)

    assert client.states[0] == {"evidence": request.evidence, "operator_question": request.question}
    assert "555" not in client.states[0]["operator_question"]
    assert verdict.provider == "typesafe" and verdict.model == "jev-latest"
    assert verdict.injection_risk == 0.05
    assert usage.input_tokens == 550


async def test_a_missing_injection_answer_is_a_failure_not_a_zero() -> None:
    client = _ScriptedClient({"intent": Judgement(value="deterministic_query", confidence=0.9)})

    with pytest.raises(ProviderError):
        await TypeSafeProvider(client).screen(ModelRequest.build("hi", CONTEXT), timeout_s=5.0)


async def test_typesafe_never_writes_explanations() -> None:
    provider = TypeSafeProvider(_ScriptedClient())

    assert provider.model_for("explain") is None
    with pytest.raises(ProviderError):
        await provider.explain(ModelRequest.build("why?", CONTEXT), timeout_s=5.0)


def test_the_screening_questions_share_the_intent_vocabulary() -> None:
    assert set(SCREEN_QUESTIONS["intent"]["criteria"]) == {
        "deterministic_query",
        "fleet_query",
        "explain_decision",
        "draft_action",
        "out_of_scope",
    }
