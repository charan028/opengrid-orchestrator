"""opengrid.pq_ingest -- WP-G: waveform ingest (07-delivery/06 S6.4-S6.5, S9.2 Agent G).

Validates, persists and analyzes the two payload kinds a hub inverter publishes over
the SCADA network (S6.4): the periodic `PqWaveformSummary` (`og.pq_waveform_summary`)
and the triggered-only `PqWaveformRawCaptureHeader` (blob-stored, indexed by
`og.pq_waveform_raw_index`) -- both tables already exist
(`orchestrator/migrations/0011_asset_health.sql`, WP-A), so this package adds no new
migration. Also builds the on-demand `WaveformCaptureRequest` (S6.4b/S6.6) and runs the
S6.5-step-2 background audit job that independently recomputes THD/RMS/frequency from a
raw capture's samples via `opengrid.core.pq` -- never re-deriving any of that math
(BUILD.md S1; `opengrid.core.pq` is the single canonical implementation).

**Wave-2 fix (S9 build report).** `ingest_summary` used to insert every message straight
to Postgres, one `INSERT`/commit per message. At the fleet's real scale (~1,000 msg/s at
2,000 hubs on the old 2 s cadence) that is exactly the pattern that stalled the live SCADA
write path (`opengrid.fleet`/`opengrid.engine`'s own `pg_backend.py`: a WAL fsync takes
~0.5 s on the base server's disk). `ingest_summary` now BUFFERS the validated row in
memory (`_pending_summaries`, capped at `configure(..., summary_buffer_max=...)`, drop-
oldest when full, counted in `opengrid.pq_ingest.metrics`) and `flush_summaries()` drains
the whole buffer as one batched `executemany` (`PqIngestBackend.insert_summaries_batch`,
async commit -- see `pg_backend.py`'s own docstring for why that is safe for this soft-
telemetry data class). A flush also fires opportunistically from `ingest_summary` once the
buffer reaches `flush_batch_size`, so a burst never waits for the timer. Raw captures are
unaffected (S6.4b: triggered-only, ~68 kbps at 10,000 hubs -- never the bottleneck) and
still insert one row per capture via `insert_raw_index`.

Module-level singleton facade, mirroring `opengrid.fleet`/`opengrid.ledger`: call
`configure()` once per process (the engine-owned MQTT ingest loop does this at start-up),
then call the free functions below. This package holds no MQTT client of its own -- the
caller (`opengrid.engine._mqtt_ingest_loop`, engine-owned) subscribes to
`<root>/scada/wave/#`, validates against `interfaces/mqtt/pq_waveform_*.schema.json` via
`opengrid.platform.mqtt.validate_payload`, and hands the parsed payload to
`ingest_summary`/`ingest_raw_capture` below. A second, engine-owned periodic timer must
call `flush_summaries()` (e.g. every `[pq_ingest].flush_interval_s`) so a low-traffic
period still flushes promptly instead of waiting for `flush_batch_size` messages to
accumulate. See this package's README.md for the exact wiring lines.

Never imports `ogsim` (BUILD.md S1: opengrid and ogsim share only `interfaces/`).
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import uuid4

from opengrid.core.models.pq import (
    PqWaveformRawIndex,
    PqWaveformSummaryRow,
    WaveformCaptureRequest,
    WaveformRawCaptureHeader,
)
from opengrid.pq_ingest import metrics
from opengrid.pq_ingest.aggregation import bank_measurement, fresh_summaries
from opengrid.pq_ingest.blob_store import BlobStore
from opengrid.pq_ingest.capture import PendingCaptureTracker, build_capture_request
from opengrid.pq_ingest.raw_codec import decode_raw_samples, encode_raw_samples

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_FLUSH_BATCH_SIZE",
    "DEFAULT_FLUSH_INTERVAL_S",
    "DEFAULT_SUMMARY_BUFFER_MAX",
    "PendingCaptureTracker",
    "PqIngestBackend",
    "bank_measurement",
    "build_capture_request",
    "configure",
    "flush_summaries",
    "fresh_summaries",
    "ingest_raw_capture",
    "ingest_summary",
    "latest_summaries",
    "pending_summary_count",
]

# S9 wave-2 build report's chosen defaults: `flush_interval_s` is the ENGINE's own timer period (not
# read by this module directly -- passed by the caller's own periodic call to `flush_summaries()`),
# documented here so both sides use the same number absent an explicit override. 500 rows/flush
# matches `opengrid.fleet.pg_backend.upsert_hub_states`'s existing chunk size; a 2 s interval halves
# the old per-message-commit rate's worst case while still keeping guardian/allocator staleness well
# inside S5.4's freshness gate (2x the summary publish cadence).
DEFAULT_SUMMARY_BUFFER_MAX = 20_000
DEFAULT_FLUSH_BATCH_SIZE = 500
DEFAULT_FLUSH_INTERVAL_S = 2.0


class PqIngestBackend(Protocol):
    """Storage contract this package needs. `opengrid.pq_ingest.pg_backend.PgPqIngestBackend`
    is the real Postgres-backed implementation; unit tests use an in-memory fake."""

    async def insert_summary(self, row: PqWaveformSummaryRow) -> None: ...

    async def insert_summaries_batch(self, rows: Sequence[PqWaveformSummaryRow]) -> None:
        """S9 wave-2: one batched insert for every buffered summary (`flush_summaries()`'s
        counterpart) -- see `pg_backend.PgPqIngestBackend.insert_summaries_batch`."""
        ...

    async def insert_raw_index(self, row: PqWaveformRawIndex) -> None: ...

    async def latest_summaries(
        self, hub_ids: Sequence[str], *, since: datetime
    ) -> list[PqWaveformSummaryRow]: ...


class _NotConfiguredError(RuntimeError):
    pass


_backend: PqIngestBackend | None = None
_blob_store: BlobStore | None = None
_pending: PendingCaptureTracker = PendingCaptureTracker()
_pending_summaries: deque[PqWaveformSummaryRow] = deque()
_summary_buffer_max: int = DEFAULT_SUMMARY_BUFFER_MAX
_flush_batch_size: int = DEFAULT_FLUSH_BATCH_SIZE


def configure(
    backend: PqIngestBackend,
    blob_store: BlobStore,
    *,
    summary_buffer_max: int = DEFAULT_SUMMARY_BUFFER_MAX,
    flush_batch_size: int = DEFAULT_FLUSH_BATCH_SIZE,
) -> None:
    """Wires this process-wide facade to a real backend/blob store. Called once at
    process start-up (mirrors `opengrid.fleet.configure`/`opengrid.ledger.configure`).
    `summary_buffer_max`/`flush_batch_size` should come from `[pq_ingest]` config keys of
    the same name (see README) -- the defaults here are safe without any config."""
    global _backend, _blob_store, _summary_buffer_max, _flush_batch_size
    _backend = backend
    _blob_store = blob_store
    _summary_buffer_max = summary_buffer_max
    _flush_batch_size = flush_batch_size
    _pending_summaries.clear()


def _require_backend() -> PqIngestBackend:
    if _backend is None:
        raise _NotConfiguredError("opengrid.pq_ingest.configure() must be called before use")
    return _backend


def _require_blob_store() -> BlobStore:
    if _blob_store is None:
        raise _NotConfiguredError("opengrid.pq_ingest.configure() must be called before use")
    return _blob_store


def _row_fields(payload: dict[str, Any], model: type[Any]) -> dict[str, Any]:
    """`PqWaveformSummary`/`PqWaveformRawCaptureHeader` (the WIRE shapes) carry routing
    fields (`bank_id`, `zone`) the ROW model does not persist (the table is keyed by
    `hub_id`/`ts` alone) -- both row models declare `extra=\"forbid\"`, so this narrows
    the payload to the row model's own fields before constructing it, rather than
    loosening that guard."""
    return {k: v for k, v in payload.items() if k in model.model_fields}


async def ingest_summary(payload: dict[str, Any]) -> PqWaveformSummaryRow:
    """Validates one `PqWaveformSummary` message (S6.4a) and BUFFERS it for a batched
    flush (S9 wave-2 fix, this module's own docstring) -- it no longer inserts per
    message. The caller validates against `pq_waveform_summary.schema.json`
    (`opengrid.platform.mqtt.validate_payload`) before calling this -- this function's
    own `PqWaveformSummaryRow.model_validate` is defense in depth (BUILD.md S5a
    "validate all inbound messages"), not a second copy of the wire-shape check.

    Requires `configure()` to have been called (raises before buffering, same as the
    old immediate-insert contract, so a mis-started process fails fast rather than
    silently accumulating an unflushable buffer)."""
    _require_backend()
    row = PqWaveformSummaryRow.model_validate(_row_fields(payload, PqWaveformSummaryRow))
    _buffer_summary(row)
    if len(_pending_summaries) >= _flush_batch_size:
        await flush_summaries()
    return row


def _buffer_summary(row: PqWaveformSummaryRow) -> None:
    """Drop-oldest capped buffer (S9 wave-2, BUILD.md S5a "no silent fallbacks" --
    dropping is visible via `metrics.summaries_dropped_total`, never a silent loss).
    A full buffer means the flush side (Postgres, or the timer calling
    `flush_summaries()`) is falling behind; dropping the OLDEST summary keeps the
    freshest data (what the continuous-monitoring loop and G-21..G-23 actually read,
    S5.4's freshness gate) rather than rejecting the newest arrival."""
    if len(_pending_summaries) >= _summary_buffer_max:
        _pending_summaries.popleft()
        metrics.summaries_dropped_total.inc()
    _pending_summaries.append(row)
    metrics.summaries_buffered.set(len(_pending_summaries))


def pending_summary_count() -> int:
    """Current buffer depth -- for health/tests; also exported as `og_pq_ingest_summaries_buffered`."""
    return len(_pending_summaries)


async def flush_summaries() -> int:
    """Drains the whole summary buffer as one batched insert (S9 wave-2 fix). Called by
    the caller's own periodic timer (`[pq_ingest].flush_interval_s`, engine-owned wiring,
    see README) and opportunistically from `ingest_summary` once the buffer reaches
    `flush_batch_size`. On a backend failure the rows are requeued (subject to the same
    drop-oldest cap) and the exception re-raised, so the caller's own tick-level
    try/except (the same pattern `ogsim.fleet.runtime.run_fleet`/`opengrid.engine`'s tick
    loops already use) logs it and keeps ticking rather than losing the batch silently.
    Returns the number of rows flushed (0 if the buffer was empty)."""
    if not _pending_summaries:
        return 0
    rows = list(_pending_summaries)
    _pending_summaries.clear()
    metrics.summaries_buffered.set(0)
    backend = _require_backend()
    try:
        await backend.insert_summaries_batch(rows)
    except Exception:
        metrics.summaries_flush_failed_total.inc()
        logger.exception("batched pq summary flush failed; requeuing %d row(s)", len(rows))
        for row in rows:
            _buffer_summary(row)
        raise
    metrics.summaries_flushed_total.inc(len(rows))
    return len(rows)


async def ingest_raw_capture(payload: dict[str, Any]) -> PqWaveformRawIndex:
    """Persists one already schema-validated `PqWaveformRawCaptureHeader` message
    (S6.4a-b): the sample block goes to object storage (`BlobStore`, "a blob reference,
    not a DB blob" per `og.pq_waveform_raw_index`'s own comment), and the index row goes
    to Postgres. Resolves any `PendingCaptureTracker` entry for this hub (S6.6/TS-15a's
    on-demand round trip)."""
    header = WaveformRawCaptureHeader.model_validate(_row_fields(payload, WaveformRawCaptureHeader))
    samples = payload.get("samples") or {}
    blob = encode_raw_samples(header.channels, samples)
    blob_ref = await _require_blob_store().put(blob, hub_id=header.hub_id, ts=header.ts)
    row = PqWaveformRawIndex(
        capture_id=uuid4(),
        hub_id=header.hub_id,
        ts=header.ts,
        trigger_reason=header.trigger_reason,
        blob_ref=blob_ref,
        channels=len(header.channels),
        sample_rate_hz=Decimal(str(header.sample_rate_hz)),
        cycles=header.cycles,
    )
    await _require_backend().insert_raw_index(row)
    _pending.resolve(header.hub_id)
    return row


async def latest_summaries(hub_ids: Sequence[str], *, since: datetime) -> list[PqWaveformSummaryRow]:
    """Read-through to the backend -- the building block WP-J's query API and WP-C/D's
    guardian/allocator gateways read PQ history through (S6.5 step 4's three consumers),
    kept here rather than duplicated in each caller (BUILD.md S1)."""
    return await _require_backend().latest_summaries(hub_ids, since=since)


def track_capture_request(request: WaveformCaptureRequest) -> None:
    """Registers `request` as outstanding so a later `ingest_raw_capture` for the same
    hub is understood as this request's response (S6.6). Callers that publish a
    `WaveformCaptureRequest` (built via `build_capture_request`) call this right after."""
    _pending.track(request)


def expire_stale_capture_requests(now: datetime) -> list[WaveformCaptureRequest]:
    """Drops and returns any tracked request whose `expires_at` has passed with no
    matching raw capture -- housekeeping for a caller's own periodic sweep (S6.4b: "the
    hub ignores the request if it cannot capture and publish before this deadline")."""
    return _pending.expire_stale(now)


def decode_stored_raw_capture(channels: list[str], blob: bytes) -> dict[str, list[int]]:
    """Inverse of `encode_raw_samples` -- for a reader (the S6.5-step-2 audit job, or
    WP-J's `GET .../waveform`) that has a blob_ref's bytes and needs the per-channel
    int16 sample arrays back."""
    return decode_raw_samples(channels, blob)
