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
        # Both default to None (no data yet -- a cold start), matching `evaluate_sim_offline_alert`'s
        # "no data yet is not evidence of an offline sim" rule, so existing tests that don't care about
        # ALR-SIM-OFFLINE aren't affected by it.
        self.latest_fleet_seen_at: datetime | None = None
        self.latest_scada_seen_at: datetime | None = None
        self.degraded_mode_state: dict[str, datetime] = {}
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

    async def fetch_latest_hub_seen_at(self, pool):
        return self.latest_fleet_seen_at

    async def fetch_latest_scada_obs_at(self, pool):
        return self.latest_scada_seen_at

    async def fetch_degraded_modes(self, pool):
        return list(self.degraded_mode_state.items())

    async def write_degraded_modes(self, pool, active_modes, *, now):
        for mode in set(active_modes) - set(self.degraded_mode_state):
            self.degraded_mode_state[mode] = now
        for mode in set(self.degraded_mode_state) - set(active_modes):
            del self.degraded_mode_state[mode]

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
    # Every monitored process down -> 5 ALR-PROCESS-DOWN findings: `settle` (this evaluator's own host
    # process) is always "ok" regardless of its heartbeat row, and "sim" is no longer monitored by
    # heartbeat at all (both defect fixes -- see ALL_PROCESSES's docstring).
    fake_queries.heartbeats = []

    await health.evaluate_alerts()
    assert len(fake_queries.raised) == 5
    first_round_open = len(fake_queries.open_alerts)

    # Second cycle, same conditions: no new alerts, none cleared (TS-07-06 de-duplication).
    fake_queries.raised.clear()
    await health.evaluate_alerts()
    assert fake_queries.raised == []
    assert len(fake_queries.open_alerts) == first_round_open

    # Third cycle: every process now reports -> all open alerts clear.
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
    ]
    await health.evaluate_alerts()
    assert fake_queries.open_alerts == []
    assert len(fake_queries.cleared) == first_round_open


async def test_evaluate_alerts_never_clears_a_foreign_alert(fake_queries: _FakeQueries) -> None:
    """Defect fix: `evaluate_alerts()` must not auto-clear an alert another module raised and owns the
    lifecycle of, even across several cycles where health's own findings never mention it (it previously
    cleared ANY open alert whose rule+scope didn't match one of ITS OWN this-cycle findings, closing
    `ALR-SETTLE-STALLED`/`ALR-SELECTOR-GATE-FAILED`/`ALR-ENERGY-SHORTFALL-RISK` within one ~5s cycle of
    them being raised). Also covers guardian's own alerts (R2 coordination note: guardian raises AND
    clears `ALR-SCOPE-CONSERVATIVE`/`ALR-SAFE-STOP-REQUESTED`/`ALR-CLOCK-QUALITY` itself)."""
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
    ]
    foreign_alerts = [
        Alert(
            id=101,
            rule="ALR-SETTLE-STALLED",
            severity="critical",
            summary="settle cadence stalled",
            detail={"process": "settle"},
            opened_at=NOW,
        ),
        Alert(
            id=102,
            rule="ALR-SELECTOR-GATE-FAILED",
            severity="critical",
            summary="selector gate failed",
            detail={},
            opened_at=NOW,
        ),
        Alert(
            id=103,
            rule="ALR-ENERGY-SHORTFALL-RISK",
            severity="critical",
            summary="obligation OBL-1 energy margin -3.5 kWh",
            detail={"obligation_id": "OBL-1"},
            opened_at=NOW,
        ),
        Alert(
            id=104,
            rule="ALR-SCOPE-CONSERVATIVE",
            severity="warning",
            summary="bank-000 posture CONSERVATIVE",
            detail={"scope_ref": "bank-000"},
            opened_at=NOW,
        ),
        Alert(
            id=105,
            rule="ALR-SAFE-STOP-REQUESTED",
            severity="critical",
            summary="safe stop requested for bank-000",
            detail={"scope_ref": "bank-000"},
            opened_at=NOW,
        ),
        Alert(
            id=106,
            rule="ALR-CLOCK-QUALITY",
            severity="warning",
            summary="guardian NTP offset degraded",
            detail={},
            opened_at=NOW,
        ),
    ]
    fake_queries.open_alerts = list(foreign_alerts)

    for _ in range(3):  # several evaluation cycles -- must never touch the foreign alerts
        await health.evaluate_alerts()

    assert fake_queries.cleared == []
    assert {a.id for a in fake_queries.open_alerts} == {101, 102, 103, 104, 105, 106}


async def test_evaluate_alerts_still_clears_its_own_resolved_alert(fake_queries: _FakeQueries) -> None:
    """The other half of the same defect fix: restricting clears to `HEALTH_OWNED_ALERT_RULES` must not
    stop health from clearing its own alerts once their condition genuinely resolves."""
    fake_queries.heartbeats = []  # every monitored process down -> several ALR-PROCESS-DOWN findings
    await health.evaluate_alerts()
    engine_alert = next(
        a
        for a in fake_queries.open_alerts
        if a.rule == "ALR-PROCESS-DOWN" and a.detail.get("process") == "engine"
    )

    # Engine now reports -> the alert's own condition no longer holds -> health must clear it.
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
    ]
    await health.evaluate_alerts()

    assert engine_alert.id in fake_queries.cleared
    assert not any(a.rule == "ALR-PROCESS-DOWN" for a in fake_queries.open_alerts)


async def test_evaluate_alerts_uses_per_feed_staleness_threshold(fake_queries: _FakeQueries) -> None:
    """Defect fix: ALR-FEED-STALE must use each feed's own `opengrid.feeds.staleness` budget, not
    `health.heartbeat_down_after_s` (a few-second, heartbeat-scale value applied to every feed
    regardless of its real posting cadence -- which paged EIA, a ~3h-cadence source, as stale within
    seconds, and made ERCOT RT's own much shorter budget irrelevant to when it actually alerted)."""
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
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


async def test_evaluate_alerts_suppresses_eia_stale_while_ercot_fallback_primary_healthy(
    fake_queries: _FakeQueries,
) -> None:
    """Defect fix: EIA's `feed_status` row is only ever written while it's the engaged fallback for
    ERCOT's np6-345-cd system-load product (`opengrid.feeds.scheduler._poll_eia_fallback` runs only from
    inside `_poll_ercot_product` when ERCOT's own breaker blocks the request). An old EIA row is normal,
    not a problem, while ERCOT np6-345-cd itself is healthy -- it must not raise ALR-FEED-STALE."""
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
    ]
    fake_queries.feed_statuses = [
        # Ancient (10 days old) -- would trip even EIA's generous 3h budget on its own.
        FeedStatus(source="EIA", product="eia-demand", last_value_at=NOW - timedelta(days=10)),
        # The ERCOT primary EIA backs is healthy: fresh, breaker closed.
        FeedStatus(source="ERCOT", product="np6-345-cd", last_value_at=NOW - timedelta(seconds=30)),
    ]

    await health.evaluate_alerts()

    assert not any(key.startswith("ALR-FEED-STALE:EIA") for key in fake_queries.raised)


async def test_evaluate_alerts_raises_eia_stale_while_ercot_fallback_primary_down(
    fake_queries: _FakeQueries,
) -> None:
    """The other half of the same defect fix: once ERCOT np6-345-cd is actually down (breaker open), the
    system genuinely needs the EIA fallback, so a stale EIA reading is a real problem again."""
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
    ]
    fake_queries.feed_statuses = [
        FeedStatus(source="EIA", product="eia-demand", last_value_at=NOW - timedelta(days=10)),
        FeedStatus(source="ERCOT", product="np6-345-cd", last_value_at=NOW, breaker_open=True),
    ]

    await health.evaluate_alerts()

    assert "ALR-FEED-STALE:EIA:eia-demand" in fake_queries.raised


async def test_evaluate_alerts_raises_sim_offline_when_fleet_and_scada_both_stale(
    fake_queries: _FakeQueries,
) -> None:
    """Defect fix: `ogsim` writes no `og.heartbeat` row (it's an external system, BUILD.md S1), so its
    liveness comes from the freshest of its own MQTT-driven writes -- both gone stale here."""
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
    ]
    stale_at = NOW - timedelta(seconds=health._thresholds.sim_offline_s + 1)
    fake_queries.latest_fleet_seen_at = stale_at
    fake_queries.latest_scada_seen_at = stale_at

    await health.evaluate_alerts()

    assert "ALR-SIM-OFFLINE" in fake_queries.raised


async def test_evaluate_alerts_no_sim_offline_when_fleet_or_scada_fresh(fake_queries: _FakeQueries) -> None:
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
    ]
    fake_queries.latest_fleet_seen_at = NOW
    fake_queries.latest_scada_seen_at = NOW - timedelta(seconds=health._thresholds.sim_offline_s + 1)

    await health.evaluate_alerts()

    assert "ALR-SIM-OFFLINE" not in fake_queries.raised


async def test_evaluate_alerts_raises_scada_overload(fake_queries: _FakeQueries) -> None:
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
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
        for p in ("feeds", "guardian", "safestop", "settle", "api")
    ]  # engine missing -> HOLD_LOCAL_AUTONOMY
    fake_queries.hub_rows = [("LZ_NORTH", "hub-1", NOW, None, "online")]

    snapshot = await health.evaluate_once()

    assert snapshot.degraded_modes == frozenset({"HOLD_LOCAL_AUTONOMY"})
    assert snapshot.hub_counts_by_zone["LZ_NORTH"].online == 1
    assert snapshot.open_alert_count >= 1  # at least ALR-PROCESS-DOWN for engine
    # R2 item 1 (defect fix): the computed degraded-mode set must be persisted (`og.degraded_mode_state`)
    # so `GET /og/api/health` and the UI banners can read it -- it used to go nowhere else.
    assert set(fake_queries.degraded_mode_state) == {"HOLD_LOCAL_AUTONOMY"}


async def test_evaluate_once_clears_persisted_degraded_mode_on_resolve(fake_queries: _FakeQueries) -> None:
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "guardian", "safestop", "settle", "api")
    ]  # engine still missing -> HOLD_LOCAL_AUTONOMY
    await health.evaluate_once()
    assert set(fake_queries.degraded_mode_state) == {"HOLD_LOCAL_AUTONOMY"}

    # Engine now reports -> the degraded mode resolves and must be cleared from the persisted set too.
    fake_queries.heartbeats = [
        Heartbeat(process=p, pid=1, ts=NOW, status="ok")
        for p in ("feeds", "engine", "guardian", "safestop", "settle", "api")
    ]
    await health.evaluate_once()
    assert fake_queries.degraded_mode_state == {}
