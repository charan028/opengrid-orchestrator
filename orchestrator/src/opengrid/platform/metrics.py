"""Prometheus metrics registry: the catalogue from 02b S6.6, one process per /metrics port.

Each process imports the metrics it emits from here (single definition per metric name avoids
duplicate-registration errors when a process's modules are imported more than once, e.g. in tests).
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# --- og-engine -----------------------------------------------------------------------------------
control_tick_duration_seconds = Histogram(
    "og_control_tick_duration_seconds",
    "Per-phase and total real-time control-cycle latency.",
    labelnames=("phase",),
)
control_ticks_total = Counter(
    "og_control_ticks_total",
    "Real-time control-cycle count.",
    labelnames=("outcome",),  # on_time | late
)
command_ack_latency_seconds = Histogram(
    "og_command_ack_latency_seconds",
    "Time from command batch issue to ack received.",
)
commands_total = Counter(
    "og_commands_total",
    "Commands issued, by result.",
    labelnames=("result",),  # acked | rejected | expired
)

# --- fleet health ----------------------------------------------------------------------------------
hubs = Gauge(
    "og_hubs",
    "Current hub count per health state.",
    labelnames=("health",),  # online | stale | offline | fault
)
telemetry_fresh_ratio = Gauge(
    "og_telemetry_fresh_ratio",
    "Fraction of hubs with telemetry no older than 2x the telemetry interval.",
    labelnames=("zone",),
)

# --- feeds -----------------------------------------------------------------------------------------
feed_age_seconds = Gauge(
    "og_feed_age_seconds",
    "Age of the latest accepted value for a feed source/product.",
    labelnames=("source", "product"),
)
feed_breaker_open = Gauge(
    "og_feed_breaker_open",
    "1 if the circuit breaker for this feed source is open, else 0.",
    labelnames=("source",),
)

# --- guardian ----------------------------------------------------------------------------------------
guardian_verdicts_total = Counter(
    "og_guardian_verdicts_total",
    "Guardian verdicts, by outcome.",
    labelnames=("outcome",),  # signed | vetoed | timeout
)
reserve_breaches_total = Counter(
    "og_reserve_breaches_total",
    "Count of reserve-floor breaches (K1). Must stay 0 (A10).",
)
double_sold_kwh_total = Counter(
    "og_double_sold_kwh_total",
    "kWh sold twice across obligations (K2 violation). Must stay 0 (A10).",
)
guardian_clock_offset_ms = Gauge(
    "og_guardian_clock_offset_ms",
    "Guardian's own NTP clock offset in milliseconds (K12/G-20).",
)

# --- cross-process -------------------------------------------------------------------------------
process_up = Gauge(
    "og_process_up",
    "1 if the process's heartbeat is current, else 0.",
    labelnames=("process",),
)
eventloop_lag_seconds = Histogram(
    "og_eventloop_lag_seconds",
    "asyncio event-loop scheduling lag.",
    labelnames=("process",),
)
