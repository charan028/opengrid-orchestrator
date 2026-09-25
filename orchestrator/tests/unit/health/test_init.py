"""Unit tests for `opengrid.health`'s orchestration (`evaluate_heartbeats`/`evaluate_hub_health`/
`evaluate_alerts`/`evaluate_once`) with `health.queries` monkeypatched -- no real Postgres. Metrics
scraping is left unconfigured (`health.engine_metrics_url` unset) so cycle-latency/guardian-timeout
paths short-circuit to `None`/no-op, keeping these tests focused on the DB-driven logic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import opengrid.health as health
from opengrid.core.models.platform import Alert, FeedStatus, Heartbeat
from opengrid.platform.config import Config

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_health_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """`opengrid.health` keeps module-level state (BUILD.md's stub no-arg interface); reset it so tests
    don't leak into each other, and freeze its clock via the `_now()` seam (no flaky real-time compares)."""
    health.configure(pool=object(), cfg=Config({}))  # type: ignore[arg-type]
    health._cycle_p99_consecutive_breaches = 0
    health._cycle_p99_history.clear()
    monkeypatch.setattr(health, "_now", lambda: NOW)
    yield
    health._pool = None


class _FakeQueries:
    def __init__(self) -> None:
        self.heartbeats: list[Heartbeat] = []
        self.hub_rows: list[tuple[str, str, datetime | None, str | None]] = []
        self.feed_statuses: list[FeedStatus] = []
        self.open_alerts: list[Alert] = []
        self.written_hub_health: dict[str, str] = {}
        self.raised: list[str] = []
        self.cleared: list[int] = []
        self._next_alert_id = 1

    async def fetch_heartbeats(self, pool):
        return self.heartbeats

    async def fetch_hub_states(self, pool):
        return self.hub_rows

    async def write_hub_health(self, pool, hub_id, state):
        self.written_hub_health[hub_id] = state

    async def fetch_feed_statuses(self, pool):
        return self.feed_statuses

    async def fetch_open_alerts(self, pool):
        return self.open_alerts

    async def raise_alert(self, pool, finding, *, opened_at):
        self.raised.append(finding.condition_key)
        alert = Alert(
            id=self._next_alert_id,
            rule=finding.rule,
            severity=finding.severity,
            summary=finding.summary,
            detail=finding.detail,
            opened_at=opened_at,
        )
        self.open_alerts.append(alert)
        self._next_alert_id += 1
        return alert.id

    async def clear_alert(self, pool, alert_id, *, cleared_at=None):
        self.cleared.append(alert_id)
        self.open_alerts = [a for a in self.open_alerts if a.id != alert_id]

    def condition_key_for(self, alert):
        from opengrid.health.queries import condition_key_for as _real

        return _real(alert)


@pytest.fixture
def fake_queries(monkeypatch: pytest.MonkeyPatch) -> _FakeQueries:
    fake = _FakeQueries()
    monkeypatch.setattr(health, "queries", fake)
    return fake


async def test_evaluate_heartbeats_reports_down_for_missing_process(fake_queries: _FakeQueries) -> None:
    fake_queries.heartbeats = [Heartbeat(process="engine", pid=1, ts=NOW, status="ok")]
    result = await health.evaluate_heartbeats()
    assert result["engine"] == "ok"
    assert result["guardian"] == "down"  # never reported


async def test_evaluate_hub_health_writes_classification_back(fake_queries: _FakeQueries) -> None:
    fake_queries.hub_rows = [
        ("LZ_NORTH", "hub-1", NOW, None),
        ("LZ_NORTH", "hub-2", NOW - timedelta(seconds=60), None),
    ]
    await health.evaluate_hub_health()
    assert fake_queries.written_hub_health == {"hub-1": "online", "hub-2": "offline"}


async def test_evaluate_alerts_raises_once_and_clears_on_resolve(fake_queries: _FakeQueries) -> None:
    fake_queries.heartbeats = []  # every process down -> 7 ALR-PROCESS-DOWN findings

    await health.evaluate_alerts()
    assert len(fake_queries.raised) == 7
    first_round_open = len(fake_queries.open_alerts)

    # Second cycle, same conditions: no new alerts, none cleared (TS-07-06 de-duplication).
    fake_queries.raised.clear()
    await health.evaluate_alerts()
    assert fake_queries.raised == []
    assert len(fake_queries.open_alerts) == first_round_open

    # Third cycle: every process now reports -> all open alerts clear.
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "sim", "settle", "api")
    ]
    await health.evaluate_alerts()
    assert fake_queries.open_alerts == []
    assert len(fake_queries.cleared) == first_round_open


async def test_evaluate_once_returns_snapshot_with_degraded_mode(fake_queries: _FakeQueries) -> None:
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "guardian", "safestop", "sim", "settle", "api")
    ]  # engine missing -> HOLD_LOCAL_AUTONOMY
    fake_queries.hub_rows = [("LZ_NORTH", "hub-1", NOW, None)]

    snapshot = await health.evaluate_once()

    assert snapshot.degraded_modes == frozenset({"HOLD_LOCAL_AUTONOMY"})
    assert snapshot.hub_counts_by_zone["LZ_NORTH"].online == 1
    assert snapshot.open_alert_count >= 1  # at least ALR-PROCESS-DOWN for engine
