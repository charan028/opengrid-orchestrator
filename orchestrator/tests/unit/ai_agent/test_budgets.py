"""Budgets refuse fast so the console is never slowed by the assistant (issue #26 item 6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from opengrid.ai_agent.budgets import (
    TIMEOUT_CEILING_S,
    UNPRICED,
    Budget,
    BudgetLimits,
    ModelPrice,
    Pricing,
)
from opengrid.ai_agent.gateway import ModelGateway
from opengrid.ai_agent.providers import ModelRequest, TokenUsage

from .fakes import CONTEXT, FakeProvider


class _Cfg:
    def __init__(self, values: dict[str, object]) -> None:
        self._values = values

    def get(self, path: str, default: object = None) -> object:
        return self._values.get(path, default)


def test_a_fresh_budget_allows_the_request() -> None:
    assert Budget(BudgetLimits()).check() is None


def test_the_timeout_can_never_exceed_the_ceiling() -> None:
    """A misconfiguration must not be able to hang an operator's panel."""
    limits = BudgetLimits.from_config(_Cfg({"ai_agent.timeout_s": 600}))
    assert limits.timeout_s == TIMEOUT_CEILING_S


def test_zero_daily_cost_switches_the_assistant_off() -> None:
    budget = Budget(BudgetLimits(daily_usd=0.0))
    assert budget.check() == "the assistant is switched off in this deployment"


def test_the_token_budget_stops_further_requests() -> None:
    budget = Budget(BudgetLimits(daily_tokens=100))
    budget.record(tokens=100)
    assert budget.check() == "today's token budget for the assistant is spent"


def test_the_rate_limit_counts_only_the_last_minute() -> None:
    budget = Budget(BudgetLimits(requests_per_minute=2))
    now = datetime(2026, 9, 26, 17, 0, tzinfo=UTC)
    budget.record(tokens=1, now=now)
    budget.record(tokens=1, now=now)
    assert budget.check(now=now) == "too many assistant requests in the last minute"
    assert budget.check(now=now + timedelta(seconds=61)) is None


def test_spend_resets_on_a_new_day() -> None:
    budget = Budget(BudgetLimits(daily_tokens=10))
    day_one = datetime(2026, 9, 26, 23, 0, tzinfo=UTC)
    budget.record(tokens=10, now=day_one)
    assert budget.check(now=day_one) is not None
    assert budget.check(now=day_one + timedelta(hours=2)) is None


def test_dollars_come_from_token_usage_times_the_configured_price() -> None:
    pricing = Pricing.from_config(
        _Cfg({"ai_agent.prices": {"claude-opus-5-5": {"input_per_mtok": 4.0, "output_per_mtok": 20.0}}})
    )

    assert pricing.cost_usd("claude-opus-5-5", input_tokens=1_000_000, output_tokens=0) == 4.0
    assert pricing.cost_usd("claude-opus-5-5", input_tokens=10_000, output_tokens=1_000) == pytest.approx(
        0.06
    )


def test_an_unpriced_model_is_charged_at_the_conservative_rate() -> None:
    assert Pricing().price_of("some-new-model") == UNPRICED
    assert UNPRICED.input_per_mtok >= 4.0 and UNPRICED.output_per_mtok >= 20.0


def test_the_daily_dollar_cap_blocks() -> None:
    budget = Budget(BudgetLimits(daily_usd=0.05))
    budget.record(tokens=10, usd=0.05)

    assert budget.check() == "today's cost budget for the assistant is spent"


async def test_every_call_is_charged_including_failures_and_the_fallback() -> None:
    """Review item 5: screening, explanation and fallback calls all count, in tokens, dollars and rate."""
    pricing = Pricing(
        {
            "fake-screen-1": ModelPrice(1.0, 5.0),
            "fake-explain-1": ModelPrice(4.0, 20.0),
            "jev-latest": ModelPrice(1.0, 1.0),
        }
    )
    budget = Budget(BudgetLimits(daily_usd=100.0))
    claude = FakeProvider("claude", intent="explain_decision", fail=True)
    typesafe = FakeProvider(
        "typesafe", intent="explain_decision", screen_model="jev-latest", explain_model=None
    )
    gateway = ModelGateway(
        primary=claude, fallback=typesafe, fallback_enabled=True, budget=budget, pricing=pricing
    )
    request = ModelRequest.build("why?", CONTEXT)

    screened = await gateway.screen(request)

    assert [c.provider for c in screened.calls] == ["claude", "typesafe"]
    expected_usd = (100 * 1.0 + 20 * 5.0 + 100 * 1.0 + 20 * 1.0) / 1_000_000
    headroom = budget.headroom()
    assert headroom["tokens_used"] == 240
    assert headroom["usd_used"] == pytest.approx(round(expected_usd, 4))
    assert headroom["requests_last_minute"] == 2


async def test_a_spent_dollar_cap_stops_the_next_model_call() -> None:
    budget = Budget(BudgetLimits(daily_usd=0.0005))
    pricing = Pricing({"fake-screen-1": ModelPrice(1.0, 5.0)})
    claude = FakeProvider("claude", usage=TokenUsage(input_tokens=400, output_tokens=40))
    gateway = ModelGateway(primary=claude, budget=budget, pricing=pricing)
    request = ModelRequest.build("anything at risk?", CONTEXT)

    first = await gateway.screen(request)  # $0.0004 in + $0.0002 out = $0.0006, over the $0.0005 cap
    second = await gateway.screen(request)

    assert first.value is not None
    assert second.value is None and second.calls == []
    assert second.budget_refusal == "today's cost budget for the assistant is spent"
    assert claude.calls == 1


async def test_the_per_minute_limit_counts_model_calls() -> None:
    budget = Budget(BudgetLimits(requests_per_minute=2))
    claude = FakeProvider("claude")
    gateway = ModelGateway(primary=claude, budget=budget)
    request = ModelRequest.build("anything at risk?", CONTEXT)

    for _ in range(3):
        await gateway.screen(request)

    assert claude.calls == 2


def test_headroom_reports_what_is_left_not_just_what_was_spent() -> None:
    budget = Budget(BudgetLimits(daily_tokens=1000))
    budget.record(tokens=250)
    headroom = budget.headroom()
    assert headroom["tokens_used"] == 250
    assert headroom["tokens_left_pct"] == 75.0
