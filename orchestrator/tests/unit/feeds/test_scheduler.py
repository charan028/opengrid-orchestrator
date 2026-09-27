"""FeedsScheduler: breaker-driven EIA fallback engage/disengage, key-rotation tracing, and staleness
transition tracing (TS-02-03, TS-02-05/07)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest

from opengrid.core.models.platform import FeedObs
from opengrid.feeds import configured_ercot_products
from opengrid.feeds.breaker import CONSECUTIVE_FAILURE_THRESHOLD
from opengrid.feeds.ercot import KeyRotationEvent
from opengrid.feeds.http_client import FeedHttpError
from opengrid.feeds.scheduler import POLL_INTERVAL_S, FeedsScheduler, next_poll_delay_s
from opengrid.feeds.token_bucket import TokenBucket

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
STALENESS_CFG = {
    "ercot_price_fresh_s": 600,
    "ercot_load_fresh_s": 1800,
    "wind_solar_fresh_s": 10800,
    "nws_fresh_s": 10800,
    "eia_fresh_s": 10800,
}


def _obs(source: str, product: str, series: str, ts: datetime, *, quality: str = "GOOD") -> FeedObs:
    return FeedObs(
        source=source,
        product=product,
        series=series,
        ts=ts,
        value=1.0,
        unit="mw",
        quality=quality,
        recorded_at=ts,
    )


@dataclass
class FakeErcot:
    active_key: str = "PRIMARY"
    fail: bool = False
    rotation_events: list[KeyRotationEvent] = field(default_factory=list)

    async def fetch_product(
        self, product: str, *, now: datetime
    ) -> tuple[list[FeedObs], list[KeyRotationEvent]]:
        if self.fail:
            raise FeedHttpError(500, "boom")
        events, self.rotation_events = self.rotation_events, []
        return [_obs("ERCOT", product, "LZ_NORTH", now)], events


@dataclass
class FakeEia:
    fail: bool = False

    async def hourly_demand(self, *, recorded_at: datetime) -> list[FeedObs]:
        if self.fail:
            raise FeedHttpError(500, "boom")
        return [_obs("EIA", "eia-demand", "ERCOT_SYSTEM", recorded_at, quality="ESTIMATED")]


@dataclass
class FakeNws:
    async def resolve_grid_point(self, *, pinned: str | None) -> object:
        return object()

    async def hourly_forecast(self, grid_point: object, *, recorded_at: datetime) -> list[FeedObs] | None:
        return None  # 304 Not Modified path, simplest for scheduler-focused tests


@dataclass
class FakeStore:
    obs: list[FeedObs] = field(default_factory=list)
    status_calls: list[dict] = field(default_factory=list)

    async def upsert_obs(self, rows: list[FeedObs]) -> None:
        self.obs.extend(rows)

    async def update_status(self, **kwargs: object) -> None:
        self.status_calls.append(kwargs)


@dataclass
class FakeTrace:
    events: list[tuple[str, str, str, dict]] = field(default_factory=list)

    async def append(self, stream_id: str, decision_type: str, event_class: str, payload: dict) -> None:
        self.events.append((stream_id, decision_type, event_class, payload))


def _scheduler(ercot: FakeErcot, eia: FakeEia, store: FakeStore, trace: FakeTrace) -> FeedsScheduler:
    return FeedsScheduler(
        ercot=ercot,  # type: ignore[arg-type]
        eia=eia,  # type: ignore[arg-type]
        nws=FakeNws(),  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        staleness_cfg=STALENESS_CFG,
        nws_grid_point_pinned="EWX/156,91",
        ercot_bucket=TokenBucket(capacity=100, refill_per_s=100),
        trace=trace,  # type: ignore[arg-type]
    )


async def test_successful_poll_writes_obs_and_status() -> None:
    store, trace = FakeStore(), FakeTrace()
    scheduler = _scheduler(FakeErcot(), FakeEia(), store, trace)
    await scheduler.run_cycle(now=T0)
    assert store.obs  # every due product (all 5, on a cold-start cycle) wrote at least one row
    assert any(call["source"] == "ERCOT" for call in store.status_calls)


async def test_ercot_breaker_opens_and_engages_eia_fallback_for_load() -> None:
    """Drives `_poll_ercot_product` directly for the load product (`np6-345-cd`, the only one EIA can
    stand in for) so the breaker -- shared across all five ERCOT products -- opens specifically on a
    load-product failure, the case that must trigger the EIA fallback (02b S2.3)."""
    store, trace = FakeStore(), FakeTrace()
    ercot = FakeErcot(fail=True)
    eia = FakeEia()
    scheduler = _scheduler(ercot, eia, store, trace)

    now = T0
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
        await scheduler._poll_ercot_product("np6-345-cd", now=now)
        now += timedelta(seconds=1)

    assert scheduler._ercot_breaker.is_open
    breaker_open_events = [e for e in trace.events if e[2] == "BREAKER_OPEN"]
    assert len(breaker_open_events) == 1
    fallback_events = [e for e in trace.events if e[2] == "EIA_FALLBACK_ENGAGED"]
    assert len(fallback_events) == 1
    assert any(row.source == "EIA" for row in store.obs)


async def test_eia_fallback_disengages_when_ercot_recovers() -> None:
    store, trace = FakeStore(), FakeTrace()
    ercot = FakeErcot(fail=True)
    scheduler = _scheduler(ercot, FakeEia(), store, trace)

    now = T0
    for _ in range(CONSECUTIVE_FAILURE_THRESHOLD):
        await scheduler._poll_ercot_product("np6-345-cd", now=now)
        now += timedelta(seconds=1)
    assert any(e[2] == "EIA_FALLBACK_ENGAGED" for e in trace.events)

    ercot.fail = False
    now += timedelta(seconds=90)  # past the 60s open duration, so the breaker allows a half-open probe
    await scheduler._poll_ercot_product("np6-345-cd", now=now)

    assert any(e[2] == "EIA_FALLBACK_DISENGAGED" for e in trace.events)


async def test_key_rotation_is_traced() -> None:
    store, trace = FakeStore(), FakeTrace()
    ercot = FakeErcot()
    ercot.rotation_events = [KeyRotationEvent(from_key="PRIMARY", to_key="SECONDARY", reason="http_401")]
    scheduler = _scheduler(ercot, FakeEia(), store, trace)

    await scheduler.run_cycle(now=T0)

    rotation_traces = [e for e in trace.events if e[2] == "KEY_ROTATION"]
    assert len(rotation_traces) == 1
    assert rotation_traces[0][3]["to_key"] == "SECONDARY"


async def test_staleness_transition_traced_once() -> None:
    store, trace = FakeStore(), FakeTrace()
    ercot = FakeErcot()
    scheduler = _scheduler(ercot, FakeEia(), store, trace)

    stale_now = T0 + timedelta(seconds=700)  # > 600s ercot_price_fresh_s, obs.ts == poll `now`

    # Force staleness by having fetch_product report an observation timestamped well in the past.
    async def stale_fetch(product: str, *, now: datetime):
        return [_obs("ERCOT", product, "LZ_NORTH", T0)], []

    ercot.fetch_product = stale_fetch  # type: ignore[method-assign]
    await scheduler.run_cycle(now=stale_now)

    stale_traces = [e for e in trace.events if e[2] == "STALE"]
    assert len(stale_traces) >= 1


# --- R3.4: products list honoured; failed/skipped polls retried with backoff -------------------------


@dataclass
class CountingErcot(FakeErcot):
    calls: list[str] = field(default_factory=list)
    failing: set[str] = field(default_factory=set)

    async def fetch_product(
        self, product: str, *, now: datetime
    ) -> tuple[list[FeedObs], list[KeyRotationEvent]]:
        self.calls.append(product)
        if product in self.failing:
            raise FeedHttpError(503, "ercot down")
        return await super().fetch_product(product, now=now)


def _scheduler_with(ercot: FakeErcot, **kwargs: object) -> FeedsScheduler:
    return FeedsScheduler(
        ercot=ercot,  # type: ignore[arg-type]
        eia=FakeEia(),  # type: ignore[arg-type]
        nws=FakeNws(),  # type: ignore[arg-type]
        store=FakeStore(),  # type: ignore[arg-type]
        staleness_cfg=STALENESS_CFG,
        nws_grid_point_pinned="EWX/156,91",
        ercot_bucket=TokenBucket(capacity=100, refill_per_s=100),
        trace=FakeTrace(),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


async def test_products_list_is_honoured_np4_745_not_polled_unless_listed() -> None:
    ercot = CountingErcot()
    scheduler = _scheduler_with(ercot, products=("np6-905-cd", "np4-188-cd"))
    await scheduler.run_cycle(now=T0)
    assert sorted(ercot.calls) == ["np4-188-cd", "np6-905-cd"]


async def test_disabled_flag_still_removes_a_listed_product() -> None:
    ercot = CountingErcot()
    scheduler = _scheduler_with(
        ercot, products=("np6-905-cd", "np4-745-cd"), disabled_products=frozenset({"np4-745-cd"})
    )
    await scheduler.run_cycle(now=T0)
    assert ercot.calls == ["np6-905-cd"]


def test_unknown_product_in_list_is_rejected() -> None:
    with pytest.raises(ValueError, match="np9-999"):
        _scheduler_with(CountingErcot(), products=("np6-905-cd", "np9-999"))


def test_configured_ercot_products_from_config() -> None:
    assert configured_ercot_products({}) is None
    assert configured_ercot_products({"products": ["np6-905-cd"]}) == ("np6-905-cd",)
    with pytest.raises(ValueError):
        configured_ercot_products({"products": "np6-905-cd"})


def test_next_poll_delay_backoff_is_capped_and_never_beyond_cadence() -> None:
    day = POLL_INTERVAL_S["np4-188-cd"]
    assert next_poll_delay_s("np4-188-cd", 0) == day
    assert [next_poll_delay_s("np4-188-cd", n) / 60 for n in range(1, 7)] == [5, 10, 20, 40, 60, 60]
    assert next_poll_delay_s("np6-905-cd", 3) == POLL_INTERVAL_S["np6-905-cd"]  # 5-min product: unchanged


async def test_failed_daily_poll_retries_in_minutes_not_a_day() -> None:
    ercot = CountingErcot(failing={"np4-188-cd"})
    scheduler = _scheduler_with(ercot, products=("np4-188-cd",))
    await scheduler.run_cycle(now=T0)
    state = scheduler._product_state["np4-188-cd"]
    assert state.next_poll_at == T0 + timedelta(minutes=5)

    await scheduler.run_cycle(now=T0 + timedelta(minutes=4))  # not yet due
    assert ercot.calls == ["np4-188-cd"]
    await scheduler.run_cycle(now=T0 + timedelta(minutes=5))  # second failure -> 10 min
    assert state.next_poll_at == T0 + timedelta(minutes=15)

    ercot.failing.clear()
    await scheduler.run_cycle(now=T0 + timedelta(minutes=15))  # recovers -> back to the daily cadence
    assert state.consecutive_failures == 0
    assert state.next_poll_at == T0 + timedelta(minutes=15) + timedelta(days=1)


async def test_breaker_skipped_poll_counts_as_a_miss_and_retries_early() -> None:
    ercot = CountingErcot()
    scheduler = _scheduler_with(ercot, products=("np4-188-cd",))
    scheduler._ercot_breaker.allow_request = lambda *, now: False  # type: ignore[method-assign]
    await scheduler.run_cycle(now=T0)
    assert ercot.calls == []
    assert scheduler._product_state["np4-188-cd"].next_poll_at == T0 + timedelta(minutes=5)
