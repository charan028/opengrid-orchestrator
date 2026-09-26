"""og-engine /metrics (chaos harness): loopback-only endpoint, cycle latency, gate duration, flush lag and
MQTT ingest lag series."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from prometheus_client import REGISTRY

from opengrid.engine import metrics as m
from opengrid.engine.gates import run_due_gates
from opengrid.engine.latency import LoopLagProbe
from opengrid.platform.config import Config


def _value(name: str, labels: dict[str, str] | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels or {})
    return 0.0 if value is None else value


def test_endpoint_uses_the_configured_port_on_loopback() -> None:
    calls: list[tuple[int, str]] = []
    port = m.start_metrics_server(
        Config({"metrics": {"engine_port": 9101, "bind_host": "127.0.0.1"}}),
        start=lambda p, addr: calls.append((p, addr)),
    )
    assert port == 9101
    assert calls == [(9101, "127.0.0.1")]


def test_a_non_loopback_bind_host_is_refused() -> None:
    calls: list[tuple[int, str]] = []
    m.start_metrics_server(
        Config({"metrics": {"engine_port": 9101, "bind_host": "0.0.0.0"}}),  # noqa: S104 -- the refused case
        start=lambda p, addr: calls.append((p, addr)),
    )
    assert calls == [(9101, "127.0.0.1")]


def test_no_port_configured_means_no_endpoint() -> None:
    calls: list[object] = []
    assert m.start_metrics_server(Config({}), start=lambda *a, **k: calls.append(a)) is None
    assert calls == []


def test_tick_and_cycle_summary_series() -> None:
    before = _value("og_control_tick_duration_seconds_count", {"phase": "total"})
    m.observe_tick(0.04)
    assert _value("og_control_tick_duration_seconds_count", {"phase": "total"}) == before + 1

    m.publish_cycle_summary({"p50_ms": 18.0, "p99_ms": 43.0, "max_ms": 58.0})
    assert _value("og_engine_cycle_latency_ms", {"quantile": "p99"}) == 43.0
    assert _value("og_engine_cycle_latency_ms", {"quantile": "max"}) == 58.0


def test_loop_lag_probe_feeds_the_eventloop_lag_histogram() -> None:
    before = _value("og_eventloop_lag_seconds_count", {"process": "engine"})
    LoopLagProbe(on_sample=m.observe_loop_lag).observe(expected=1.0, actual=1.02)
    assert _value("og_eventloop_lag_seconds_count", {"process": "engine"}) == before + 1


async def test_gate_duration_is_observed_even_when_the_gate_fails() -> None:
    seen: list[tuple[str, float]] = []
    ticks = iter([10.0, 12.5])

    class _Trigger:
        gate_kind = "ADMISSION"
        contract_scope = None

    async def _intake(*args, **kwargs):
        return None

    async def _gate(*args, **kwargs):
        raise RuntimeError("solver down")

    class _Trace:
        async def append(self, *args):
            return None

    async def _alert(finding):
        return None

    failed = await run_due_gates(
        [_Trigger()],
        now=datetime.now(UTC),
        run_intake=_intake,
        run_gate=_gate,
        trace=_Trace(),
        raise_alert=_alert,
        observe_duration=lambda kind, s: seen.append((kind, s)),
        clock=lambda: next(ticks),
    )
    assert failed == 1
    assert seen == [("ADMISSION", 2.5)]


def test_flush_lag_reads_time_since_the_last_good_flush() -> None:
    now = {"t": 100.0}
    lag = m.FlushLag(clock=lambda: now["t"])
    now["t"] = 107.5
    assert lag.lag_s() == 7.5
    lag.mark_ok()
    assert lag.lag_s() == 0.0


def test_ingest_lag_is_the_telemetry_age_and_bad_timestamps_are_skipped() -> None:
    now = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
    before = _value("og_engine_mqtt_ingest_lag_seconds_count")
    before_sum = _value("og_engine_mqtt_ingest_lag_seconds_sum")
    m.observe_ingest((now - timedelta(seconds=1.5)).isoformat(), now=now)
    m.observe_ingest("not-a-time", now=now)
    m.observe_ingest(None, now=now)
    assert _value("og_engine_mqtt_ingest_lag_seconds_count") == before + 1
    assert _value("og_engine_mqtt_ingest_lag_seconds_sum") - before_sum == 1.5
