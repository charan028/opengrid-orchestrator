"""A11 cycle-latency window: p50/p99/max over recent engine ticks, reported periodically."""

from __future__ import annotations

from opengrid.engine.latency import CycleLatencyWindow, percentile


def test_percentile_is_nearest_rank() -> None:
    values = [float(v) for v in range(1, 101)]
    assert percentile(values, 50) == 50.0
    assert percentile(values, 99) == 99.0
    assert percentile([], 99) == 0.0


def test_window_keeps_only_recent_samples_and_summarizes() -> None:
    window = CycleLatencyWindow(window=3)
    for ms in (1000.0, 10.0, 20.0, 30.0):
        window.record(ms)

    assert window.summary() == {"samples": 3.0, "p50_ms": 20.0, "p99_ms": 30.0, "max_ms": 30.0}


def test_report_is_due_first_then_once_per_interval() -> None:
    window = CycleLatencyWindow(report_every_s=60.0)
    assert window.report_due(0.0)
    assert not window.report_due(59.0)
    assert window.report_due(60.0)
