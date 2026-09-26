"""Bounded background worker for slow ingest side effects: regression for the live 2026-09-26 finding
that raw waveform captures (blob write + synchronous index commit, ~7/s) handled inline on the MQTT
ingest loop delayed every hub's telemetry by minutes."""

from __future__ import annotations

import asyncio
import contextlib

from opengrid.engine.background import BackgroundIngest


async def test_submit_never_blocks_and_the_worker_handles_in_order() -> None:
    handled: list[int] = []
    gate = asyncio.Event()

    async def _slow(payload):
        await gate.wait()
        handled.append(payload["n"])

    worker = BackgroundIngest("t", _slow, queue_max=10)
    task = asyncio.create_task(worker.run())
    assert all(worker.submit({"n": n}) for n in range(3))  # returns immediately while the handler waits
    gate.set()
    await worker.drain()
    task.cancel()

    assert handled == [0, 1, 2]


async def test_a_full_queue_drops_and_counts_and_a_failing_handler_does_not_stop_the_worker() -> None:
    seen: list[int] = []

    async def _flaky(payload):
        if payload["n"] == 0:
            raise RuntimeError("disk full")
        seen.append(payload["n"])

    worker = BackgroundIngest("t", _flaky, queue_max=2)
    assert worker.submit({"n": 0}) and worker.submit({"n": 1})
    assert worker.submit({"n": 2}) is False
    assert worker.dropped == 1

    task = asyncio.create_task(worker.run())
    await worker.drain()
    task.cancel()
    assert seen == [1]


async def test_raw_capture_handler_validates_off_loop_and_drops_invalid(monkeypatch) -> None:
    import opengrid.pq_ingest as pq_ingest
    from opengrid.engine import ingest_raw_capture_off_loop

    ingested: list[dict] = []

    async def _ingest(payload):
        ingested.append(payload)

    monkeypatch.setattr(pq_ingest, "ingest_raw_capture", _ingest)

    await ingest_raw_capture_off_loop({"hub_id": "hub-1"})  # missing required fields -> dropped

    assert ingested == []


async def test_periodic_job_repeats_on_its_interval_and_survives_a_failure() -> None:
    """A11 regression (live 2026-09-26): fleet persistence (telemetry COPY + 2,000-row hub_state upsert)
    ran inside the 2 s dispatch tick and was its p99 (~700 ms of ~1.1 s). It now runs as its own periodic
    task; a failed run is logged and the next one still happens."""
    from opengrid.engine.background import run_periodic

    runs: list[int] = []
    sleeps: list[float] = []

    async def _job() -> None:
        runs.append(len(runs))
        if len(runs) == 1:
            raise RuntimeError("db down")

    async def _sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 3:
            raise asyncio.CancelledError

    clock = iter([0.0, 0.5, 2.0, 2.25, 4.0, 6.5])
    with contextlib.suppress(asyncio.CancelledError):
        await run_periodic("t", 2.0, _job, clock=lambda: next(clock), sleep=_sleep)

    assert runs == [0, 1, 2]
    assert sleeps == [1.5, 1.75, 0.0]  # interval minus the run's own duration, never negative


async def test_persist_fleet_state_flushes_the_twin_and_pq_summaries(monkeypatch) -> None:
    import types

    import opengrid.engine as engine
    import opengrid.fleet as fleet
    from opengrid.platform.process import Cadence

    calls: list[str] = []

    async def _fleet_flush(*, now=None):
        calls.append("fleet")

    async def _pq(state):
        calls.append("pq")

    monkeypatch.setattr(fleet, "flush", _fleet_flush)
    monkeypatch.setattr(engine, "_flush_pq_summaries", _pq)

    await engine.persist_fleet_state(types.SimpleNamespace(pq_flush=Cadence(2.0)))

    assert calls == ["fleet", "pq"]


async def test_a_failed_fleet_flush_still_flushes_pq_summaries(monkeypatch) -> None:
    import types

    import opengrid.engine as engine
    import opengrid.fleet as fleet

    calls: list[str] = []

    async def _fleet_flush(*, now=None):
        raise RuntimeError("db down")

    async def _pq(state):
        calls.append("pq")

    monkeypatch.setattr(fleet, "flush", _fleet_flush)
    monkeypatch.setattr(engine, "_flush_pq_summaries", _pq)

    await engine.persist_fleet_state(types.SimpleNamespace(pq_flush=None))

    assert calls == ["pq"]


def test_the_dispatch_tick_no_longer_persists_the_fleet_twin_or_writes_the_heartbeat() -> None:
    import inspect

    import opengrid.engine as engine

    source = inspect.getsource(engine._engine_tick)
    assert "fleet.flush(" not in source
    assert "_flush_pq_summaries(" not in source
    assert "write_heartbeat(" not in source


async def test_heartbeat_is_written_only_while_the_tick_keeps_completing(monkeypatch) -> None:
    """Live 2026-09-26 04:30: a host disk stall made each synchronous heartbeat commit take up to 10 s, and
    because the heartbeat was the tick's first await, dispatch waited on it. The heartbeat is now its own
    task, and it still stops when the tick stops completing, so a hung tick still reads as engine down."""
    import types

    import opengrid.engine as engine

    beats: list[str] = []

    async def _write(pool, name):
        beats.append(name)

    monkeypatch.setattr(engine, "write_heartbeat", _write)
    state = types.SimpleNamespace(
        heartbeat_pool=object(), last_tick_at=None, cycle_interval_s=2.0, heartbeat_interval_s=5.0
    )

    assert await engine.beat_if_ticking(state, monotonic_now=100.0) is False  # no tick yet
    state.last_tick_at = 98.0
    assert await engine.beat_if_ticking(state, monotonic_now=100.0) is True
    assert await engine.beat_if_ticking(state, monotonic_now=104.0) is True  # within 3 cycles (6 s)
    assert await engine.beat_if_ticking(state, monotonic_now=104.5) is False  # tick stuck
    assert beats == ["engine", "engine"]


async def test_timed_tick_records_completion_even_when_the_tick_raises(monkeypatch) -> None:
    import opengrid.engine as engine

    async def _boom(state):
        raise RuntimeError("tick failed")

    monkeypatch.setattr(engine, "_engine_tick", _boom)
    state = engine._EngineState.__new__(engine._EngineState)
    state.latency = engine.CycleLatencyWindow(report_every_s=1e9)
    state.latency.report_due(0.0)
    state.last_tick_at = None
    with contextlib.suppress(RuntimeError):
        await engine.timed_tick(state)
    assert state.last_tick_at is not None
