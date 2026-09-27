"""Telemetry ingest decoupling step 1 (`engine.telemetry_ingest`): parse/validate off the loop into a bounded
latest-value-per-hub buffer, applied on the loop every ~100 ms."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from opengrid.engine.telemetry_ingest import TelemetryDecoupler

T0 = datetime(2026, 9, 27, 5, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Tel:
    hub_id: str
    ts: datetime
    p_kw: float


def _parse(payload: dict) -> _Tel:
    if "hub_id" not in payload:
        raise ValueError("invalid")
    return _Tel(payload["hub_id"], datetime.fromisoformat(payload["ts"]), float(payload["p_kw"]))


def _raw(hub: str, dt_s: float, p_kw: float) -> bytes:
    return json.dumps(
        {"hub_id": hub, "ts": (T0 + timedelta(seconds=dt_s)).isoformat(), "p_kw": p_kw}
    ).encode()


class _Twin:
    def __init__(self) -> None:
        self.state: dict[str, _Tel] = {}
        self.applies = 0

    async def apply(self, tel: _Tel) -> None:
        self.state[tel.hub_id] = tel
        self.applies += 1


def _decoupler(twin: _Twin, **kw: object) -> TelemetryDecoupler:
    return TelemetryDecoupler(_parse, twin.apply, **kw)  # type: ignore[arg-type]


def test_newest_wins_whatever_the_arrival_order() -> None:
    twin = _Twin()
    d = _decoupler(twin)
    d.parse_one(_raw("h1", 2, -200.0))
    d.parse_one(_raw("h1", 1, -100.0))  # older, arrived late: dropped
    d.parse_one(_raw("h1", 3, -300.0))  # newer: supersedes the unapplied one
    asyncio.run(d.apply_once())
    assert twin.state["h1"].p_kw == -300.0 and twin.applies == 1
    assert d.stats.stale == 1 and d.stats.superseded == 1
    # An older sample after the newer one was applied is stale too (never rolls the twin back).
    d.parse_one(_raw("h1", 2.5, -250.0))
    asyncio.run(d.apply_once())
    assert twin.state["h1"].p_kw == -300.0 and d.stats.stale == 2


def test_the_backlog_stays_bounded_under_load() -> None:
    twin = _Twin()
    d = _decoupler(twin, raw_max=1_000, max_hubs=150)
    for i in range(50_000):  # parser not running: a stalled consumer
        d.submit(_raw(f"h{i % 200}", i * 0.01, float(i)))
    raw, parsed = d.backlog()
    assert raw == 1_000  # the deque never grows past its bound
    assert d.stats.raw_dropped == 49_000 and d.stats.received == 50_000
    d.start()
    try:
        deadline = time.monotonic() + 10
        while d.backlog()[0] and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        d.stop()
    raw, parsed = d.backlog()
    assert raw == 0 and parsed <= 150  # at most one entry per hub, capped
    assert d.stats.hub_cap_dropped > 0
    applied = asyncio.run(d.apply_once())
    assert applied == parsed and d.backlog() == (0, 0)


def test_the_oldest_raw_message_is_the_one_dropped() -> None:
    twin = _Twin()
    d = _decoupler(twin, raw_max=3)
    for k in range(5):
        d.submit(_raw("h1", k, float(k)))
    d.start()
    try:
        deadline = time.monotonic() + 5
        while d.backlog()[0] and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        d.stop()
    asyncio.run(d.apply_once())
    assert twin.state["h1"].p_kw == 4.0  # the newest survived


def test_parsing_runs_on_the_parser_thread_not_the_loop() -> None:
    import threading

    seen: list[str] = []

    def _parse_here(payload: dict) -> _Tel:
        seen.append(threading.current_thread().name)
        return _parse(payload)

    twin = _Twin()
    d = TelemetryDecoupler(_parse_here, twin.apply)
    d.start()
    try:
        d.submit(_raw("h1", 1, -1.0))
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        d.stop()
    assert seen == ["og-telemetry-parse"]


def test_invalid_payloads_are_counted_and_skipped() -> None:
    twin = _Twin()
    d = _decoupler(twin)
    d.parse_one(b"{not json")
    d.parse_one(json.dumps({"ts": T0.isoformat()}).encode())
    d.parse_one(_raw("h2", 1, -5.0))
    asyncio.run(d.apply_once())
    assert d.stats.invalid == 2 and twin.state["h2"].p_kw == -5.0


def test_the_applier_observes_ingest_lag_and_survives_a_failed_apply() -> None:
    observed: list[object] = []

    async def _apply(tel: _Tel) -> None:
        if tel.hub_id == "bad":
            raise RuntimeError("boom")

    d = TelemetryDecoupler(_parse, _apply, observe=observed.append)
    d.parse_one(_raw("bad", 1, 0.0))
    d.parse_one(_raw("ok", 1, 0.0))
    assert asyncio.run(d.apply_once()) == 2
    assert d.stats.apply_failed == 1 and d.stats.applied == 1 and len(observed) == 2


async def test_run_applier_applies_about_every_interval() -> None:
    twin = _Twin()
    d = _decoupler(twin, apply_interval_s=0.05)
    task = asyncio.create_task(d.run_applier())
    try:
        d.parse_one(_raw("h1", 1, -1.0))
        await asyncio.sleep(0.2)
        assert twin.state["h1"].p_kw == -1.0
    finally:
        task.cancel()


def test_the_switch_turns_the_inline_path_back_on() -> None:
    from opengrid.engine import build_telemetry_decoupler
    from opengrid.platform.config import Config

    assert build_telemetry_decoupler(Config({"ingest": {"telemetry_decoupled": False}})) is None
    d = build_telemetry_decoupler(Config({}))
    try:
        assert d is not None and d.apply_interval_s == pytest.approx(0.1)
    finally:
        assert d is not None
        d.stop()
