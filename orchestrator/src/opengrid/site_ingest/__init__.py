"""opengrid.site_ingest -- the customer-side closed-loop signals (06-service-profiles-and-power-quality.md
S4.a PIPELINE_AC, S4.b DATA_CENTER; S1.3 `feedback_signal_ref`).

Ingests two payloads the customer's own equipment publishes:

- `<root>/site/<customer_id>/<site_id>/meter` -- `interfaces/mqtt/customer_site_meter.schema.json`, the
  DATA_CENTER site meter at the point of common coupling;
- `<root>/corridor/<customer_id>/<corridor_id>/current` -- `interfaces/mqtt/pipeline_corridor_current
  .schema.json`, the PIPELINE_AC corridor current.

`ingest_*` validates the payload (the pydantic mirror of the schema, and that the payload's customer and
source ids match the topic's, so one customer cannot publish into another's loop), keeps the newest
reading per source in memory, and buffers it. It never touches the database, so it is safe on the MQTT
ingest loop. `flush_readings()` -- driven by the engine's own periodic timer -- writes the buffer in one
batched, asynchronously committed statement per table (the host has Postgres checkpoint stalls; no
per-message commits). A failed flush keeps the rows for the next one, bounded by the buffer size
(oldest dropped first, counted).

`resolve_feedback_signal()` turns a profile's `feedback_signal_ref` into the current value and its age
for the closed-loop controllers (`opengrid.allocator.closed_loop_data_center` /
`closed_loop_pipeline_ac`), scoped to the obligation's customer.

Module-level singleton facade like `opengrid.pq_ingest`: `configure()` once per process. It holds no MQTT
client; the engine's ingest loop subscribes and hands payloads over (see README for the wiring lines).
"""

from __future__ import annotations

import logging
from collections import Counter, deque
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import ValidationError

from opengrid.site_ingest.latest import (
    FeedbackRefError,
    LatestValues,
    Reading,
    StaleOrFutureReadingError,
    parse_feedback_ref,
)
from opengrid.site_ingest.models import CorridorCurrentReading, FeedbackValue, SiteMeterReading

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_BUFFER_MAX",
    "DEFAULT_FLUSH_INTERVAL_S",
    "DEFAULT_MAX_FUTURE_SKEW_S",
    "CorridorCurrentReading",
    "FeedbackRefError",
    "FeedbackValue",
    "SiteIngestBackend",
    "SiteIngestRejectedError",
    "SiteMeterReading",
    "configure",
    "counters",
    "flush_readings",
    "ingest_corridor_current",
    "ingest_site_meter",
    "latest_corridor_current",
    "latest_site_meter",
    "parse_feedback_ref",
    "pending_count",
    "reset_for_testing",
    "resolve_feedback_signal",
]

#: Both signals publish every 2 s (the schemas' descriptions); 10,000 rows is > 5 min of 30 sources.
DEFAULT_BUFFER_MAX = 10_000
#: The engine's flush timer period. Readings are soft telemetry: the controllers use the in-memory value.
DEFAULT_FLUSH_INTERVAL_S = 5.0
#: A reading stamped more than this ahead of the receiver's clock is refused (clock-skew guard).
DEFAULT_MAX_FUTURE_SKEW_S = 5.0

_SITE_TOPIC = ("site", "meter")
_CORRIDOR_TOPIC = ("corridor", "current")
_TOPIC_PARTS = 4


class SiteIngestBackend(Protocol):
    async def insert_batch(
        self, site_rows: Sequence[SiteMeterReading], corridor_rows: Sequence[CorridorCurrentReading]
    ) -> None: ...


class SiteIngestRejectedError(ValueError):
    """An inbound payload that is not accepted: schema-invalid, ids not matching its topic, or a
    timestamp ahead of the receiver clock. The caller logs and drops it."""


class _NotConfiguredError(RuntimeError):
    pass


_backend: SiteIngestBackend | None = None
_latest = LatestValues(max_future_skew_s=DEFAULT_MAX_FUTURE_SKEW_S)
_pending_site: deque[SiteMeterReading] = deque()
_pending_corridor: deque[CorridorCurrentReading] = deque()
_buffer_max = DEFAULT_BUFFER_MAX
_counters: Counter[str] = Counter()


def configure(
    backend: SiteIngestBackend,
    *,
    buffer_max: int = DEFAULT_BUFFER_MAX,
    max_future_skew_s: float = DEFAULT_MAX_FUTURE_SKEW_S,
) -> None:
    """Wire the process-wide facade (once at start-up). Clears any held state."""
    global _backend, _latest, _buffer_max
    _backend = backend
    _buffer_max = buffer_max
    _latest = LatestValues(max_future_skew_s=max_future_skew_s)
    _pending_site.clear()
    _pending_corridor.clear()
    _counters.clear()


def reset_for_testing() -> None:
    global _backend
    _backend = None
    _latest.clear()
    _pending_site.clear()
    _pending_corridor.clear()
    _counters.clear()


def _topic_ids(topic: str, root: str, expected: tuple[str, str]) -> tuple[str, str]:
    """(customer_id, source_id) from `<root>/<kind>/<customer_id>/<source_id>/<leaf>`."""
    prefix = root.rstrip("/") + "/"
    parts = topic.removeprefix(prefix).split("/") if topic.startswith(prefix) else []
    if len(parts) != _TOPIC_PARTS or (parts[0], parts[3]) != expected or not parts[1] or not parts[2]:
        raise SiteIngestRejectedError(f"unexpected topic {topic!r}")
    return parts[1], parts[2]


def _validated[ModelT: (SiteMeterReading, CorridorCurrentReading)](
    model: type[ModelT], payload: dict[str, Any]
) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        _counters["rejected_invalid"] += 1
        raise SiteIngestRejectedError(f"{model.__name__}: {exc.error_count()} validation error(s)") from exc


def _accept(reading: Reading, buffer: deque[Any], now: datetime | None) -> bool:
    try:
        became_latest = _latest.offer(reading, now=now or datetime.now(UTC))
    except StaleOrFutureReadingError as exc:
        _counters["rejected_future_ts"] += 1
        raise SiteIngestRejectedError(str(exc)) from exc
    if len(buffer) >= _buffer_max:
        buffer.popleft()
        _counters["dropped_buffer_full"] += 1
    buffer.append(reading)
    _counters["accepted"] += 1
    return became_latest


def ingest_site_meter(payload: dict[str, Any], *, topic: str, root: str, now: datetime | None = None) -> bool:
    """Validate and hold one site-meter reading. Returns whether it is now the latest for its site.
    Raises `SiteIngestRejectedError` (nothing held) when it is not accepted."""
    customer_id, site_id = _topic_ids(topic, root, _SITE_TOPIC)
    reading = _validated(SiteMeterReading, payload)
    if (reading.customer_id, reading.site_id) != (customer_id, site_id):
        _counters["rejected_topic_mismatch"] += 1
        raise SiteIngestRejectedError("payload customer_id/site_id do not match the topic")
    return _accept(reading, _pending_site, now)


def ingest_corridor_current(
    payload: dict[str, Any], *, topic: str, root: str, now: datetime | None = None
) -> bool:
    """Validate and hold one corridor-current reading (see `ingest_site_meter`)."""
    customer_id, corridor_id = _topic_ids(topic, root, _CORRIDOR_TOPIC)
    reading = _validated(CorridorCurrentReading, payload)
    if (reading.customer_id, reading.corridor_id) != (customer_id, corridor_id):
        _counters["rejected_topic_mismatch"] += 1
        raise SiteIngestRejectedError("payload customer_id/corridor_id do not match the topic")
    return _accept(reading, _pending_corridor, now)


async def flush_readings() -> int:
    """Write every buffered reading in one batch; returns the number of rows handed to the backend. On
    failure the rows go back to the front of the buffer (still bounded) and the error propagates to
    the caller's periodic runner, which logs it and retries on the next interval."""
    if _backend is None:
        raise _NotConfiguredError("opengrid.site_ingest.configure() must be called before flush_readings()")
    site_rows = list(_pending_site)
    corridor_rows = list(_pending_corridor)
    _pending_site.clear()
    _pending_corridor.clear()
    try:
        await _backend.insert_batch(site_rows, corridor_rows)
    except Exception:
        _requeue(_pending_site, site_rows)
        _requeue(_pending_corridor, corridor_rows)
        _counters["flush_failed"] += 1
        raise
    return len(site_rows) + len(corridor_rows)


def _requeue(buffer: deque[Any], rows: list[Any]) -> None:
    newer = list(buffer)
    combined = rows + newer
    overflow = max(len(combined) - _buffer_max, 0)
    if overflow:
        _counters["dropped_buffer_full"] += overflow
    buffer.clear()
    buffer.extend(combined[overflow:])


def resolve_feedback_signal(
    ref: str, *, customer_id: str, max_age_s: float, now: datetime | None = None
) -> FeedbackValue | None:
    """The current value of `ref` (S1.3 `feedback_signal_ref`) for `customer_id`, with its age against
    `max_age_s` (the profile's freshness gate). `None` when nothing has been received for that
    customer's source -- the controller treats that exactly like a stale signal."""
    return _latest.resolve(ref, customer_id=customer_id, now=now or datetime.now(UTC), max_age_s=max_age_s)


def latest_site_meter(customer_id: str, site_id: str) -> SiteMeterReading | None:
    reading = _latest.latest("site_meter", customer_id, site_id)
    return reading if isinstance(reading, SiteMeterReading) else None


def latest_corridor_current(customer_id: str, corridor_id: str) -> CorridorCurrentReading | None:
    reading = _latest.latest("corridor", customer_id, corridor_id)
    return reading if isinstance(reading, CorridorCurrentReading) else None


def pending_count() -> int:
    return len(_pending_site) + len(_pending_corridor)


def counters() -> dict[str, int]:
    """accepted / rejected_* / dropped_buffer_full / flush_failed since `configure()`."""
    return dict(_counters)
