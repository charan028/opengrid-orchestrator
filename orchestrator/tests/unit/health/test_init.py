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
        # (zone, hub_id, last_seen_at, fault_code, current_health) -- current_health defaults to
        # "online" (the schema default, `og.hub_state.health`) so tests that only care about other
        # fields don't need to spell it out every time.
        self.hub_rows: list[tuple[str, str, datetime | None, str | None, str]] = []
        self.feed_statuses: list[FeedStatus] = []
        self.bank_loads: list[tuple[str, float, float | None]] = []
        self.open_alerts: list[Alert] = []
        self.written_hub_health: dict[str, str] = {}
        self.write_hub_health_batch_calls: list[list[tuple[str, str]]] = []
        self.raised: list[str] = []
        self.cleared: list[int] = []
        self._next_alert_id = 1

    async def fetch_heartbeats(self, pool):
        return self.heartbeats

    async def fetch_hub_states(self, pool):
        return self.hub_rows

    async def write_hub_health_batch(self, pool, changes):
        self.write_hub_health_batch_calls.append(list(changes))
        for hub_id, state in changes:
            self.written_hub_health[hub_id] = state

    async def fetch_feed_statuses(self, pool):
        return self.feed_statuses

    async def fetch_bank_loads(self, pool):
        return self.bank_loads

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
        ("LZ_NORTH", "hub-1", NOW, None, "online"),
        ("LZ_NORTH", "hub-2", NOW - timedelta(seconds=60), None, "online"),
    ]
    await health.evaluate_hub_health()
    assert fake_queries.written_hub_health == {"hub-2": "offline"}  # hub-1 stays "online" -- unchanged


async def test_evaluate_hub_health_skips_unchanged_rows(fake_queries: _FakeQueries) -> None:
    """Defect fix: a hub whose classification matches `hub_state.health` already is not written at all --
    proves the evaluator no longer does a write-storm of ~2,000 unconditional single-row commits/cycle."""
    fake_queries.hub_rows = [
        ("LZ_NORTH", "hub-1", NOW, None, "online"),  # still online: no write
        ("LZ_NORTH", "hub-2", NOW - timedelta(seconds=60), None, "offline"),  # still offline: no write
        ("LZ_NORTH", "hub-3", None, "INV-01", "fault"),  # still fault: no write
    ]
    await health.evaluate_hub_health()
    assert fake_queries.write_hub_health_batch_calls == [[]]
    assert fake_queries.written_hub_health == {}


async def test_evaluate_hub_health_writes_one_batch_for_many_changed_hubs(
    fake_queries: _FakeQueries,
) -> None:
    """Benchmark-style shape check (dispatch-live pass: ~2,000 hubs/cycle): every changed hub is written
    through exactly one `write_hub_health_batch` call, not one call per hub."""
    hub_count = 2_000
    fake_queries.hub_rows = [
        (
            "LZ_NORTH",
            f"hub-{i}",
            NOW - timedelta(seconds=60),  # offline threshold: everyone flips from "online"
            None,
            "online",
        )
        for i in range(hub_count)
    ]
    await health.evaluate_hub_health()
    assert len(fake_queries.write_hub_health_batch_calls) == 1
    batch = fake_queries.write_hub_health_batch_calls[0]
    assert len(batch) == hub_count
    assert all(state == "offline" for _hub_id, state in batch)


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


async def test_evaluate_alerts_uses_per_feed_staleness_threshold(fake_queries: _FakeQueries) -> None:
    """Defect fix: ALR-FEED-STALE must use each feed's own `opengrid.feeds.staleness` budget, not
    `health.heartbeat_down_after_s` (a few-second, heartbeat-scale value applied to every feed
    regardless of its real posting cadence -- which paged EIA, a ~3h-cadence source, as stale within
    seconds, and made ERCOT RT's own much shorter budget irrelevant to when it actually alerted)."""
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "sim", "settle", "api")
    ]
    fake_queries.feed_statuses = [
        # EIA default budget is 10800s (3h, `threshold_s_for_product`'s `eia_fresh_s` default): 2h old
        # is comfortably fresh.
        FeedStatus(source="EIA", product="eia-fuel-mix", last_value_at=NOW - timedelta(hours=2)),
        # ERCOT RT price (np6-905-cd) default budget is 600s (`ercot_price_fresh_s` default): 700s old
        # is stale.
        FeedStatus(source="ERCOT", product="np6-905-cd", last_value_at=NOW - timedelta(seconds=700)),
    ]

    await health.evaluate_alerts()

    assert "ALR-FEED-STALE:EIA:eia-fuel-mix" not in fake_queries.raised
    assert "ALR-FEED-STALE:ERCOT:np6-905-cd" in fake_queries.raised


async def test_evaluate_alerts_raises_scada_overload(fake_queries: _FakeQueries) -> None:
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "sim", "settle", "api")
    ]
    fake_queries.bank_loads = [("bank-000", 75.0, 95.0)]  # 126% of rating -> critical

    await health.evaluate_alerts()

    assert "ALR-SCADA-OVERLOAD:bank-000" in fake_queries.raised
    alert = next(a for a in fake_queries.open_alerts if a.rule == "ALR-SCADA-OVERLOAD")
    assert alert.severity == "critical"

    # Resolves once the load drops back under rating.
    fake_queries.raised.clear()
    fake_queries.bank_loads = [("bank-000", 75.0, 10.0)]
    await health.evaluate_alerts()
    assert not any(a.rule == "ALR-SCADA-OVERLOAD" for a in fake_queries.open_alerts)


async def test_evaluate_once_returns_snapshot_with_degraded_mode(fake_queries: _FakeQueries) -> None:
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "guardian", "safestop", "sim", "settle", "api")
    ]  # engine missing -> HOLD_LOCAL_AUTONOMY
    fake_queries.hub_rows = [("LZ_NORTH", "hub-1", NOW, None, "online")]

    snapshot = await health.evaluate_once()

    assert snapshot.degraded_modes == frozenset({"HOLD_LOCAL_AUTONOMY"})
    assert snapshot.hub_counts_by_zone["LZ_NORTH"].online == 1
    assert snapshot.open_alert_count >= 1  # at least ALR-PROCESS-DOWN for engine
