"""opengrid.pq_ingest.blob_store.FileBlobStore: local-disk object storage round trip."""

from __future__ import annotations

from datetime import UTC, datetime

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
