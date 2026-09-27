"""Screening resilience (lead follow-up to the r3.4.3 incident: three 20 s Haiku screening timeouts).

Pinned here: screening has its own short timeout and one retry on a timeout / connection error / 5xx
(never on auth, bad request or rate limit); explanations are not retried; every attempt is measured
(latency, error class) and logged without any content; failed screenings in a row drive
`ScreeningHealth.alerting`, reset by one success; budget refusals are not counted as failures.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from opengrid.ai_agent.budgets import Budget, BudgetLimits
from opengrid.ai_agent.gateway import ModelGateway, ScreeningHealth, error_class
from opengrid.ai_agent.providers import ModelRequest, ProviderError, TokenUsage
from opengrid.ai_agent.router import verdict_from
from opengrid.ai_agent.types import ProviderName, Purpose, RouterVerdict
from opengrid.platform import metrics

from .fakes import CONTEXT

QUESTION = "how many hubs in LZ_NORTH have a secret-looking phrase xyzzy"


class ScriptedProvider:
    """Each screen call takes the next scripted step: a delay in seconds (then succeeds) or an error."""

    def __init__(self, steps: list[float | str]) -> None:
        self.steps = list(steps)
        self.screens = 0
        self.explains = 0

    @property
    def name(self) -> ProviderName:
        return "claude"

    @property
    def available(self) -> bool:
        return True

    def model_for(self, purpose: Purpose) -> str | None:
        return "fake-screen" if purpose == "screen" else "fake-explain"

    async def _step(self) -> None:
        step = self.steps.pop(0) if self.steps else 0.0
        if isinstance(step, str):
            raise ProviderError(f"claude: {step}", usage=TokenUsage(input_tokens=10))
        await asyncio.sleep(step)

    async def screen(self, request: ModelRequest, *, timeout_s: float) -> tuple[RouterVerdict, TokenUsage]:
        self.screens += 1
        await self._step()
        verdict = verdict_from(
            intent="fleet_query",
            intent_confidence=0.9,
            needs_trace=0.1,
            injection_risk=0.0,
            provider="claude",
            model="fake-screen",
        )
        return verdict, TokenUsage(input_tokens=100, output_tokens=10)

    async def explain(self, request: ModelRequest, *, timeout_s: float) -> tuple[str, TokenUsage]:
        self.explains += 1
        await self._step()
        return "text", TokenUsage(input_tokens=100, output_tokens=10)


def _gateway(provider: ScriptedProvider, **limits: float) -> ModelGateway:
    base = {"screen_timeout_s": 0.05, "screen_retries": 1, "timeout_s": 1.0, "screen_alert_after": 3}
    return ModelGateway(primary=provider, budget=Budget(BudgetLimits(**{**base, **limits})))  # type: ignore[arg-type]


REQUEST = ModelRequest.build(QUESTION, CONTEXT)


async def test_a_slow_screen_times_out_fast_and_the_retry_answers() -> None:
    provider = ScriptedProvider([0.5, 0.0])  # first attempt hangs past the 50 ms screen timeout

    outcome = await _gateway(provider).screen(REQUEST)

    assert outcome.value is not None and provider.screens == 2
    first, second = outcome.calls
    assert (first.ok, first.error, first.attempt) == (False, "claude: timeout", 1)
    assert first.latency_ms is not None and first.latency_ms < 400, "bounded by screen_timeout_s, not 20 s"
    assert (second.ok, second.attempt) == (True, 2)


@pytest.mark.parametrize("error", ["auth_error", "bad_request", "rate_limited", "refused"])
async def test_errors_that_would_repeat_are_not_retried(error: str) -> None:
    provider = ScriptedProvider([error, 0.0])
    outcome = await _gateway(provider).screen(REQUEST)
    assert outcome.value is None and provider.screens == 1


@pytest.mark.parametrize("error", ["connection_error", "http_503"])
async def test_transport_errors_are_retried_once(error: str) -> None:
    provider = ScriptedProvider([error, error, 0.0])
    outcome = await _gateway(provider).screen(REQUEST)
    assert outcome.value is None and provider.screens == 2  # 1 + screen_retries


async def test_explanations_keep_the_long_timeout_and_are_not_retried() -> None:
    provider = ScriptedProvider([0.2])  # longer than screen_timeout_s, shorter than timeout_s
    outcome = await _gateway(provider).explain(REQUEST)
    assert outcome.value == "text" and provider.explains == 1


async def test_consecutive_failed_screenings_alert_and_one_success_clears() -> None:
    provider = ScriptedProvider(["connection_error"] * 6 + [0.0])
    gateway = _gateway(provider)

    for _ in range(3):
        await gateway.screen(REQUEST)
    assert gateway.screening.alerting and gateway.screening.consecutive_failures == 3
    assert gateway.screening.last_error == "connection_error"
    assert gateway.status()["screening"]["alerting"] is True

    await gateway.screen(REQUEST)
    assert not gateway.screening.alerting and gateway.screening.consecutive_failures == 0


async def test_a_budget_refusal_is_not_a_screening_failure() -> None:
    gateway = _gateway(ScriptedProvider([]), daily_usd=0.0)
    outcome = await gateway.screen(REQUEST)
    assert outcome.budget_refusal is not None and gateway.screening.screenings == 0


async def test_calls_are_logged_by_latency_and_class_never_content(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="opengrid.ai_agent.gateway")
    await _gateway(ScriptedProvider([0.5, 0.0])).screen(REQUEST)

    records = [r for r in caplog.records if r.getMessage() == "copilot model call"]
    assert [(r.__dict__["attempt"], r.__dict__["outcome"]) for r in records] == [(1, "timeout"), (2, "ok")]
    assert all(isinstance(r.__dict__["latency_ms"], int) for r in records)
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.__dict__)
        assert "xyzzy" not in rendered and "LZ_NORTH" not in rendered


async def test_attempts_are_measured_in_the_metrics_registry() -> None:
    histogram = metrics.copilot_model_call_seconds.labels(purpose="screen", outcome="timeout")
    before = histogram._sum.get()  # prometheus_client exposes no public reader
    await _gateway(ScriptedProvider([0.5, 0.0])).screen(REQUEST)
    assert histogram._sum.get() > before


def test_error_class_is_the_class_only() -> None:
    assert error_class("claude: timeout") == "timeout"
    assert error_class(None) == "ok"
    health = ScreeningHealth(alert_after=2)
    health.record(ok=False, error="claude: http_502")
    assert health.last_error == "http_502" and not health.alerting
    health.record(ok=False, error="claude: timeout")
    assert health.alerting


def test_screening_limits_from_config_are_clamped() -> None:
    cfg = {
        "ai_agent.timeout_s": 20.0,
        "ai_agent.screen_timeout_s": 60.0,
        "ai_agent.screen_retries": 9,
        "ai_agent.screen_alert_after": 0,
    }

    class Reader:
        def get(self, path: str, default: object = None) -> object:
            return cfg.get(path, default)

    limits = BudgetLimits.from_config(Reader())
    assert (limits.screen_timeout_s, limits.screen_retries, limits.screen_alert_after) == (20.0, 2, 1)
    defaults = BudgetLimits.from_config(type("Empty", (), {"get": lambda self, p, d=None: d})())
    assert (defaults.screen_timeout_s, defaults.screen_retries, defaults.screen_alert_after) == (5.0, 1, 3)
