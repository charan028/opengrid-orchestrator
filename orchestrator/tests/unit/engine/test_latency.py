"""A11 cycle-latency window: p50/p99/max over recent engine ticks, reported periodically, with a
per-phase breakdown of the slowest tick and the event-loop lag seen by a probe coroutine."""

from __future__ import annotations

import asyncio
import contextlib

from opengrid.engine.latency import DEFAULT_WINDOW, CycleLatencyWindow, LoopLagProbe, PhaseTimer, percentile


def test_percentile_is_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 99) == 99.0
    assert percentile([], 99) == 0.0


def test_default_window_covers_at_least_300_ticks() -> None:
    assert DEFAULT_WINDOW >= 300


def test_window_keeps_only_recent_samples_and_summarizes() -> None:
    window = CycleLatencyWindow(window=3)
    for ms in (1000.0, 10.0, 20.0, 30.0):
        window.record(ms)

    summary = window.summary()
    assert {k: summary[k] for k in ("samples", "p50_ms", "p99_ms", "max_ms")} == {
        "samples": 3.0,
        "p50_ms": 20.0,
        "p99_ms": 30.0,
        "max_ms": 30.0,
    }


def test_summary_carries_the_slowest_ticks_phase_breakdown() -> None:
    window = CycleLatencyWindow(window=10)
    window.record(50.0, {"allocator": 40.0, "fleet_flush": 10.0})
    window.record(900.0, {"allocator": 100.0, "fleet_flush": 790.0})
    window.record(60.0, {"allocator": 55.0})

    assert window.summary()["slowest_phases"] == {"allocator": 100.0, "fleet_flush": 790.0}


def test_summary_reports_phase_p99_across_the_window() -> None:
    window = CycleLatencyWindow(window=10)
    for ms in (10.0, 20.0, 300.0):
        window.record(ms, {"fleet_flush": ms})
    assert window.summary()["phase_p99_ms"] == {"fleet_flush": 300.0}


def test_summary_includes_loop_lag_from_the_probe() -> None:
    probe = LoopLagProbe(interval_s=0.1, window=10)
    probe.observe(expected=1.0, actual=1.25)
    probe.observe(expected=2.0, actual=2.01)
    window = CycleLatencyWindow(window=10, lag_probe=probe)
    window.record(10.0)

    summary = window.summary()
    assert summary["loop_lag_p99_ms"] == 250.0
    assert summary["loop_lag_max_ms"] == 250.0


def test_phase_timer_accumulates_named_phases() -> None:
    ticks = iter([0.0, 0.010, 0.010, 0.030, 0.030, 0.035])
    timer = PhaseTimer(clock=lambda: next(ticks))
    with timer.phase("a"):
        pass
    with timer.phase("b"):
        pass
    with timer.phase("a"):
        pass
    assert timer.phases == {"a": 15.0, "b": 20.0}


def test_phase_timer_records_a_phase_that_raises() -> None:
    ticks = iter([0.0, 0.5])
    timer = PhaseTimer(clock=lambda: next(ticks))
    try:
        with timer.phase("boom"):
            raise RuntimeError("x")
    except RuntimeError:
        pass
    assert timer.phases == {"boom": 500.0}


def test_loop_lag_probe_runs_until_cancelled() -> None:
    async def scenario() -> int:
        probe = LoopLagProbe(interval_s=0.001, window=50)
        task = asyncio.create_task(probe.run())
        await asyncio.sleep(0.05)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return len(probe.samples)

    assert asyncio.run(scenario()) > 0


def test_report_is_due_first_then_once_per_interval() -> None:
    window = CycleLatencyWindow(report_every_s=60.0)
    assert window.report_due(0.0)
    assert not window.report_due(59.0)
    assert window.report_due(60.0)
