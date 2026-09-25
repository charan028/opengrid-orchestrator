"""Cross-process metric reads for the health evaluator (02b S6.4 cycle latency / guardian timeout rate /
reserve-breach counter). `og_control_tick_duration_seconds`, `og_guardian_verdicts_total` and
`og_reserve_breaches_total` live in `og-engine`'s and `og-guardian`'s own `prometheus_client` registries
(one per process, 02b S6.6) -- a different OS process from `og-settle`, so `health` reads them the same
way any Prometheus server would: an HTTP GET of that process's `/metrics` text exposition.
"""

from __future__ import annotations

import httpx

_METRIC_LINE_RE_COMMENT_PREFIXES = ("#",)


def parse_prometheus_text(text: str) -> dict[str, float]:
    """Minimal Prometheus text-exposition parser: returns `{"name{labels}": value}` (labels kept in the
    key, sorted, so callers can match e.g. `og_guardian_verdicts_total{outcome="timeout"}` exactly).
    Sufficient for `health`'s own metric reads; not a general Prometheus client."""
    samples: dict[str, float] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(_METRIC_LINE_RE_COMMENT_PREFIXES):
            continue
        name_and_labels, _, value_str = line.rpartition(" ")
        if not name_and_labels:
            continue
        try:
            value = float(value_str)
        except ValueError:
            continue
        samples[name_and_labels] = value
    return samples


def sum_metric(samples: dict[str, float], metric_name: str, *, label_filter: str | None = None) -> float:
    """Sum every sample whose key starts with `metric_name` and (if given) contains `label_filter`."""
    total = 0.0
    for key, value in samples.items():
        if not (key == metric_name or key.startswith(metric_name + "{") or key.startswith(metric_name + " ")):
            continue
        if label_filter is not None and label_filter not in key:
            continue
        total += value
    return total


def histogram_p99_from_buckets(samples: dict[str, float], histogram_name: str) -> float | None:
    """Approximate p99 (seconds) from a cumulative Prometheus histogram's `_bucket{le="..."}` samples --
    the smallest `le` whose cumulative count reaches 99% of the total observation count.

    Aggregates across every label combination (e.g. `og_control_tick_duration_seconds`'s `phase` label)
    into one overall figure -- sufficient for the health screen's single p99 number at MVP-S scale.
    """
    bucket_prefix = f"{histogram_name}_bucket{{"
    count_prefix = f"{histogram_name}_count"
    total = sum(
        value for key, value in samples.items() if key == count_prefix or key.startswith(count_prefix + "{")
    )
    if not total:
        return None
    bucket_totals: dict[float, float] = {}
    for key, cumulative_count in samples.items():
        if not key.startswith(bucket_prefix):
            continue
        le_marker = 'le="'
        start = key.find(le_marker)
        if start == -1:
            continue
        start += len(le_marker)
        end = key.find('"', start)
        le_str = key[start:end]
        le = float("inf") if le_str == "+Inf" else float(le_str)
        bucket_totals[le] = bucket_totals.get(le, 0.0) + cumulative_count
    buckets = sorted(bucket_totals.items())
    if not buckets:
        return None
    target = 0.99 * total
    for le, cumulative_count in buckets:
        if cumulative_count >= target:
            return None if le == float("inf") else le
    return None


async def scrape_metrics_text(url: str, *, timeout_s: float) -> str:
    """Fetch `url` (a process's `/metrics` endpoint). Raises `httpx.HTTPError` on failure -- the caller
    treats a scrape failure the same as a missing metric (health degrades, it does not crash, K7)."""
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text
