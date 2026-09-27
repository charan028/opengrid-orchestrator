"""opengrid.pq_ingest.blob_store.FileBlobStore: local-disk object storage round trip."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from opengrid.pq_ingest.blob_store import FileBlobStore

pytestmark = pytest.mark.asyncio


async def test_put_then_get_round_trip(tmp_path) -> None:
    store = FileBlobStore(str(tmp_path))
    ts = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    ref = await store.put(b"hello waveform", hub_id="hub-00000", ts=ts)
    assert await store.get(ref) == b"hello waveform"


async def test_put_partitions_by_date_and_hub(tmp_path) -> None:
    store = FileBlobStore(str(tmp_path))
    ts = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    ref = await store.put(b"data", hub_id="hub-00042", ts=ts)
    assert ref.startswith("2026-09-26/hub-00042/")


async def test_two_puts_get_distinct_refs(tmp_path) -> None:
    store = FileBlobStore(str(tmp_path))
    ts = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    ref1 = await store.put(b"a", hub_id="hub-00000", ts=ts)
    ref2 = await store.put(b"b", hub_id="hub-00000", ts=ts)
    assert ref1 != ref2
    assert await store.get(ref1) == b"a"
    assert await store.get(ref2) == b"b"


async def test_put_throttles_concurrent_writes_to_the_configured_bound(tmp_path, monkeypatch) -> None:
    """R3.4.3 (L-8): a slow disk must apply backpressure to the caller (bounded concurrency), not let an
    unlimited number of writes pile up. Simulates a slow disk by delaying `Path.write_bytes` and proving
    at most `max_concurrent_writes` are ever in flight at once."""
    store = FileBlobStore(str(tmp_path), max_concurrent_writes=2)
    in_flight = 0
    max_in_flight = 0
    real_write_bytes = Path.write_bytes

    def _slow_write_bytes(self: Path, data: bytes):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        time.sleep(0.05)
        result = real_write_bytes(self, data)
        in_flight -= 1
        return result

    monkeypatch.setattr(Path, "write_bytes", _slow_write_bytes)
    ts = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)

    await asyncio.gather(*(store.put(f"payload-{i}".encode(), hub_id="hub-00000", ts=ts) for i in range(6)))

    assert max_in_flight == 2
