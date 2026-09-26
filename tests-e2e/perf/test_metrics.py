"""Unit tests for `metrics.py` against the bundled hand-written exposition (WORKBOARD Q3, TS-N cycle latency)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from metrics import Histogram, counter_delta, histogram_delta, histogram_quantile, parse_exposition

SAMPLE = (Path(__file__).parent / "sample_metrics.txt").read_text(encoding="utf-8")


def test_parse_skips_comments_and_reads_labels() -> None:
    exp = parse_exposition(SAMPLE)
    assert exp.value("og_hubs", {"health": "online"}) == 1987.0
    assert exp.value("og_hubs") == 2000.0  # summed across label values
    assert exp.value("og_reserve_breaches_total") == 0.0
    assert exp.value("og_does_not_exist") is None
    assert exp.value("og_control_ticks_total", {"outcome": "late"}) == 1.0


def test_parse_tolerates_escaped_quotes_timestamps_and_junk() -> None:
    exp = parse_exposition('a{x="q\\"uote"} 1 1700000000\nnot a sample line at all\nb NaNx\n')
    assert exp.samples[0].label("x") == 'q"uote'
    assert len(exp.samples) == 1


def test_histogram_p99_interpolates_within_bucket() -> None:
    hist = parse_exposition(SAMPLE).histogram("og_control_tick_duration_seconds", {"phase": "total"})
    assert hist is not None
    assert hist.count == 300.0
    # rank 297 falls in the (0.25, 0.5] bucket: 285 below, 14 inside -> 0.25 + 0.25 * 12/14
    assert hist.quantile(0.99) == pytest.approx(0.25 + 0.25 * 12 / 14)
    # rank 150 is exactly the upper edge of the 0.1 bucket
    assert hist.quantile(0.5) == pytest.approx(0.1)
    assert hist.mean == pytest.approx(36.5 / 300)


def test_histogram_aggregates_across_labels_when_no_filter() -> None:
    hist = parse_exposition(SAMPLE).histogram("og_control_tick_duration_seconds")
    assert hist is not None
    assert hist.count == 600.0
    assert hist.buckets[-1] == (math.inf, 600.0)


def test_quantile_edge_cases() -> None:
    assert histogram_quantile(0.99, []) is None
    assert histogram_quantile(0.99, [(0.1, 0.0), (math.inf, 0.0)]) is None
    assert histogram_quantile(0.99, [(0.1, 5.0)]) is None  # no +Inf bucket -> not a valid histogram
    # everything above the last finite bucket -> its upper bound is the best answer
    assert histogram_quantile(0.99, [(0.1, 0.0), (math.inf, 10.0)]) == 0.1
    # first bucket: interpolate from 0
    assert histogram_quantile(0.5, [(0.1, 10.0), (math.inf, 10.0)]) == pytest.approx(0.05)


def test_delta_between_two_scrapes() -> None:
    before = Histogram("h", ((0.1, 10.0), (0.5, 20.0), (math.inf, 20.0)), 3.0, 20.0)
    after = Histogram("h", ((0.1, 12.0), (0.5, 40.0), (math.inf, 40.0)), 10.0, 40.0)
    d = histogram_delta(before, after)
    assert d.buckets == ((0.1, 2.0), (0.5, 20.0), (math.inf, 20.0))
    assert d.count == 20.0 and d.sum == 7.0
    assert histogram_delta(None, after) is after
    assert histogram_delta(after, before) is before  # counter reset -> use the newer scrape as-is
    assert counter_delta(5.0, 8.0) == 3.0
    assert counter_delta(None, 8.0) == 8.0
    assert counter_delta(9.0, 8.0) == 8.0
    assert counter_delta(1.0, None) is None
