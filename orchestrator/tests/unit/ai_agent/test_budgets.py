"""Budgets refuse fast so the console is never slowed by the assistant (issue #26 item 6)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.ai_agent.budgets import TIMEOUT_CEILING_S, Budget, BudgetLimits


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


def test_headroom_reports_what_is_left_not_just_what_was_spent() -> None:
    budget = Budget(BudgetLimits(daily_tokens=1000))
    budget.record(tokens=250)
    headroom = budget.headroom()
    assert headroom["tokens_used"] == 250
    assert headroom["tokens_left_pct"] == 75.0
