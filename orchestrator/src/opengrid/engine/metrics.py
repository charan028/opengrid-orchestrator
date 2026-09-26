"""og-engine's Prometheus `/metrics` endpoint (loopback only) and its engine-specific series.

The 02b S6.6 catalogue metrics (`og_control_tick_duration_seconds`, `og_eventloop_lag_seconds`, ...) are
defined once in `opengrid.platform.metrics`; this module only adds what the chaos harness needs beyond
them and owns the endpoint:

- `og_engine_cycle_latency_ms{quantile="p50"|"p99"|"max"}`: the dispatch tick's rolling window (A11);
- `og_engine_gate_duration_seconds{gate_kind}`: intake + selector gate wall time;
- `og_engine_fleet_flush_lag_seconds`: seconds since the last successful fleet persistence pass;
- `og_engine_mqtt_ingest_lag_seconds`: age of a telemetry message when the twin applies it.

`health` scrapes this endpoint (`[health].engine_metrics_url`) for the cycle-latency alert.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from prometheus_client import Gauge, Histogram, start_http_server

from opengrid.platform import metrics as platform_metrics
from opengrid.platform.config import Config

logger = logging.getLogger(__name__)

PROCESS = "engine"
LOOPBACK = "127.0.0.1"

cycle_latency_ms = Gauge(
    "og_engine_cycle_latency_ms",
    "og-engine dispatch tick latency over the rolling window (A11), by quantile.",
    labelnames=("quantile",),
)
gate_duration_seconds = Histogram(
    "og_engine_gate_duration_seconds",
    "Wall time of one selector gate (intake + solve + commit), by gate kind.",
    labelnames=("gate_kind",),
    buckets=(0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0),
)
fleet_flush_lag_seconds = Gauge(
    "og_engine_fleet_flush_lag_seconds",
    "Seconds since the last successful fleet persistence pass (telemetry, hub_state, SCADA, acks).",
)
mqtt_ingest_lag_seconds = Histogram(
    "og_engine_mqtt_ingest_lag_seconds",
    "Age of a hub telemetry message (its ts to now) when og-engine's fleet twin applies it.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0),
)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def start_metrics_server(cfg: Config, *, start: Callable[..., Any] = start_http_server) -> int | None:
    """Serve `/metrics` on `[metrics].engine_port`, bound to loopback only. A non-loopback
    `[metrics].bind_host` is refused (logged) and loopback is used. Returns the port, or `None` when no
    port is configured."""
    port = cfg.get("metrics.engine_port")
    if port is None:
        return None
    host = str(cfg.get("metrics.bind_host", LOOPBACK))
    if not _is_loopback(host):
        logger.warning(
            "og-engine metrics must bind to loopback; ignoring bind_host", extra={"bind_host": host}
        )
        host = LOOPBACK
    start(int(port), addr=host)
    return int(port)


def observe_tick(duration_s: float) -> None:
    platform_metrics.control_tick_duration_seconds.labels(phase="total").observe(duration_s)


def publish_cycle_summary(summary: dict[str, Any]) -> None:
    for quantile, key in (("p50", "p50_ms"), ("p99", "p99_ms"), ("max", "max_ms")):
        cycle_latency_ms.labels(quantile=quantile).set(float(summary.get(key, 0.0)))


def observe_loop_lag(lag_ms: float) -> None:
    platform_metrics.eventloop_lag_seconds.labels(process=PROCESS).observe(lag_ms / 1000.0)


def observe_gate(gate_kind: str, duration_s: float) -> None:
    gate_duration_seconds.labels(gate_kind=str(gate_kind)).observe(duration_s)


class FlushLag:
    """Tracks the last successful fleet persistence pass; the gauge reads the lag at scrape time."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._last_ok = clock()

    def mark_ok(self) -> None:
        self._last_ok = self._clock()

    def lag_s(self) -> float:
        return max(0.0, self._clock() - self._last_ok)

    def bind(self) -> None:
        fleet_flush_lag_seconds.set_function(self.lag_s)


def observe_ingest(payload_ts: str | None, *, now: datetime | None = None) -> None:
    """Record a telemetry message's age on arrival; an unparsable/missing ts is skipped."""
    if not payload_ts:
        return
    try:
        ts = datetime.fromisoformat(payload_ts.replace("Z", "+00:00"))
    except ValueError:
        return
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    age_s = ((now or datetime.now(UTC)) - ts).total_seconds()
    mqtt_ingest_lag_seconds.observe(max(age_s, 0.0))
