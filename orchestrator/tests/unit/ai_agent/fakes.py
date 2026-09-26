"""Fakes shared by the copilot tests. Nothing here touches the network (CI must never call a real API)."""

from __future__ import annotations

from typing import Any

from opengrid.ai_agent.providers import ModelRequest, ProviderError, TokenUsage
from opengrid.ai_agent.router import verdict_from
from opengrid.ai_agent.types import ProviderName, Purpose, RouterVerdict

CONTEXT: dict[str, Any] = {
    "health": {
        "reserve_breaches": 0,
        "double_sold_kwh": 0,
        "commitment_switches": 0,
        "alerts": [{"id": 7, "severity": "warning", "summary": "ERCOT price feed approaching staleness"}],
    },
    "hubs": {"counts": {"online": 198, "stale": 2}},
    "obligations": [
        {
            "obligation_id": "ffcc182c-d1fd",
            "service_type": "DIST_DEFERRAL",
            "state": "COMMITTED",
            "committed_qty_kw": 500,
            "at_risk": True,
            "last_reason_code": "FEEDER_OVERLOAD",
        },
        {
            "obligation_id": "1a7ccd31-cafc",
            "service_type": "ERCOT_AS",
            "state": "OFFERED",
            "value_per_mwh": 5.37,
            "degradation_cost": 0.03,
        },
    ],
}


class FakeProvider:
    """A scripted `ModelProvider`. Records every request it is handed."""

    def __init__(
        self,
        name: ProviderName = "claude",
        *,
        intent: str = "deterministic_query",
        confidence: float = 0.97,
        injection: float = 0.0,
        explanation: str | None = "Because the offer priced below cost.",
        available: bool = True,
        fail: bool = False,
        usage: TokenUsage | None = None,
        screen_model: str = "fake-screen-1",
        explain_model: str | None = "fake-explain-1",
    ) -> None:
        self._name: ProviderName = name
        self._intent = intent
        self._confidence = confidence
        self._injection = injection
        self._explanation = explanation
        self._available = available
        self._fail = fail
        self._usage = usage or TokenUsage(input_tokens=100, output_tokens=20)
        self._screen_model = screen_model
        self._explain_model = explain_model
        self.requests: list[tuple[Purpose, ModelRequest]] = []

    @property
    def name(self) -> ProviderName:
        return self._name

    @property
    def available(self) -> bool:
        return self._available

    def model_for(self, purpose: Purpose) -> str | None:
        return self._screen_model if purpose == "screen" else self._explain_model

    @property
    def calls(self) -> int:
        return len(self.requests)

    async def screen(self, request: ModelRequest, *, timeout_s: float) -> tuple[RouterVerdict, TokenUsage]:
        self.requests.append(("screen", request))
        if self._fail:
            raise ProviderError(f"{self._name}: stubbed outage", usage=self._usage)
        verdict = verdict_from(
            intent=self._intent,
            intent_confidence=self._confidence,
            needs_trace=0.2,
            injection_risk=self._injection,
            provider=self._name,
            model=self._screen_model,
        )
        return verdict, self._usage

    async def explain(self, request: ModelRequest, *, timeout_s: float) -> tuple[str, TokenUsage]:
        self.requests.append(("explain", request))
        if self._fail or self._explanation is None:
            raise ProviderError(f"{self._name}: stubbed outage", usage=self._usage)
        return self._explanation, self._usage


class RecordingTrace:
    """A trace sink that keeps what it was asked to write, or fails on demand."""

    def __init__(self, *, fail: bool = False) -> None:
        self.records: list[dict[str, Any]] = []
        self._fail = fail

    async def __call__(self, payload: dict[str, Any]) -> str | None:
        if self._fail:
            raise ConnectionError("trace store down")
        self.records.append(payload)
        return f"trace-{len(self.records)}"
