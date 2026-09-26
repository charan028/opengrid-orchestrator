"""Unit tests for capture.py's pure parts (summary, checks, SSE timing) plus the dry-run end to end."""

from __future__ import annotations

from pathlib import Path

import pytest

from capture import (
    DryRunSource,
    Targets,
    evaluate,
    frame_timestamp,
    iter_sse_data,
    main,
    run_capture,
    sse_stats,
)
from metrics import Exposition, parse_exposition

SAMPLE = (Path(__file__).parent / "sample_metrics.txt").read_text(encoding="utf-8")


def _run(source: DryRunSource, frames: int = 5):
    return run_capture(
        source,
        endpoints={"engine": "http://x/metrics"},
        samples=3,
        interval_s=2.0,
        sse_url="http://x/og/api/stream/health",
        sse_frames=frames,
        sleep=lambda _s: None,
    )


def test_dry_run_summary_passes_targets() -> None:
    summary = _run(DryRunSource())
    assert summary.cycle is not None and summary.cycle.count == 300.0
    assert summary.hub_total == 2000.0
    assert summary.sse.mode == "timestamp"
    assert summary.sse.p99_s == pytest.approx(0.4)
    assert all(c.passed for c in evaluate(summary, Targets())), evaluate(summary, Targets())


def test_evaluate_fails_when_load_or_budget_missed() -> None:
    summary = _run(DryRunSource())
    by_name = {c.name.split(" (")[0]: c for c in evaluate(summary, Targets(expected_hubs=10_000))}
    assert not by_name["load present"].passed
    tight = evaluate(summary, Targets(cycle_p99_budget_s=0.2))
    assert not tight[0].passed and "p99 =" in tight[0].detail


def test_evaluate_reports_missing_histogram_and_scrape_failures() -> None:
    class Empty(DryRunSource):
        def scrape(self, endpoints):  # type: ignore[override]
            return Exposition(), {"engine": 1}

    checks = evaluate(_run(Empty(), frames=0), Targets())
    assert [c.passed for c in checks] == [False, False, False, False]
    assert "no cycle observations" in checks[0].detail
    assert "3 failed" in checks[3].detail


def test_sse_stats_uses_frame_clock_when_present_else_gaps() -> None:
    stamped = [
        (100.0, {"as_of": "1970-01-01T00:01:39.500000+00:00"}),
        (101.0, {"as_of": "1970-01-01T00:01:40+00:00"}),
    ]
    s = sse_stats(stamped)
    assert s.mode == "timestamp" and s.latencies_s == (0.5, 1.0)
    gaps = sse_stats([(100.0, {"fleet_mw": 1}), (101.5, {"fleet_mw": 2}), (103.5, {"fleet_mw": 3})])
    assert gaps.mode == "gap" and gaps.latencies_s == (1.5, 2.0) and gaps.max_s == 2.0
    assert sse_stats([(1.0, {})]).mode == "none"
    assert frame_timestamp({"as_of": "not a date"}) is None
    assert frame_timestamp("scalar") is None


def test_iter_sse_data_skips_comments_and_joins_multiline() -> None:
    lines = [": ping", "", "event: message", 'data: {"a":', "data: 1}", "", "data: 2", ""]
    assert list(iter_sse_data(lines)) == ['{"a":\n1}', "2"]


def test_sample_exposition_is_what_dry_run_serves() -> None:
    assert parse_exposition(SAMPLE).value("og_hubs") == 2000.0


def test_cli_dry_run_writes_report(tmp_path: Path) -> None:
    report = tmp_path / "perf.md"
    assert main(["--dry-run", "--report", str(report)]) == 0
    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Perf capture -- PASS")
    assert "RT cycle p99 < 500 ms | PASS" in text
