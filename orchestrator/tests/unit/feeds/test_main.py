"""`feeds/main.py`'s forecast-recompute hook: self-timed 15-minute cadence riding on feeds' own 5s
`run_forever` tick, folded in via `extra_tick` instead of a second `run_forever` loop (see
`opengrid.feeds.run_feeds_process`'s docstring for why two loops in one process drop SIGTERM)."""

from __future__ import annotations

from datetime import datetime

import pytest

import opengrid.feeds.main as feeds_main


async def test_forecast_tick_runs_immediately_then_waits_for_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = {"t": 1000.0}
    monkeypatch.setattr(feeds_main.time, "monotonic", lambda: clock["t"])

    calls = {"n": 0}

    async def fake_run_forecast_cycle(now: datetime | None = None) -> list[object]:
        calls["n"] += 1
        return []

    monkeypatch.setattr(feeds_main.forecast, "run_forecast_cycle", fake_run_forecast_cycle)

    tick = feeds_main._forecast_tick()

    await tick()  # due immediately on the first call
    assert calls["n"] == 1

    clock["t"] += 60  # well under FORECAST_RECOMPUTE_INTERVAL_S
    await tick()
    assert calls["n"] == 1  # not due yet

    clock["t"] += feeds_main.FORECAST_RECOMPUTE_INTERVAL_S
    await tick()
    assert calls["n"] == 2  # cadence elapsed
