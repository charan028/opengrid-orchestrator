"""ALR-COPILOT-SCREENING: raised once when screening keeps failing, cleared by the next success, and
never an error for the operator (`opengrid.api.routers.ai.sync_screening_alert`)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import pytest

from opengrid.ai_agent.gateway import ScreeningHealth
from opengrid.api.routers import ai as ai_routes


@dataclass
class _Alert:
    id: int
    rule: str


class FakeAlerts:
    def __init__(self, *, fail: bool = False) -> None:
        self.open: list[_Alert] = []
        self.raised: list[Any] = []
        self.cleared: list[int] = []
        self.reads = 0
        self.fail = fail

    async def fetch_open_alerts(self, pool: Any) -> list[_Alert]:
        self.reads += 1
        if self.fail:
            raise ConnectionError("db down")
        return list(self.open)

    async def raise_alert(self, pool: Any, finding: Any, *, opened_at: datetime) -> int:
        self.raised.append(finding)
        self.open.append(_Alert(len(self.raised), finding.rule))
        return len(self.raised)

    async def clear_alert(self, pool: Any, alert_id: int, *, cleared_at: datetime | None = None) -> None:
        self.cleared.append(alert_id)
        self.open = [a for a in self.open if a.id != alert_id]


@pytest.fixture
def alerts(monkeypatch: pytest.MonkeyPatch) -> FakeAlerts:
    fake = FakeAlerts()
    for name in ("fetch_open_alerts", "raise_alert", "clear_alert"):
        monkeypatch.setattr(ai_routes, name, getattr(fake, name))
    monkeypatch.setattr(ai_routes, "_screening_alert_open", None)
    return fake


async def test_raised_once_while_failing_and_cleared_by_a_success(alerts: FakeAlerts) -> None:
    health = ScreeningHealth(alert_after=3)
    pool = object()

    for _ in range(5):
        health.record(ok=False, error="claude: timeout")
        await ai_routes.sync_screening_alert(pool, health)  # type: ignore[arg-type]

    assert len(alerts.raised) == 1
    finding = alerts.raised[0]
    assert finding.rule == "ALR-COPILOT-SCREENING" and finding.severity == "warning"
    assert "timeout" in finding.summary and finding.detail["consecutive_failures"] == 3
    reads_while_failing = alerts.reads

    health.record(ok=True, error=None)
    await ai_routes.sync_screening_alert(pool, health)  # type: ignore[arg-type]
    assert alerts.cleared == [1] and alerts.open == []

    health.record(ok=True, error=None)
    await ai_routes.sync_screening_alert(pool, health)  # type: ignore[arg-type]
    assert alerts.reads == reads_while_failing + 1, "a healthy copilot costs no alert query per question"


async def test_an_alert_left_open_by_a_previous_process_is_cleared(alerts: FakeAlerts) -> None:
    alerts.open.append(_Alert(41, "ALR-COPILOT-SCREENING"))
    health = ScreeningHealth(alert_after=3)
    health.record(ok=True, error=None)

    await ai_routes.sync_screening_alert(object(), health)  # type: ignore[arg-type]

    assert alerts.cleared == [41]


async def test_a_failing_alert_write_never_reaches_the_operator(
    monkeypatch: pytest.MonkeyPatch, alerts: FakeAlerts
) -> None:
    alerts.fail = True
    health = ScreeningHealth(alert_after=1)
    health.record(ok=False, error="claude: timeout")

    await ai_routes.sync_screening_alert(object(), health)  # type: ignore[arg-type]  # no exception

    assert alerts.raised == []


async def test_no_pool_no_alert(alerts: FakeAlerts) -> None:
    health = ScreeningHealth(alert_after=1)
    health.record(ok=False, error="claude: timeout")
    await ai_routes.sync_screening_alert(None, health)
    assert alerts.reads == 0
