"""opengrid.pq_ingest's module-level facade: configure/ingest_summary/ingest_raw_capture
against fake backend/blob-store implementations (no DB, no filesystem)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

import opengrid.pq_ingest as pq_ingest
from opengrid.core.models.pq import PqWaveformRawIndex, PqWaveformSummaryRow

pytestmark = pytest.mark.asyncio


class FakeBackend:
    def __init__(self) -> None:
        self.summaries: list[PqWaveformSummaryRow] = []
        self.raw_indices: list[PqWaveformRawIndex] = []
        self.batch_calls: list[int] = []
        self.fail_batches = False
        self.characterization_batches: list[int] = []
        self.hub_inverter_pq: dict[str, object] = {}
        self.fail_characterization = False
        self.aggregate_delay_s = 0.0

    async def insert_summary(self, row: PqWaveformSummaryRow) -> None:
        self.summaries.append(row)

    async def insert_summaries_batch(self, rows) -> None:
        if self.fail_batches:
            raise RuntimeError("simulated backend failure")
        self.batch_calls.append(len(rows))
        self.summaries.extend(rows)

    async def insert_raw_index(self, row: PqWaveformRawIndex) -> None:
        self.raw_indices.append(row)

    async def latest_summaries(self, hub_ids, *, since):
        return [s for s in self.summaries if s.hub_id in hub_ids and s.ts >= since]

    async def latest_summary_aggregates(self, hub_ids, *, since):
        """R3.4.1 PROD-IO fix: the fake's counterpart to `pg_backend`'s SQL aggregation -- reuses the
        same Python reference (`characterize._aggregate_hub`) the SQL is proven equivalent to
        (`tests/unit/pq_ingest/test_characterize.py`), so this fake stays a faithful stand-in for the
        real backend without re-deriving the aggregation math a second time here."""
        from opengrid.pq_ingest.characterize import _aggregate_hub

        if self.aggregate_delay_s:
            await asyncio.sleep(self.aggregate_delay_s)
        raw = await self.latest_summaries(hub_ids, since=since)
        by_hub: dict[str, list[PqWaveformSummaryRow]] = {}
        for row in raw:
            by_hub.setdefault(row.hub_id, []).append(row)
        aggregates = (_aggregate_hub(hub_id, rows) for hub_id, rows in by_hub.items())
        return [a for a in aggregates if a is not None]

    async def upsert_hub_inverter_pq_batch(self, rows) -> None:
        if self.fail_characterization:
            raise RuntimeError("simulated characterization backend failure")
        self.characterization_batches.append(len(rows))
        for row in rows:
            self.hub_inverter_pq[row.hub_id] = row


class FakeBlobStore:
    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}
        self._n = 0

    async def put(self, data: bytes, *, hub_id: str, ts) -> str:
        self._n += 1
        ref = f"{hub_id}-{self._n}"
        self.blobs[ref] = data
        return ref

    async def get(self, blob_ref: str) -> bytes:
        return self.blobs[blob_ref]


@pytest.fixture(autouse=True)
def _configure():
    backend = FakeBackend()
    blob_store = FakeBlobStore()
    pq_ingest.configure(backend, blob_store)
    yield backend, blob_store
    pq_ingest.configure(None, None)  # type: ignore[arg-type]


_SUMMARY_PAYLOAD = {
    "hub_id": "hub-00000",
    "bank_id": "bank-000",
    "zone": "LZ_NORTH",
    "ts": "2026-09-26T00:00:00.000Z",
    "v_rms_a": 240.0,
    "i_rms_a": 10.0,
    "freq_hz": 60.0,
    "sync_source": "ptp",
    "sync_quality_ns": 50.0,
}

_RAW_PAYLOAD = {
    "hub_id": "hub-00000",
    "phase_connection": "A",
    "ts": "2026-09-26T00:00:00.000Z",
    "sample_rate_hz": 7680,
    "cycles": 10,
    "channels": ["V", "I"],
    "sync_source": "ptp",
    "sync_quality_ns": 50.0,
    "compression": "none",
    "trigger_reason": "API_REQUEST",
    "samples": {"V": [1, 2, 3], "I": [4, 5, 6]},
}


async def test_ingest_summary_buffers_without_inserting(_configure) -> None:
    """S9 wave-2 fix: `ingest_summary` buffers -- it no longer inserts per message."""
    backend, _ = _configure
    row = await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)
    assert row.hub_id == "hub-00000"
    assert backend.summaries == []
    assert pq_ingest.pending_summary_count() == 1


async def test_flush_summaries_persists_buffered_rows_in_one_batch(_configure) -> None:
    backend, _ = _configure
    row = await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)
    flushed = await pq_ingest.flush_summaries()
    assert flushed == 1
    assert backend.summaries == [row]
    assert backend.batch_calls == [1]
    assert pq_ingest.pending_summary_count() == 0


async def test_flush_summaries_on_empty_buffer_is_a_noop(_configure) -> None:
    backend, _ = _configure
    assert await pq_ingest.flush_summaries() == 0
    assert backend.batch_calls == []


async def test_ingest_summary_auto_flushes_at_batch_size_threshold() -> None:
    backend = FakeBackend()
    blob_store = FakeBlobStore()
    pq_ingest.configure(backend, blob_store, flush_batch_size=2)
    try:
        await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)
        assert pq_ingest.pending_summary_count() == 1
        await pq_ingest.ingest_summary({**_SUMMARY_PAYLOAD, "ts": "2026-09-26T00:00:02.000Z"})
        # The second message crossed the threshold -- an auto-flush already ran.
        assert pq_ingest.pending_summary_count() == 0
        assert backend.batch_calls == [2]
    finally:
        pq_ingest.configure(None, None)  # type: ignore[arg-type]


async def test_ingest_summary_drops_oldest_when_buffer_full() -> None:
    backend = FakeBackend()
    blob_store = FakeBlobStore()
    # A huge flush_batch_size so the buffer fills up without auto-flushing.
    pq_ingest.configure(backend, blob_store, summary_buffer_max=2, flush_batch_size=1000)
    try:
        await pq_ingest.ingest_summary({**_SUMMARY_PAYLOAD, "hub_id": "hub-00001"})
        await pq_ingest.ingest_summary({**_SUMMARY_PAYLOAD, "hub_id": "hub-00002"})
        await pq_ingest.ingest_summary({**_SUMMARY_PAYLOAD, "hub_id": "hub-00003"})
        assert pq_ingest.pending_summary_count() == 2
        await pq_ingest.flush_summaries()
        assert [row.hub_id for row in backend.summaries] == ["hub-00002", "hub-00003"]
    finally:
        pq_ingest.configure(None, None)  # type: ignore[arg-type]


async def test_flush_summaries_requeues_on_backend_failure(_configure) -> None:
    backend, _ = _configure
    backend.fail_batches = True
    await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)
    with pytest.raises(RuntimeError, match="simulated backend failure"):
        await pq_ingest.flush_summaries()
    assert pq_ingest.pending_summary_count() == 1
    backend.fail_batches = False
    assert await pq_ingest.flush_summaries() == 1


async def test_ingest_summary_ignores_wire_only_routing_fields(_configure) -> None:
    """bank_id/zone are wire routing fields, not part of the row model (extra='forbid')
    -- ingest_summary must not choke on their presence."""
    row = await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)
    assert not hasattr(row, "bank_id")
    assert not hasattr(row, "zone")


async def test_ingest_raw_capture_stores_blob_and_index(_configure) -> None:
    backend, blob_store = _configure
    row = await pq_ingest.ingest_raw_capture(_RAW_PAYLOAD)
    assert row.hub_id == "hub-00000"
    assert row.channels == 2
    assert backend.raw_indices == [row]
    stored = await blob_store.get(row.blob_ref)
    assert pq_ingest.decode_stored_raw_capture(["V", "I"], stored) == {"V": [1, 2, 3], "I": [4, 5, 6]}


async def test_ingest_raw_capture_resolves_pending_request(_configure) -> None:
    now = datetime(2026, 9, 26, tzinfo=UTC)
    request = pq_ingest.build_capture_request("hub-00000", "API_REQUEST", now=now)
    pq_ingest.track_capture_request(request)
    await pq_ingest.ingest_raw_capture(_RAW_PAYLOAD)
    # Resolved already -- a second raw capture for the same hub has nothing left to resolve.
    assert pq_ingest.expire_stale_capture_requests(now) == []


async def test_functions_raise_before_configure() -> None:
    pq_ingest.configure(None, None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="configure"):
        await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)


async def test_latest_summaries_reads_through_backend(_configure) -> None:
    await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)
    await pq_ingest.flush_summaries()
    rows = await pq_ingest.latest_summaries(["hub-00000"], since=datetime(2020, 1, 1, tzinfo=UTC))
    assert len(rows) == 1
    assert rows[0].hub_id == "hub-00000"


# ---------------------------------------------------------------------------
# run_characterization_pass (blocker fix: og.hub_inverter_pq had 0 live rows)
# ---------------------------------------------------------------------------


async def test_run_characterization_pass_upserts_hubs_with_enough_data(_configure) -> None:
    backend, _ = _configure
    now = datetime(2026, 9, 26, 0, 5, 0, tzinfo=UTC)  # within the default 15-min window of ts
    await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)
    await pq_ingest.flush_summaries()

    characterized = await pq_ingest.run_characterization_pass(["hub-00000"], now=now)

    assert characterized == 1
    assert backend.characterization_batches == [1]  # one batched call, not one per hub
    assert "hub-00000" in backend.hub_inverter_pq


async def test_run_characterization_pass_is_one_batched_read_and_write_for_many_hubs(
    _configure,
) -> None:
    backend, _ = _configure
    now = datetime(2026, 9, 26, 0, 5, 0, tzinfo=UTC)
    hub_ids = [f"hub-{i:05d}" for i in range(50)]
    for hub_id in hub_ids:
        await pq_ingest.ingest_summary({**_SUMMARY_PAYLOAD, "hub_id": hub_id})
    await pq_ingest.flush_summaries()

    characterized = await pq_ingest.run_characterization_pass(hub_ids, now=now)

    assert characterized == 50
    assert backend.characterization_batches == [50]  # one batch call for all 50 hubs


async def test_run_characterization_pass_skips_hubs_without_enough_data(_configure) -> None:
    backend, _ = _configure
    now = datetime(2026, 9, 27, tzinfo=UTC)

    characterized = await pq_ingest.run_characterization_pass(["hub-nonexistent"], now=now)

    assert characterized == 0
    assert backend.characterization_batches == []


async def test_run_characterization_pass_propagates_and_counts_backend_failure(_configure) -> None:
    backend, _ = _configure
    backend.fail_characterization = True
    now = datetime(2026, 9, 26, 0, 5, 0, tzinfo=UTC)
    await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)
    await pq_ingest.flush_summaries()

    with pytest.raises(RuntimeError, match="simulated characterization backend failure"):
        await pq_ingest.run_characterization_pass(["hub-00000"], now=now)


async def test_run_characterization_pass_times_out_instead_of_hanging(_configure) -> None:
    """R3.4.1 PROD-IO fix: a slow/hung fetch+characterize must raise `TimeoutError` (traced as the
    "timeout" outcome, `metrics.characterization_passes_total`) rather than block indefinitely."""
    backend, _ = _configure
    backend.aggregate_delay_s = 0.2
    now = datetime(2026, 9, 26, 0, 5, 0, tzinfo=UTC)
    await pq_ingest.ingest_summary(_SUMMARY_PAYLOAD)
    await pq_ingest.flush_summaries()

    with pytest.raises(TimeoutError):
        await pq_ingest.run_characterization_pass(["hub-00000"], now=now, timeout_s=0.02)


async def test_characterization_pass_keeps_event_loop_lag_under_100ms_at_3500_hubs(_configure) -> None:
    """R3.4.1 PROD-IO fix's own acceptance test: production saw 4-5s event-loop freezes every
    characterization pass at ~3,509 hubs. Feeds a fixture at that same scale straight into the fake
    backend's raw-row store (bypassing `ingest_summary`'s one-message-at-a-time buffering, which isn't
    what's under test here) and runs a real `run_characterization_pass` concurrently with a lag probe
    that samples how late its own `asyncio.sleep` wakes up -- the signal the offloaded
    `characterize_fleet_from_aggregates` thread (`asyncio.to_thread`) must not block."""
    backend, _ = _configure
    hub_count = 3_500
    now = datetime(2026, 9, 26, 0, 5, 0, tzinfo=UTC)
    hub_ids = [f"hub-{i:05d}" for i in range(hub_count)]
    for hub_id in hub_ids:
        for sample in range(5):
            backend.summaries.append(
                PqWaveformSummaryRow(
                    hub_id=hub_id,
                    ts=datetime(2026, 9, 26, 0, sample, 0, tzinfo=UTC),
                    v_rms_a=240.0 + sample * 0.1,
                    i_rms_a=10.0,
                    freq_hz=60.0 + sample * 0.001,
                    sync_source="ptp",
                    sync_quality_ns=50.0,
                )
            )

    max_lag_s = 0.0
    probe_interval_s = 0.005
    stop = asyncio.Event()

    async def _lag_probe() -> None:
        nonlocal max_lag_s
        loop = asyncio.get_running_loop()
        last = loop.time()
        while not stop.is_set():
            await asyncio.sleep(probe_interval_s)
            now_t = loop.time()
            max_lag_s = max(max_lag_s, (now_t - last) - probe_interval_s)
            last = now_t

    probe_task = asyncio.create_task(_lag_probe())
    try:
        characterized = await pq_ingest.run_characterization_pass(hub_ids, now=now)
    finally:
        stop.set()
        await probe_task

    assert characterized == hub_count
    assert max_lag_s < 0.1, f"event loop lag {max_lag_s * 1000:.1f}ms exceeded the 100ms budget"
