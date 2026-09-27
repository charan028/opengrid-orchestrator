"""TypeSafe System One: the opt-in FALLBACK screening provider. Owner: ui-a/ai.

Claude is the copilot's primary provider (`opengrid.ai_agent.claude`). This module is only ever called
when Claude is unavailable AND `TYPESAFE_API_KEY` is set AND `[ai_agent].fallback_enabled = true`
(default false); the gateway enforces that, and otherwise nothing here runs.

System One returns a *typed answer with a calibrated probability* rather than generated prose, so it can
stand in for screening (intent and injection risk). It does not write explanations, so as a fallback it
screens only and the answer comes from the no-model tier.

It receives exactly the same redacted `ModelRequest` Claude does, and the gateway applies the same
budgets and tracing to it: there is no separate path.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from opengrid.ai_agent.providers import ModelRequest, ProviderError, TokenUsage
from opengrid.ai_agent.router import (
    INJECTION_QUESTION,
    INTENT_CRITERIA,
    INTENT_QUESTION,
    NEEDS_TRACE_QUESTION,
    verdict_from,
)
from opengrid.ai_agent.types import Judgement, ProviderName, Purpose, RouterVerdict

logger = logging.getLogger(__name__)

API_URL = "https://api.typesafe.ai/v1/systemone"
API_KEY_ENV = "TYPESAFE_API_KEY"
DEFAULT_MODEL = "jev-latest"

#: The screening questions, asked in one batched call (they are independent judgements over one state).
SCREEN_QUESTIONS: dict[str, dict[str, Any]] = {
    "intent": {"type": "choice", "instructions": INTENT_QUESTION, "criteria": INTENT_CRITERIA},
    "needs_trace": {"type": "noul", "instructions": NEEDS_TRACE_QUESTION},
    "injection_risk": {"type": "noul", "instructions": INJECTION_QUESTION},
}


class SystemOneUnavailable(Exception):  # noqa: N818 -- mirrors opengrid.ui.api_client.ApiUnavailable
    """The judgement service could not answer."""


class SystemOneClient:
    """Thin async transport. Holds no state beyond its configuration so it is safe to share per process."""

    def __init__(
        self, *, model: str = DEFAULT_MODEL, timeout_s: float = 6.0, api_key: str | None = None
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._api_key = api_key if api_key is not None else os.environ.get(API_KEY_ENV, "")

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def model(self) -> str:
        return self._model

    async def ask(
        self, state: Any, questions: dict[str, dict[str, Any]], *, timeout_s: float | None = None
    ) -> tuple[dict[str, Judgement], TokenUsage]:
        """Ask every question in one call; return the typed answers by question id and the usage billed.

        `state` must already be redacted: this client does not inspect it, by design.
        """
        if not self._api_key:
            raise SystemOneUnavailable(f"{API_KEY_ENV} is not set")
        payload = {"state": state, "model": self._model, "questions": questions}
        timeout = min(self._timeout_s, timeout_s) if timeout_s is not None else self._timeout_s
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(
                    API_URL,
                    json=payload,
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
                response.raise_for_status()
                body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise SystemOneUnavailable(type(exc).__name__) from exc
        if not isinstance(body, dict):
            raise SystemOneUnavailable("unexpected response shape")
        return parse_answers(body), usage_of(body)


class TypeSafeProvider:
    """`ModelProvider` over System One: screening only."""

    def __init__(self, client: SystemOneClient) -> None:
        self._client = client

    @property
    def name(self) -> ProviderName:
        return "typesafe"

    @property
    def available(self) -> bool:
        return self._client.configured

    def model_for(self, purpose: Purpose) -> str | None:
        return self._client.model if purpose == "screen" else None

    async def screen(self, request: ModelRequest, *, timeout_s: float) -> tuple[RouterVerdict, TokenUsage]:
        # Screening classifies the operator's words only: never the console snapshot (same as Claude).
        state = {"operator_question": request.question}
        try:
            answers, usage = await self._client.ask(state, SCREEN_QUESTIONS, timeout_s=timeout_s)
        except SystemOneUnavailable as exc:
            raise ProviderError(f"typesafe: {exc}") from exc
        intent = answers.get("intent")
        needs_trace = answers.get("needs_trace")
        injection = answers.get("injection_risk")
        if intent is None or injection is None:
            raise ProviderError("typesafe: screening answer incomplete", usage=usage)
        verdict = verdict_from(
            intent=intent.value,
            intent_confidence=intent.confidence,
            needs_trace=needs_trace.value if needs_trace else None,
            injection_risk=injection.value,
            provider=self.name,
            model=self._client.model,
        )
        return verdict, usage

    async def explain(self, request: ModelRequest, *, timeout_s: float) -> tuple[str, TokenUsage]:
        raise ProviderError("typesafe does not write explanations")


def parse_answers(body: dict[str, Any]) -> dict[str, Judgement]:
    """Map the API's per-type answer shapes onto one `Judgement`.

    A Choice carries the selected option and a probability per option; a Noul carries only the
    probability of yes (there is no separate confidence -- the probability *is* the answer); a Score
    carries the level. Flattening them here keeps the branching in one tested place.
    """
    answers: dict[str, Judgement] = {}
    for key, raw in (body.get("answers") or {}).items():
        if not isinstance(raw, dict):
            continue
        kind = raw.get("type")
        if kind == "choice":
            answers[key] = Judgement(
                value=raw.get("choice"),
                confidence=raw.get("confidence"),
                probabilities=raw.get("probabilities") or {},
            )
        elif kind == "noul":
            answers[key] = Judgement(value=raw.get("noul"), confidence=None)
        elif kind == "score":
            answers[key] = Judgement(
                value=raw.get("score"),
                confidence=raw.get("confidence"),
                probabilities=raw.get("probabilities") or {},
            )
        else:
            answers[key] = Judgement(value=raw.get("value"))
    return answers


def usage_of(body: dict[str, Any]) -> TokenUsage:
    usage = body.get("usage") or {}
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
    )


def usage_tokens(body: dict[str, Any]) -> int:
    usage = usage_of(body)
    return usage.input_tokens + usage.output_tokens
