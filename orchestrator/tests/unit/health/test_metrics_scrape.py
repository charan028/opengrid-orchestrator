"""Unit tests for `opengrid.health.metrics_scrape`'s pure Prometheus text-format helpers."""

from __future__ import annotations

from opengrid.health.metrics_scrape import histogram_p99_from_buckets, parse_prometheus_text, sum_metric

_SAMPLE_TEXT = """
# HELP og_control_tick_duration_seconds Per-phase and total real-time control-cycle latency.
# TYPE og_control_tick_duration_seconds histogram
og_control_tick_duration_seconds_bucket{phase="select",le="0.1"} 50
og_control_tick_duration_seconds_bucket{phase="select",le="0.5"} 99
og_control_tick_duration_seconds_bucket{phase="select",le="1.0"} 100
og_control_tick_duration_seconds_bucket{phase="select",le="+Inf"} 100
og_control_tick_duration_seconds_sum{phase="select"} 12.3
og_control_tick_duration_seconds_count{phase="select"} 100
og_guardian_verdicts_total{outcome="signed"} 950
og_guardian_verdicts_total{outcome="timeout"} 5
og_reserve_breaches_total 0
"""


def test_parse_prometheus_text_ignores_comments_and_blank_lines() -> None:
    samples = parse_prometheus_text(_SAMPLE_TEXT)
    assert "og_reserve_breaches_total" in samples
    assert samples["og_reserve_breaches_total"] == 0.0
    assert samples['og_guardian_verdicts_total{outcome="signed"}'] == 950.0


def test_sum_metric_across_label_combinations() -> None:
    samples = parse_prometheus_text(_SAMPLE_TEXT)
    total = sum_metric(samples, "og_guardian_verdicts_total")
    assert total == 955.0


def test_sum_metric_with_label_filter() -> None:
    samples = parse_prometheus_text(_SAMPLE_TEXT)
    timeouts = sum_metric(samples, "og_guardian_verdicts_total", label_filter='outcome="timeout"')
    assert timeouts == 5.0


def test_histogram_p99_from_buckets_uses_count_specific_to_labels() -> None:
    samples = parse_prometheus_text(_SAMPLE_TEXT)
    p99 = histogram_p99_from_buckets(samples, "og_control_tick_duration_seconds")
    # 99% of 100 observations = 99 -> first bucket reaching that cumulative count is le="0.5"
    assert p99 == 0.5


def test_histogram_p99_returns_none_without_matching_metric() -> None:
    samples = parse_prometheus_text(_SAMPLE_TEXT)
    assert histogram_p99_from_buckets(samples, "og_nonexistent_histogram") is None


def test_histogram_p99_returns_none_when_only_inf_bucket_reaches_target() -> None:
    text = 'og_x_bucket{le="0.1"} 0\nog_x_bucket{le="+Inf"} 100\nog_x_count 100\n'
    samples = parse_prometheus_text(text)
    assert histogram_p99_from_buckets(samples, "og_x") is None
