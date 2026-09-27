"""Object storage for raw waveform captures (07-delivery/06 S6.4a, S6.5) -- "a blob
reference, not a DB blob" (`0011_asset_health.sql`'s `og.pq_waveform_raw_index` comment).

`FileBlobStore` is the dev/test-workspace backend (local disk under a configured
directory). A production S3/GCS-backed implementation of the same `BlobStore` protocol
can be substituted later without touching any `opengrid.pq_ingest` ingest/analysis logic
(BUILD.md S5a "pure logic separated from I/O").
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path
from typing import Protocol


class BlobStore(Protocol):
    async def put(self, data: bytes, *, hub_id: str, ts: datetime) -> str:
        """Stores `data`, returning an opaque reference `get()` can resolve back to it."""
        ...

    async def get(self, blob_ref: str) -> bytes: ...


class FileBlobStore:
    """Local-disk `BlobStore`: one file per capture, under `root_dir/<date>/<hub_id>/`.

    R3.4.1 PROD-IO fix: `put`'s `mkdir`/`write_bytes` used to run directly on the calling
    coroutine -- individually small (~1.3-5 KB compressed) as this module's docstring says, but
    `ingest_raw_capture` (the MQTT ingest path) calls this inline on the event loop, so it still
    adds a real synchronous disk-write stall per capture on a busy/contended disk (the same class
    of loop-blocking bug PROD-IO found in `pq_ingest.run_characterization_pass`). `to_thread` costs
    a thread hop either way, so there is no meaningful downside to always paying it here.
    """

    def __init__(self, root_dir: str) -> None:
        self._root = Path(root_dir)

    def _path_for(self, hub_id: str, ts: datetime, ref: str) -> Path:
        day = ts.strftime("%Y-%m-%d")
        return self._root / day / hub_id / f"{ref}.bin"

    async def put(self, data: bytes, *, hub_id: str, ts: datetime) -> str:
        ref = f"{int(ts.timestamp() * 1000)}-{os.urandom(4).hex()}"
        path = self._path_for(hub_id, ts, ref)

        def _write() -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)

        await asyncio.to_thread(_write)
        return str(path.relative_to(self._root).as_posix())

    async def get(self, blob_ref: str) -> bytes:
        path = self._root / blob_ref
        return await asyncio.to_thread(path.read_bytes)
