"""Bounded background worker for slow ingest side effects: regression for the live 2026-09-26 finding
that raw waveform captures (blob write + synchronous index commit, ~7/s) handled inline on the MQTT
ingest loop delayed every hub's telemetry by minutes."""

from __future__ import annotations

import asyncio

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
