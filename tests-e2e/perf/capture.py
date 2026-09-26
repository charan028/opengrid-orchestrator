"""Cycle-latency and UI-latency capture for the perf run (WORKBOARD Q3, TS-N).

Scrapes each og-* process's `/metrics` N times, samples the SSE stream the UI consumes, and writes a
Markdown PASS/FAIL report against the MVP-S targets (02b S6.4: RT cycle p99 < 500 ms at 2k hubs; 02b
S7.2: UI event-to-screen <= 2 s).

    python tests-e2e/perf/capture.py --report perf.md                       # live, dev stack defaults
    python tests-e2e/perf/capture.py --dry-run --report perf-dry.md         # no network, bundled sample

Port convention: every process serves `prometheus_client.start_http_server` on `metrics.bind_host`
(127.0.0.1) at `metrics.<process>_port`; `orchestrator/src/opengrid/guardian/main.py` uses 9103 and
`orchestrator/src/opengrid/health/metrics_scrape.py` reads them over plain HTTP GET. Override with
`--metrics name=url`. Pure logic (summaries, checks, report) is separate from the httpx I/O so it is
unit-tested in `test_capture.py` without a system.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any, Protocol

import httpx

from metrics import Exposition, Histogram, histogram_delta, parse_exposition

log = logging.getLogger("perf.capture")

HERE = Path(__file__).resolve().parent
SAMPLE_PATH = HERE / "sample_metrics.txt"
# 9103 is the only port grounded in code today (guardian/main.py); the rest follow the same
# `metrics.<process>_port` key and are the numbers the lead should pass once the processes expose them.
DEFAULT_METRICS = {"engine": "http://127.0.0.1:9102/metrics", "guardian": "http://127.0.0.1:9103/metrics"}
DEFAULT_API_BASE = "http://127.0.0.1:8080"
DEFAULT_SSE_PATH = "/og/api/stream/health"  # carries `as_of`, so true event-to-screen latency is measurable
FRAME_TIMESTAMP_KEYS = ("as_of", "ts", "issued_at", "evaluated_at")
TICK_HISTOGRAM = "og_control_tick_duration_seconds"
TICK_PHASE_TOTAL = {"phase": "total"}


@dataclass(frozen=True, slots=True)
class Targets:
    expected_hubs: int = 2000
    cycle_p99_budget_s: float = 0.5
    ui_latency_budget_s: float = 2.0
    min_hubs_ratio: float = 0.95  # the load must actually be there for the p99 number to mean anything


@dataclass(frozen=True, slots=True)
class SseStats:
    frames: int
    latencies_s: tuple[float, ...]
    mode: str  # "timestamp" (frame carries its own clock) | "gap" (inter-frame arrival) | "none"
    error: str | None = None

    @property
    def p99_s(self) -> float | None:
        return _quantile(self.latencies_s, 0.99)

    @property
    def max_s(self) -> float | None:
        return max(self.latencies_s) if self.latencies_s else None


@dataclass(frozen=True, slots=True)
class PerfSummary:
    scrapes: int
    scrape_failures: dict[str, int]
    hubs_by_state: dict[str, float]
    cycle: Histogram | None
    late_ticks: float | None
    eventloop_lag_p99_s: float | None
    fresh_ratio_min: float | None
    sse: SseStats

    @property
    def hub_total(self) -> float:
        return sum(self.hubs_by_state.values())


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    passed: bool
    detail: str


def _quantile(values: Iterable[float], q: float) -> float | None:
    ordered = sorted(values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    return statistics.quantiles(ordered, n=100, method="inclusive")[min(98, max(0, int(q * 100) - 1))]


def merge(expositions: Iterable[Exposition]) -> Exposition:
    return Exposition(tuple(s for e in expositions for s in e.samples))


def summarize(
    first: Exposition, last: Exposition, *, scrapes: int, failures: dict[str, int], sse: SseStats
) -> PerfSummary:
    """Reduce the first and last merged scrapes plus the SSE sample into the report's numbers. Histogram
    quantiles are taken over the capture window (last - first), not the process lifetime."""
    cycle_last = last.histogram(TICK_HISTOGRAM, TICK_PHASE_TOTAL) or last.histogram(TICK_HISTOGRAM)
    cycle = (
        histogram_delta(first.histogram(TICK_HISTOGRAM, TICK_PHASE_TOTAL), cycle_last) if cycle_last else None
    )
    lag_last = last.histogram("og_eventloop_lag_seconds")
    lag = histogram_delta(first.histogram("og_eventloop_lag_seconds"), lag_last) if lag_last else None
    fresh = [s.value for s in last.select("og_telemetry_fresh_ratio")]
    return PerfSummary(
        scrapes=scrapes,
        scrape_failures=failures,
        hubs_by_state={s.label("health") or "?": s.value for s in last.select("og_hubs")},
        cycle=cycle,
        late_ticks=last.value("og_control_ticks_total", {"outcome": "late"}),
        eventloop_lag_p99_s=lag.quantile(0.99) if lag else None,
        fresh_ratio_min=min(fresh) if fresh else None,
        sse=sse,
    )


def evaluate(summary: PerfSummary, targets: Targets) -> list[Check]:
    checks: list[Check] = []
    p99 = summary.cycle.quantile(0.99) if summary.cycle else None
    n = summary.cycle.count if summary.cycle else 0
    checks.append(
        Check(
            f"RT cycle p99 < {targets.cycle_p99_budget_s * 1000:.0f} ms",
            p99 is not None and p99 < targets.cycle_p99_budget_s,
            f"p99 = {_ms(p99)} over {n:.0f} cycles" if p99 is not None else "no cycle observations captured",
        )
    )
    hubs = summary.hub_total
    checks.append(
        Check(
            f"load present (>= {targets.min_hubs_ratio:.0%} of {targets.expected_hubs} hubs reporting)",
            hubs >= targets.expected_hubs * targets.min_hubs_ratio,
            f"og_hubs total = {hubs:.0f}",
        )
    )
    ui = summary.sse.p99_s
    checks.append(
        Check(
            f"UI latency <= {targets.ui_latency_budget_s:.1f} s ({summary.sse.mode})",
            ui is not None and ui <= targets.ui_latency_budget_s,
            f"p99 = {ui:.2f} s, max = {summary.sse.max_s:.2f} s over {summary.sse.frames} frames"
            if ui is not None
            else f"no frames measured ({summary.sse.error or 'stream empty'})",
        )
    )
    failed = sum(summary.scrape_failures.values())
    checks.append(Check("all scrapes succeeded", failed == 0, f"{failed} failed scrape(s)"))
    return checks


def _ms(seconds: float | None) -> str:
    return "n/a" if seconds is None else f"{seconds * 1000:.1f} ms"


def render_report(
    summary: PerfSummary, checks: list[Check], *, targets: Targets, meta: dict[str, Any]
) -> str:
    verdict = "PASS" if all(c.passed for c in checks) else "FAIL"
    lines = [
        f"# Perf capture -- {verdict}",
        "",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')}; "
        + ", ".join(f"{k}={v}" for k, v in meta.items()),
        "",
        "## Checks",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
        *(f"| {c.name} | {'PASS' if c.passed else 'FAIL'} | {c.detail} |" for c in checks),
        "",
        "## Control cycle (`og_control_tick_duration_seconds`, phase=total, capture window)",
        "",
    ]
    if summary.cycle:
        c = summary.cycle
        lines += [
            f"- cycles observed: {c.count:.0f}; mean {_ms(c.mean)}; p50 {_ms(c.quantile(0.5))}; "
            f"p99 {_ms(c.quantile(0.99))} (budget {targets.cycle_p99_budget_s * 1000:.0f} ms)",
            f'- late ticks (`og_control_ticks_total{{outcome="late"}}`): {summary.late_ticks}',
        ]
    else:
        lines.append("- no histogram found (is og-engine serving /metrics? see README)")
    lines += [
        f"- event-loop lag p99: {_ms(summary.eventloop_lag_p99_s)}",
        "",
        "## Fleet",
        "",
        f"- hubs by state (`og_hubs`): {json.dumps(summary.hubs_by_state, sort_keys=True)} "
        f"(expected {targets.expected_hubs})",
        f"- min `og_telemetry_fresh_ratio` across zones: {summary.fresh_ratio_min}",
        "",
        "## UI latency (SSE)",
        "",
        f"- mode: {summary.sse.mode}; frames: {summary.sse.frames}; p99 "
        f"{summary.sse.p99_s if summary.sse.p99_s is None else f'{summary.sse.p99_s:.2f} s'}; "
        f"max {summary.sse.max_s if summary.sse.max_s is None else f'{summary.sse.max_s:.2f} s'}",
        "- `timestamp` = wall clock minus the frame's own `as_of`; `gap` = inter-frame arrival interval "
        "(upper bound on staleness when the frame carries no clock)",
        "",
        "## Scrapes",
        "",
        f"- {summary.scrapes} scrape rounds; failures per endpoint: {json.dumps(summary.scrape_failures)}",
        "",
    ]
    return "\n".join(lines)


# --- SSE frame timing (pure) -------------------------------------------------------------------------


def frame_timestamp(payload: Any) -> float | None:
    """Epoch seconds of the frame's own clock field, if it has one (`as_of` on the health stream)."""
    if not isinstance(payload, dict):
        return None
    for key in FRAME_TIMESTAMP_KEYS:
        raw = payload.get(key)
        if isinstance(raw, str):
            try:
                return datetime.fromisoformat(raw).timestamp()
            except ValueError:
                continue
    return None


def sse_stats(arrivals: list[tuple[float, Any]], *, error: str | None = None) -> SseStats:
    """`arrivals` = (wall-clock epoch seconds when the frame arrived, decoded JSON payload)."""
    stamped = [(t, frame_timestamp(p)) for t, p in arrivals]
    if stamped and all(ts is not None for _, ts in stamped):
        lat = tuple(max(0.0, t - ts) for t, ts in stamped if ts is not None)
        return SseStats(len(arrivals), lat, "timestamp", error)
    if len(arrivals) >= 2:
        gaps = tuple(b[0] - a[0] for a, b in pairwise(arrivals))
        return SseStats(len(arrivals), gaps, "gap", error)
    return SseStats(len(arrivals), (), "none", error)


def iter_sse_data(lines: Iterable[str]) -> Iterator[str]:
    """Yield the concatenated `data:` payload of each SSE event; comments (`: ping`) are skipped."""
    buf: list[str] = []
    for line in lines:
        if line.startswith("data:"):
            buf.append(line[5:].lstrip())
        elif line == "" and buf:
            yield "\n".join(buf)
            buf = []


# --- I/O ------------------------------------------------------------------------------------------------


class MetricsSource(Protocol):
    def scrape(self, endpoints: dict[str, str]) -> tuple[Exposition, dict[str, int]]: ...

    def sse(self, url: str, frames: int) -> SseStats: ...


@dataclass
class HttpSource:
    client: httpx.Client
    clock: Callable[[], float] = time.time

    def scrape(self, endpoints: dict[str, str]) -> tuple[Exposition, dict[str, int]]:
        parts: list[Exposition] = []
        failures: dict[str, int] = {}
        for name, url in endpoints.items():
            try:
                r = self.client.get(url)
                r.raise_for_status()
                parts.append(parse_exposition(r.text))
            except httpx.HTTPError as exc:
                log.warning("scrape %s failed: %s", name, exc)
                failures[name] = 1
        return merge(parts), failures

    def sse(self, url: str, frames: int) -> SseStats:
        arrivals: list[tuple[float, Any]] = []
        try:
            with self.client.stream("GET", url, headers={"Accept": "text/event-stream"}) as r:
                r.raise_for_status()
                for data in iter_sse_data(r.iter_lines()):
                    arrivals.append((self.clock(), json.loads(data)))
                    if len(arrivals) >= frames:
                        break
        except (httpx.HTTPError, json.JSONDecodeError) as exc:
            log.warning("sse %s failed after %d frames: %s", url, len(arrivals), exc)
            return sse_stats(arrivals, error=str(exc))
        return sse_stats(arrivals)


@dataclass
class DryRunSource:
    """First scrape is an empty exposition (process just started), every later one is the bundled sample,
    so the window delta equals the sample. SSE frames arrive 1 s apart, each stamped 0.4 s earlier."""

    sample_text: str = field(default_factory=lambda: SAMPLE_PATH.read_text(encoding="utf-8"))
    calls: int = 0

    def scrape(self, endpoints: dict[str, str]) -> tuple[Exposition, dict[str, int]]:
        self.calls += 1
        return (Exposition() if self.calls == 1 else parse_exposition(self.sample_text)), {}

    def sse(self, url: str, frames: int) -> SseStats:
        t0 = 1_700_000_000.0
        arrivals = [
            (t0 + i, {"as_of": datetime.fromtimestamp(t0 + i - 0.4, UTC).isoformat()}) for i in range(frames)
        ]
        return sse_stats(arrivals)


def run_capture(
    source: MetricsSource,
    *,
    endpoints: dict[str, str],
    samples: int,
    interval_s: float,
    sse_url: str,
    sse_frames: int,
    sleep: Callable[[float], None],
) -> PerfSummary:
    first: Exposition | None = None
    last = Exposition()
    failures: dict[str, int] = {}
    for i in range(samples):
        last, failed = source.scrape(endpoints)
        for k, v in failed.items():
            failures[k] = failures.get(k, 0) + v
        first = last if first is None else first
        if i + 1 < samples:
            sleep(interval_s)
    sse = source.sse(sse_url, sse_frames) if sse_frames > 0 else SseStats(0, (), "none", "disabled")
    return summarize(first or Exposition(), last, scrapes=samples, failures=failures, sse=sse)


def _parse_endpoint(arg: str) -> tuple[str, str]:
    name, sep, url = arg.partition("=")
    if not sep or not url.startswith("http"):
        raise argparse.ArgumentTypeError(f"expected name=http://host:port/metrics, got {arg!r}")
    return name, url


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--report", type=Path, required=True, help="Markdown report path")
    p.add_argument("--dry-run", action="store_true", help="no network; use the bundled sample exposition")
    p.add_argument(
        "--metrics",
        type=_parse_endpoint,
        action="append",
        metavar="NAME=URL",
        help=f"metrics endpoint (repeatable); default {DEFAULT_METRICS}",
    )
    p.add_argument("--api-base", default=DEFAULT_API_BASE)
    p.add_argument("--sse-path", default=DEFAULT_SSE_PATH)
    p.add_argument("--sse-frames", type=int, default=10, help="0 disables the UI-latency sample")
    p.add_argument("--remote-user", default="viewer", help="X-Remote-User for the SSE stream (api/auth.py)")
    p.add_argument("--samples", type=int, default=30, help="number of /metrics scrape rounds")
    p.add_argument("--interval-s", type=float, default=2.0)
    p.add_argument("--timeout-s", type=float, default=5.0, help="per-request timeout")
    defaults = Targets()
    p.add_argument("--expected-hubs", type=int, default=defaults.expected_hubs)
    p.add_argument("--cycle-p99-budget-ms", type=float, default=defaults.cycle_p99_budget_s * 1000)
    p.add_argument("--ui-latency-budget-s", type=float, default=defaults.ui_latency_budget_s)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    endpoints = dict(args.metrics) if args.metrics else dict(DEFAULT_METRICS)
    targets = Targets(args.expected_hubs, args.cycle_p99_budget_ms / 1000, args.ui_latency_budget_s)
    sse_url = f"{args.api_base.rstrip('/')}{args.sse_path}"
    meta = {"mode": "dry-run" if args.dry_run else "live", "endpoints": json.dumps(endpoints), "sse": sse_url}

    def _run(source: MetricsSource, sleep: Callable[[float], None]) -> PerfSummary:
        return run_capture(
            source,
            endpoints=endpoints,
            samples=max(2, args.samples) if args.dry_run else args.samples,
            interval_s=args.interval_s,
            sse_url=sse_url,
            sse_frames=args.sse_frames,
            sleep=sleep,
        )

    if args.dry_run:
        summary = _run(DryRunSource(), lambda _s: None)
    else:
        # SSE frames arrive every 1-2 s; the read timeout must outlast the api's 15 s comment heartbeat.
        timeout = httpx.Timeout(args.timeout_s, read=max(args.timeout_s, 20.0))
        with httpx.Client(timeout=timeout, headers={"X-Remote-User": args.remote_user}) as client:
            summary = _run(HttpSource(client), time.sleep)

    checks = evaluate(summary, targets)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(summary, checks, targets=targets, meta=meta), encoding="utf-8")
    verdict = "PASS" if all(c.passed for c in checks) else "FAIL"
    sys.stdout.write(f"{verdict} -- report written to {args.report}\n")
    return 0 if verdict == "PASS" or args.dry_run else 1


if __name__ == "__main__":
    sys.exit(main())
