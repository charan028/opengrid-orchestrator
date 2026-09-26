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


async def test_characterization_pass_covers_every_hub_in_the_twin(monkeypatch) -> None:
    import opengrid.fleet as fleet
    import opengrid.pq_ingest as pq_ingest
    from opengrid.engine import characterize_hubs

    seen: list[list[str]] = []

    async def _pass(hub_ids, **kwargs):
        seen.append(list(hub_ids))
        return len(hub_ids)

    monkeypatch.setattr(fleet, "known_hub_ids", lambda: ["hub-1", "hub-2"])
    monkeypatch.setattr(pq_ingest, "run_characterization_pass", _pass)

    assert await characterize_hubs() == 2
    assert seen == [["hub-1", "hub-2"]]


async def test_waveform_summaries_are_ingested_off_the_mqtt_loop(monkeypatch) -> None:
    """Live 2026-09-26 06:04-06:09: pq_ingest.ingest_summary's size-triggered flush ran on the MQTT ingest
    loop and, during a host disk stall, held telemetry until hubs aged. The summary path is now a
    background worker; an invalid summary is dropped there."""
    import opengrid.platform.mqtt as mqtt
    import opengrid.pq_ingest as pq_ingest
    from opengrid.engine import SUMMARY_QUEUE_MAX, ingest_summary_off_loop

    ingested: list[dict] = []

    async def _ingest(payload):
        ingested.append(payload)

    monkeypatch.setattr(pq_ingest, "ingest_summary", _ingest)
    await ingest_summary_off_loop({"hub_id": "hub-1"})  # invalid -> dropped
    assert ingested == []

    monkeypatch.setattr(mqtt, "validate_payload", lambda kind, payload: None)
    worker = BackgroundIngest("pq-summary", ingest_summary_off_loop, queue_max=SUMMARY_QUEUE_MAX)
    task = asyncio.create_task(worker.run())
    assert worker.submit({"hub_id": "hub-1", "ok": True})
    await worker.drain()
    task.cancel()
    assert ingested == [{"hub_id": "hub-1", "ok": True}]


async def test_calibration_ack_handler_validates_then_hands_off_to_assets(monkeypatch) -> None:
    """S6.7 calibration loop, engine side: ack/cal/<hub> is validated against calibration_ack.schema.json
    and handed to opengrid.assets.calibration_ack; an invalid ack never reaches the asset state."""
    import opengrid.assets.calibration_ack as calibration_ack
    import opengrid.platform.mqtt as mqtt
    from opengrid.engine import make_calibration_ack_handler

    handled: list[tuple[object, dict]] = []

    async def _handle(service, payload):
        handled.append((service, payload))

    monkeypatch.setattr(calibration_ack, "handle_calibration_ack", _handle)
    service = object()
    handler = make_calibration_ack_handler(service)

    await handler({"hub_id": "hub-1"})  # missing required fields -> dropped
    assert handled == []

    monkeypatch.setattr(mqtt, "validate_payload", lambda kind, payload: None)
    good = {"hub_id": "hub-1", "calibration_id": "c1"}
    await handler(good)
    assert handled == [(service, good)]


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


async def test_bank_batches_are_proposed_concurrently_with_a_bound_and_a_failure_is_isolated() -> None:
    """A11: at 07:00 ERCOT_AS spreads over up to 24 banks; proposing each bank's batch (K10 trace
    pre-image, batch row, NOTIFY -- three synchronous commits) one bank after another would put 24 x 3
    commits on the tick. Banks are independent trace streams, so their batches go out concurrently; the
    per-bank order (pre-image first) is unchanged, and one bank's failure no longer skips the rest."""
    from opengrid.engine import propose_all_banks

    in_flight = 0
    peak = 0
    done: list[str] = []

    async def _propose(bank_id: str, grants: list[str]) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        if bank_id == "bank-3":
            raise RuntimeError("insert failed")
        done.append(bank_id)

    by_bank = {f"bank-{i}": [f"g{i}"] for i in range(10)}
    failed = await propose_all_banks(by_bank, _propose, concurrency=4)

    assert peak == 4
    assert sorted(done) == sorted(b for b in by_bank if b != "bank-3")
    assert failed == ["bank-3"]


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
