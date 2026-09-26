"""Pure Prometheus text-exposition parsing and histogram quantiles for the perf capture (WORKBOARD Q3).

No I/O. Parses the `/metrics` text format each og-* process serves from its own `prometheus_client`
registry (`orchestrator/src/opengrid/platform/metrics.py`, 02b S6.6) into typed samples, and computes
p50/p99 from cumulative `_bucket{le=...}` counts the same way PromQL's `histogram_quantile` does
(linear interpolation inside the bucket that crosses the rank).

This deliberately re-implements a tiny subset of `orchestrator/src/opengrid/health/metrics_scrape.py`
rather than importing it: tests-e2e must not depend on the product package (BUILD.md S4, qa owns
`tests-e2e/` only).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

_SAMPLE_RE = re.compile(
    r"^(?P<name>[A-Za-z_:][A-Za-z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+(?P<value>\S+)(?:\s+\S+)?$"
)
_LABEL_RE = re.compile(r'(?P<key>[A-Za-z_][A-Za-z0-9_]*)="(?P<value>(?:[^"\\]|\\.)*)"')

Labels = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class Sample:
    name: str
    labels: Labels
    value: float

    def label(self, key: str) -> str | None:
        return dict(self.labels).get(key)


@dataclass(frozen=True, slots=True)
class Histogram:
    """One (possibly label-aggregated) cumulative histogram. `buckets` is sorted by `le`, ascending, and
    always ends with the `+Inf` bucket when the exposition had one."""

    name: str
    buckets: tuple[tuple[float, float], ...]  # (le, cumulative count)
    sum: float
    count: float

    def quantile(self, q: float) -> float | None:
        return histogram_quantile(q, self.buckets)

    @property
    def mean(self) -> float | None:
        return self.sum / self.count if self.count else None


@dataclass(frozen=True, slots=True)
class Exposition:
    samples: tuple[Sample, ...] = field(default_factory=tuple)

    def select(self, name: str, labels: Mapping[str, str] | None = None) -> list[Sample]:
        """All samples named exactly `name` whose labels contain every pair in `labels`."""
        wanted = tuple((labels or {}).items())
        return [s for s in self.samples if s.name == name and all(pair in s.labels for pair in wanted)]

    def value(self, name: str, labels: Mapping[str, str] | None = None) -> float | None:
        """Sum of a gauge/counter across the matching series (`None` when absent)."""
        matched = self.select(name, labels)
        return sum(s.value for s in matched) if matched else None

    def histogram(self, name: str, labels: Mapping[str, str] | None = None) -> Histogram | None:
        """Aggregate `<name>_bucket/_sum/_count` across every series matching `labels` (e.g. all
        `phase` values of `og_control_tick_duration_seconds`, or just `{"phase": "total"}`)."""
        buckets: dict[float, float] = {}
        for s in self.select(f"{name}_bucket", labels):
            le_str = s.label("le")
            if le_str is None:
                continue
            le = _parse_le(le_str)
            buckets[le] = buckets.get(le, 0.0) + s.value
        if not buckets:
            return None
        total = self.value(f"{name}_sum", labels) or 0.0
        count = self.value(f"{name}_count", labels) or 0.0
        return Histogram(name=name, buckets=tuple(sorted(buckets.items())), sum=total, count=count)


def _parse_le(le_str: str) -> float:
    return math.inf if le_str == "+Inf" else float(le_str)


def _parse_labels(raw: str | None) -> Labels:
    if not raw:
        return ()
    pairs = [(m.group("key"), m.group("value").replace('\\"', '"')) for m in _LABEL_RE.finditer(raw)]
    return tuple(sorted(pairs))


def parse_exposition(text: str) -> Exposition:
    """Parse Prometheus text exposition. Comment/`# TYPE`/`# HELP` lines and malformed lines are skipped;
    a sample's trailing timestamp, if any, is ignored."""
    samples: list[Sample] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        m = _SAMPLE_RE.match(line)
        if m is None:
            continue
        try:
            value = float(m.group("value"))
        except ValueError:
            continue
        samples.append(Sample(m.group("name"), _parse_labels(m.group("labels")), value))
    return Exposition(tuple(samples))


def histogram_quantile(q: float, buckets: Iterable[tuple[float, float]]) -> float | None:
    """PromQL `histogram_quantile` over cumulative buckets: find the first bucket whose cumulative count
    reaches `q * total`, then interpolate linearly between its lower and upper bound. Returns the lower
    bound of the `+Inf` bucket when the rank lands there, `None` when there are no observations."""
    ordered = sorted(buckets)
    if not ordered or ordered[-1][0] != math.inf:
        return None
    total = ordered[-1][1]
    if total <= 0:
        return None
    rank = q * total
    for i, (le, cumulative) in enumerate(ordered):
        if cumulative < rank:
            continue
        if math.isinf(le):
            return ordered[i - 1][0] if i > 0 else None
        lower, prev_count = ordered[i - 1] if i > 0 else (0.0, 0.0)
        in_bucket = cumulative - prev_count
        if in_bucket <= 0 or le <= 0:
            return le
        return lower + (le - lower) * (rank - prev_count) / in_bucket
    return None


def histogram_delta(before: Histogram | None, after: Histogram) -> Histogram:
    """`after - before` bucket by bucket, so a quantile can be computed over one capture window instead of
    the process's whole lifetime. A missing/reset `before` (counter went down) yields `after` unchanged."""
    if before is None or before.count > after.count:
        return after
    prev = dict(before.buckets)
    buckets = tuple((le, max(0.0, c - prev.get(le, 0.0))) for le, c in after.buckets)
    return Histogram(after.name, buckets, after.sum - before.sum, after.count - before.count)


def counter_delta(before: float | None, after: float | None) -> float | None:
    if after is None:
        return None
    if before is None or before > after:
        return after
    return after - before
