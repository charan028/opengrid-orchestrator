#!/usr/bin/env python3
"""tests-perf/analyze.py -- summarise a campaign's samples into results.json, Markdown tables and charts.

    python tests-perf/analyze.py --run /srv/ogwork/perf-run [--charts DIR] [--md FILE]

Inputs (all under <run>/data): samples.jsonl (sampler.py), stages.tsv (campaign.sh), guard.jsonl and
../aborts.jsonl (watchdog.sh), stress-*.json (stress.py). Windows are the sampler records whose stage is
`step-<homes>` (the measured steady state) and `step-`/`soak-<largest>` (the soak). Cumulative counters and
histograms are differenced between the first and last record of a window.

Charts need matplotlib (tests-perf/requirements-charts.txt, a separate venv); without it only the JSON and
Markdown are written.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean, median
from typing import Any

BUDGET_MS = 500.0
TELEMETRY_INTERVAL_S = 10.0
PROCS = (
    "engine",
    "guardian",
    "api",
    "settle",
    "feeds",
    "safestop",
    "mosquitto",
    "postgres",
    "sim-fleet",
    "sim-scada",
    "sim-market",
)
API_NAMES = ("health", "fleet_table", "fleet_table_soc100", "fleet_map", "alerts", "ui_fleet")
LE_RE = re.compile(r'le="([^"]+)"')


# --- pure helpers (unit-tested) ----------------------------------------------------------------------------


def _value_or_nan(value: Any) -> float:
    """A chart value: a missing measurement plots as a gap (NaN), never as 0."""
    return math.nan if value is None else float(value)


def pct(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..100)."""
    if not values:
        return None
    s = sorted(values)
    return s[max(1, math.ceil(q / 100.0 * len(s))) - 1]


def buckets(metrics: dict[str, float], name: str, label: str = "") -> list[tuple[float, float]]:
    """Cumulative (le, count) pairs of one histogram series, sorted by le. `label` filters, e.g. phase="total"."""
    out = []
    for key, value in metrics.items():
        if key.startswith(name + "_bucket") and (not label or label in key):
            m = LE_RE.search(key)
            if m:
                out.append((math.inf if m.group(1) == "+Inf" else float(m.group(1)), value))
    return sorted(out)


def bucket_delta(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> list[tuple[float, float]]:
    before = dict(a)
    return [(le, max(0.0, c - before.get(le, 0.0))) for le, c in b]


def hist_quantile(q: float, cum: list[tuple[float, float]]) -> float | None:
    """PromQL histogram_quantile over cumulative buckets (linear interpolation inside the bucket)."""
    if not cum or cum[-1][1] <= 0:
        return None
    total = cum[-1][1]
    rank = q * total
    prev_le, prev_c = 0.0, 0.0
    for le, c in cum:
        if c >= rank:
            if math.isinf(le):
                return prev_le
            if c == prev_c:
                return le
            return prev_le + (le - prev_le) * (rank - prev_c) / (c - prev_c)
        prev_le, prev_c = le, c
    return prev_le


def slope_per_hour(points: list[tuple[float, float]]) -> float | None:
    """Least-squares slope of (t_seconds, value) in value per hour."""
    if len(points) < 3:
        return None
    ts = [p[0] for p in points]
    vs = [p[1] for p in points]
    mt, mv = mean(ts), mean(vs)
    den = sum((t - mt) ** 2 for t in ts)
    if den == 0:
        return None
    return sum((t - mt) * (v - mv) for t, v in zip(ts, vs, strict=True)) / den * 3600.0


def fit_knee(sizes: list[float], p99: list[float], budget: float = BUDGET_MS) -> dict[str, Any]:
    """Quadratic least-squares fit of p99 vs hubs; where it crosses the budget, and the 10k projection."""
    n = len(sizes)
    if n < 3:
        return {}
    # normal equations for a + b x + c x^2 (x in thousands of hubs)
    xs = [s / 1000.0 for s in sizes]
    sx = [sum(x**k for x in xs) for k in range(5)]
    sy = [sum((x**k) * y for x, y in zip(xs, p99, strict=True)) for k in range(3)]
    m = [[sx[i + j] for j in range(3)] + [sy[i]] for i in range(3)]
    for i in range(3):  # Gauss-Jordan
        piv = max(range(i, 3), key=lambda r: abs(m[r][i]))
        m[i], m[piv] = m[piv], m[i]
        if abs(m[i][i]) < 1e-12:
            return {}
        m[i] = [v / m[i][i] for v in m[i]]
        for r in range(3):
            if r != i:
                factor = m[r][i]
                m[r] = [u - factor * w for u, w in zip(m[r], m[i], strict=True)]
    a, b, c = (m[i][3] for i in range(3))

    def fitted(x: float) -> float:
        return a + b * x + c * x * x

    # smallest positive root of c x^2 + b x + (a - budget) = 0, in thousands of hubs
    roots: list[float] = []
    if abs(c) < 1e-9:
        if abs(b) > 1e-12:
            roots = [(budget - a) / b]
    else:
        disc = b * b - 4 * c * (a - budget)
        if disc >= 0:
            roots = [(-b + s * math.sqrt(disc)) / (2 * c) for s in (1.0, -1.0)]
    positive = sorted(r for r in roots if r > 0)
    cross = round(positive[0] * 1000) if positive else None
    return {
        "a": a,
        "b_per_k": b,
        "c_per_k2": c,
        "p99_at_10k_ms": round(fitted(10.0), 1),
        "budget_crossing_hubs": cross,
    }


# --- loading -------------------------------------------------------------------------------------------------


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def ts_s(rec: dict[str, Any]) -> float:
    from datetime import datetime

    return datetime.fromisoformat(rec["ts"]).timestamp()


def window_summary(recs: list[dict[str, Any]], expected_hubs: int) -> dict[str, Any]:
    first, last = recs[0], recs[-1]
    dt = ts_s(last) - ts_s(first)
    out: dict[str, Any] = {
        "samples": len(recs),
        "duration_min": round(dt / 60.0, 1),
        "expected_hubs": expected_hubs,
    }

    # engine cycle: histogram delta (all ticks in the window) + CYCLE_LATENCY trace summaries
    e0, e1 = first.get("engine") or {}, last.get("engine") or {}
    tick = bucket_delta(
        buckets(e0, "og_control_tick_duration_seconds", 'phase="total"'),
        buckets(e1, "og_control_tick_duration_seconds", 'phase="total"'),
    )
    out["ticks"] = tick[-1][1] if tick else 0
    for q in (50, 95, 99):
        v = hist_quantile(q / 100.0, tick)
        out[f"hist_p{q}_ms"] = round(v * 1000.0, 1) if v is not None else None
    cyc = [c for r in recs for c in (r.get("db") or {}).get("cycle_latency", [])]
    if cyc:
        out["trace_p50_ms"] = round(median(float(c["p50_ms"]) for c in cyc), 1)
        out["trace_p99_ms"] = max(float(c["p99_ms"]) for c in cyc)
        out["trace_max_ms"] = max(float(c["max_ms"]) for c in cyc)
        phases: dict[str, float] = defaultdict(float)
        for c in cyc:
            for k, v in (c.get("phase_p99_ms") or {}).items():
                phases[k] = max(phases[k], float(v))
        out["phase_p99_ms"] = dict(sorted(phases.items(), key=lambda kv: -kv[1]))
        out["loop_lag_p99_ms"] = max(float(c.get("loop_lag_p99_ms") or 0) for c in cyc)
        out["loop_lag_max_ms"] = max(float(c.get("loop_lag_max_ms") or 0) for c in cyc)
    late0 = e0.get('og_control_ticks_total{outcome="late"}', 0.0)
    out["late_ticks"] = e1.get('og_control_ticks_total{outcome="late"}', 0.0) - late0

    lag = bucket_delta(
        buckets(e0, "og_engine_mqtt_ingest_lag_seconds"), buckets(e1, "og_engine_mqtt_ingest_lag_seconds")
    )
    for q in (50, 99):
        v = hist_quantile(q / 100.0, lag)
        out[f"ingest_lag_p{q}_s"] = round(v, 3) if v is not None else None

    # guardian verdicts (per-sample aggregates from the trace)
    vs = [(r.get("db") or {}).get("verdicts") or {} for r in recs[1:]]
    vs = [v for v in vs if v.get("n")]
    out["verdicts"] = int(sum(v["n"] for v in vs))
    out["verdict_p50_ms"] = round(median(v["p50_ms"] for v in vs), 1) if vs else None
    out["verdict_p99_ms_max"] = max((v["p99_ms"] for v in vs), default=None)
    out["verdict_max_ms"] = max((v["max_ms"] for v in vs), default=None)
    out["g20_timeouts"] = int(sum(v.get("timeouts") or 0 for v in vs))
    out["verdicts_per_min"] = round(out["verdicts"] / (dt / 60.0), 1) if dt else None

    # dispatch load: home banks with grants per engine cycle (what separates DELIVERING from IDLE)
    gs = [(r.get("db") or {}).get("grants") or {} for r in recs[1:]]
    cycles = sum(float(g.get("cycles") or 0) for g in gs)
    bank_cycles = sum(float(g.get("bank_cycles") or 0) for g in gs)
    grant_rows = sum(float(g.get("rows") or 0) for g in gs)
    miscs = [(r.get("db") or {}).get("misc") or {} for r in recs]
    home_banks = next((int(m["home_banks"]) for m in reversed(miscs) if m.get("home_banks")), None)
    out["home_banks"] = home_banks
    out["banks_per_cycle"] = round(bank_cycles / cycles, 1) if cycles else 0.0
    out["bank_share"] = round(out["banks_per_cycle"] / home_banks, 3) if home_banks else None
    out["grant_rows_per_cycle"] = round(grant_rows / cycles, 1) if cycles else 0.0

    # telemetry ingest and freshness
    d0, d1 = first.get("db") or {}, last.get("db") or {}
    tel = (d1.get("tel", {}).get("ins", 0) - d0.get("tel", {}).get("ins", 0)) / dt if dt else 0
    out["telemetry_rows_s"] = round(tel, 1)
    out["telemetry_expected_s"] = round(expected_hubs / TELEMETRY_INTERVAL_S, 1)
    fresh = [(r.get("db") or {}).get("hubs", {}).get("fresh") for r in recs]
    fresh = [f for f in fresh if f is not None]
    out["fresh_min"] = min(fresh) if fresh else None
    out["fresh_mean"] = round(mean(fresh), 1) if fresh else None

    # broker
    def sysval(r: dict[str, Any], k: str) -> float:
        try:
            return float((r.get("broker") or {}).get(k, "nan"))
        except ValueError:
            return math.nan

    for k, name in (("messages/received", "mqtt_in_s"), ("messages/sent", "mqtt_out_s")):
        m0, m1 = sysval(first, k), sysval(last, k)
        ok = dt and not math.isnan(m0) and not math.isnan(m1) and m1 >= m0
        out[name] = round((m1 - m0) / dt, 1) if ok else None
    out["mqtt_clients"] = sysval(last, "clients/connected")

    # Postgres
    db0, db1 = d0.get("db", {}), d1.get("db", {})
    if db0 and db1 and dt:
        out["pg_xact_s"] = round((db1["xact_commit"] - db0["xact_commit"]) / dt, 1)
        out["pg_ins_s"] = round((db1["tup_inserted"] - db0["tup_inserted"]) / dt, 1)
        out["pg_upd_s"] = round((db1["tup_updated"] - db0["tup_updated"]) / dt, 1)
        out["wal_mb_h"] = round((db1["wal_bytes"] - db0["wal_bytes"]) / dt * 3600 / 1e6, 1)
        out["db_growth_mb_h"] = round((db1["db_bytes"] - db0["db_bytes"]) / dt * 3600 / 1e6, 1)
        out["db_size_mb"] = round(db1["db_bytes"] / 1e6, 1)
    t0 = {t["relname"]: t["bytes"] for t in d0.get("tables", [])}
    growth = (
        {
            t["relname"]: round((t["bytes"] - t0.get(t["relname"], 0)) / dt * 3600 / 1e6, 2)
            for t in d1.get("tables", [])
        }
        if dt
        else {}
    )
    out["table_growth_mb_h"] = dict(sorted(growth.items(), key=lambda kv: -kv[1])[:8])

    # disks (base: pgstandby/pgdata/sda; compose: the device the Postgres container writes to, "db_disk")
    disks: dict[str, Any] = {}
    for dev in sorted((last.get("disk") or {}).keys()):
        s0, s1 = (first.get("disk") or {}).get(dev), (last.get("disk") or {}).get(dev)
        if s0 and s1 and dt:
            w = s1["writes"] - s0["writes"]
            disks[dev] = {
                "util_pct": round((s1["io_ms"] - s0["io_ms"]) / (dt * 1000) * 100, 1),
                "w_s": round(w / dt, 1),
                "wkb_s": round((s1["sectors_w"] - s0["sectors_w"]) * 0.5 / dt, 1),
                "w_await_ms": round((s1["write_ms"] - s0["write_ms"]) / w, 1) if w else 0.0,
            }
    out["disks"] = disks

    # processes (base: MainPID; compose: container id)
    procs: dict[str, Any] = {}
    for p in PROCS:
        p0, p1 = (first.get("procs") or {}).get(p), (last.get("procs") or {}).get(p)
        rss: list[float] = [
            float(x) for r in recs if (x := ((r.get("procs") or {}).get(p) or {}).get("rss_mb"))
        ]
        if p0 and p1 and dt and p0.get("pid") == p1.get("pid"):
            procs[p] = {
                "cpu_pct": round((p1["cpu_s"] - p0["cpu_s"]) / dt * 100, 1),
                "rss_mb_max": round(max(rss), 1),
            }
        elif rss:
            procs[p] = {"cpu_pct": None, "rss_mb_max": round(max(rss), 1), "note": "restarted in window"}
    out["procs"] = procs
    h0, h1 = first.get("host") or {}, last.get("host") or {}
    if h0 and h1:
        tot = h1["cpu_total_ticks"] - h0["cpu_total_ticks"]
        out["host_cpu_pct"] = (
            round((1 - (h1["cpu_idle_ticks"] - h0["cpu_idle_ticks"]) / tot) * 100, 1) if tot else None
        )
        out["host_mem_available_min_mb"] = round(
            min((r.get("host") or {}).get("mem_available_mb", 1e9) for r in recs)
        )

    # API latency
    api: dict[str, Any] = {}
    for name in API_NAMES:
        ms = [x for r in recs for x in ((r.get("api") or {}).get(name) or {}).get("ms", [])]
        codes: dict[str, int] = defaultdict(int)
        for r in recs:
            for c, n in (((r.get("api") or {}).get(name) or {}).get("codes") or {}).items():
                codes[c] += n
        if ms:
            api[name] = {
                "n": len(ms),
                "p50_ms": pct(ms, 50),
                "p95_ms": pct(ms, 95),
                "max_ms": max(ms),
                "codes": dict(codes),
            }
    out["api"] = api
    return out


def soak_summary(recs: list[dict[str, Any]]) -> dict[str, Any]:
    t0 = ts_s(recs[0])
    out: dict[str, Any] = {"duration_min": round((ts_s(recs[-1]) - t0) / 60.0, 1)}
    for p in PROCS:
        pts = [(ts_s(r) - t0, ((r.get("procs") or {}).get(p) or {}).get("rss_mb")) for r in recs]
        pts2 = [(t, v) for t, v in pts if v]
        if len(pts2) < 3:
            continue
        head = [v for t, v in pts2 if t < 600] or [pts2[0][1]]
        tail = [v for t, v in pts2 if t > pts2[-1][0] - 600] or [pts2[-1][1]]
        out[p] = {
            "rss_start_mb": round(mean(head), 1),
            "rss_end_mb": round(mean(tail), 1),
            "slope_mb_h": round(slope_per_hour(pts2) or 0.0, 2),
            "pids": sorted({((r.get("procs") or {}).get(p) or {}).get("pid") for r in recs} - {None}),
        }
    cyc = [c for r in recs for c in (r.get("db") or {}).get("cycle_latency", [])]
    out["trace_p99_ms_series"] = [(c["ts"], c["p99_ms"]) for c in cyc]
    return out


def analyze(run: Path) -> dict[str, Any]:
    data = run / "data"
    samples = load_jsonl(data / "samples.jsonl")
    by_stage: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in samples:
        by_stage[r.get("stage", "")].append(r)
    steps: dict[int, dict[str, Any]] = {}  # IDLE: `step-<homes>`
    deliver: dict[int, dict[str, Any]] = {}  # DELIVERING: `deliver-<homes>`
    for stage, recs in by_stage.items():
        m = re.fullmatch(r"(step|deliver)-(\d+)", stage)
        if m and len(recs) >= 3:
            homes = int(m.group(2))
            (steps if m.group(1) == "step" else deliver)[homes] = window_summary(recs, homes + 9)
    result: dict[str, Any] = {
        "steps": {str(k): steps[k] for k in sorted(steps)},
        "deliver": {str(k): deliver[k] for k in sorted(deliver)},
        "dispatch": {
            p.stem.removeprefix("dispatch-"): json.loads(p.read_text())
            for p in sorted(data.glob("dispatch-*.json"))
        },
    }
    loaded = deliver or steps  # the soak runs in the DELIVERING regime when there is one
    if loaded:
        largest = max(loaded)
        head = "deliver" if deliver else "step"
        soak = by_stage.get(f"{head}-{largest}", []) + by_stage.get(f"soak-{largest}", [])
        if len(soak) >= 3:
            result["soak"] = soak_summary(soak)
    for key, regime in (("knee", loaded), ("knee_idle", steps)):
        sizes = sorted(regime)
        p99s = [regime[s].get("trace_p99_ms") or regime[s].get("hist_p99_ms") for s in sizes]
        if len(sizes) >= 3 and all(p is not None for p in p99s):
            result[key] = fit_knee([s + 9.0 for s in sizes], [float(p) for p in p99s if p is not None])
    result["knee_regime"] = "delivering" if deliver else "idle"
    result["stress"] = {
        p.stem.removeprefix("stress-"): json.loads(p.read_text()) for p in sorted(data.glob("stress-*.json"))
    }
    guard = load_jsonl(data / "guard.jsonl")
    result["guard"] = {
        "samples": len(guard),
        "breach_samples": sum(1 for g in guard if g.get("breach")),
        "prod_p99_max_ms": max(
            (g["prod_p99_ms"] for g in guard if g.get("prod_p99_ms") is not None), default=None
        ),
        "pgdata_util_max": max(
            (g["pgdata_util"] for g in guard if g.get("pgdata_util") is not None), default=None
        ),
    }
    result["aborts"] = load_jsonl(run / "aborts.jsonl")
    stages = (data / "stages.tsv").read_text().splitlines() if (data / "stages.tsv").exists() else []
    result["stages"] = [line.split("\t") for line in stages]
    return result


# --- Markdown -------------------------------------------------------------------------------------------------


def fmt(v: Any, nd: int = 0) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return f"{v:,.{nd}f}"
    return f"{v:,}" if isinstance(v, int) else str(v)


def markdown(res: dict[str, Any]) -> str:
    """One table per regime: IDLE (`step-<homes>`) and, when measured, DELIVERING (`deliver-<homes>`)."""
    if not res.get("deliver"):
        return table(res["steps"])
    return (
        "#### IDLE: between delivery windows (no committed obligation delivering)\n\n"
        + table(res["steps"])
        + "\n#### DELIVERING: committed obligations on about half the home banks\n\n"
        + table(res["deliver"])
    )


def table(steps: dict[str, Any]) -> str:
    cols = list(steps)
    hubs = [steps[c]["expected_hubs"] for c in cols]
    lines = ["| Metric | " + " | ".join(f"{h:,} hubs" for h in hubs) + " |", "|---|" + "---:|" * len(cols)]

    def row(label: str, key: str | tuple[str, ...], nd: int = 0) -> None:
        vals = []
        for c in cols:
            v: Any = steps[c]
            for k in key if isinstance(key, tuple) else (key,):
                v = v.get(k) if isinstance(v, dict) else None
            vals.append(fmt(v, nd))
        lines.append(f"| {label} | " + " | ".join(vals) + " |")

    row("Engine cycle p50 (ms, trace)", "trace_p50_ms", 1)
    row("Engine cycle p95 (ms, histogram)", "hist_p95_ms", 0)
    row("Engine cycle p99 (ms, trace)", "trace_p99_ms", 1)
    row("Engine cycle max (ms, trace)", "trace_max_ms", 1)
    row("Late ticks", "late_ticks")
    row("Event-loop lag p99 (ms)", "loop_lag_p99_ms", 1)
    row("Home banks with grants per cycle", "banks_per_cycle", 1)
    row("Share of home banks with grants", "bank_share", 2)
    row("Grant rows per cycle", "grant_rows_per_cycle", 1)
    row("Guardian verdicts", "verdicts")
    row("Verdicts per minute", "verdicts_per_min", 1)
    row("Verdict latency p50 (ms)", "verdict_p50_ms", 1)
    row("Verdict latency p99, worst 15 s (ms)", "verdict_p99_ms_max", 1)
    row("G-20 timeouts", "g20_timeouts")
    row("Telemetry rows/s (expected)", "telemetry_rows_s", 1)
    row("Telemetry expected rows/s", "telemetry_expected_s", 1)
    row("Ingest lag p50 (s)", "ingest_lag_p50_s", 2)
    row("Ingest lag p99 (s)", "ingest_lag_p99_s", 2)
    row("Fresh hubs, min", "fresh_min")
    row("MQTT in msgs/s", "mqtt_in_s", 1)
    row("MQTT out msgs/s", "mqtt_out_s", 1)
    row("Broker CPU %", ("procs", "mosquitto", "cpu_pct"), 1)
    row("PG commits/s", "pg_xact_s", 1)
    row("PG rows inserted/s", "pg_ins_s", 1)
    row("PG rows updated/s", "pg_upd_s", 1)
    row("WAL MB/h", "wal_mb_h", 1)
    row("DB growth MB/h", "db_growth_mb_h", 1)
    dev = "db_disk" if any("db_disk" in (steps[c].get("disks") or {}) for c in cols) else "pgstandby"
    row(f"DB disk util % ({dev})", ("disks", dev, "util_pct"), 1)
    row(f"DB disk w_await ms ({dev})", ("disks", dev, "w_await_ms"), 1)
    row(f"DB disk write kB/s ({dev})", ("disks", dev, "wkb_s"), 0)
    for p in ("engine", "guardian", "api", "settle", "feeds", "postgres", "sim-fleet"):
        row(f"{p} CPU %", ("procs", p, "cpu_pct"), 1)
        row(f"{p} RSS max (MB)", ("procs", p, "rss_mb_max"), 0)
    row("Host CPU %", "host_cpu_pct", 1)
    for name in API_NAMES:
        row(f"API p95 {name} (ms)", ("api", name, "p95_ms"), 1)
    return "\n".join(lines) + "\n"


# --- charts ---------------------------------------------------------------------------------------------------


def charts(res: dict[str, Any], out: Path) -> list[str]:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update(
        {"font.size": 10, "axes.spines.top": False, "axes.spines.right": False, "svg.hashsalt": "og"}
    )
    ink, grid = "#1f2933", "#d9dee3"
    series_c = ["#2563eb", "#0d9488", "#d97706", "#9333ea", "#dc2626", "#64748b"]
    written = []
    idle = res["steps"]
    # Charts 2-5 show the loaded regime (DELIVERING when measured); chart 1 shows both.
    steps = res.get("deliver") or idle
    hubs = [steps[c]["expected_hubs"] for c in steps]
    idle_hubs = [idle[c]["expected_hubs"] for c in idle]
    regime = "delivering" if res.get("deliver") else "idle"

    def save(fig: Any, name: str) -> None:
        fig.tight_layout()
        path = out / name
        fig.savefig(path, format="svg", metadata={"Date": None})
        plt.close(fig)
        written.append(str(path))

    # 1 cycle latency vs size
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    for i, (key, label) in enumerate(
        (
            ("trace_p50_ms", "p50"),
            ("hist_p95_ms", "p95 (histogram)"),
            ("trace_p99_ms", "p99"),
            ("trace_max_ms", "max"),
        )
    ):
        ys = [_value_or_nan(steps[c].get(key)) for c in steps]
        ax.plot(hubs, ys, marker="o", color=series_c[i], label=f"{label} ({regime})")
    if res.get("deliver") and idle:
        ax.plot(
            idle_hubs,
            [_value_or_nan(idle[c].get("trace_p99_ms")) for c in idle],
            marker="o",
            linestyle=":",
            color=series_c[2],
            label="p99 (idle)",
        )
    ax.axhline(BUDGET_MS, color="#dc2626", linestyle="--", linewidth=1)
    ax.text(hubs[0], BUDGET_MS * 1.03, "500 ms budget", color="#dc2626", fontsize=9)
    ax.set_xlabel("fleet size (hubs)")
    ax.set_ylabel("og-engine cycle (ms)")
    ax.set_title("Engine cycle latency vs fleet size", color=ink, loc="left")
    ax.grid(axis="y", color=grid)
    ax.legend(frameon=False)
    save(fig, "cycle-latency-vs-fleet.svg")

    # 2 phase p99 stacked
    phases = sorted(
        {k for c in steps for k in (steps[c].get("phase_p99_ms") or {})},
        key=lambda k: -max((steps[c].get("phase_p99_ms") or {}).get(k, 0) for c in steps),
    )[:6]
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    bottom = [0.0] * len(hubs)
    xs = list(range(len(hubs)))
    for i, ph in enumerate(phases):
        ys = [(steps[c].get("phase_p99_ms") or {}).get(ph, 0.0) for c in steps]
        ax.bar(xs, ys, bottom=bottom, color=series_c[i % len(series_c)], label=ph, width=0.6)
        bottom = [b + y for b, y in zip(bottom, ys, strict=True)]
    ax.set_xticks(xs, [f"{h:,}" for h in hubs])
    ax.set_xlabel("fleet size (hubs)")
    ax.set_ylabel("per-phase p99 (ms, stacked)")
    ax.set_title("Where the cycle goes: per-phase p99", color=ink, loc="left")
    ax.legend(frameon=False, fontsize=8)
    save(fig, "cycle-phases-vs-fleet.svg")

    # 3 CPU per process
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    for i, p in enumerate(("engine", "guardian", "api", "settle", "sim-fleet", "mosquitto")):
        ys = [_value_or_nan(((steps[c].get("procs") or {}).get(p) or {}).get("cpu_pct")) for c in steps]
        ax.plot(hubs, ys, marker="o", color=series_c[i], label=p)
    ax.axhline(100, color=grid, linestyle="--")
    ax.set_xlabel("fleet size (hubs)")
    ax.set_ylabel("CPU % of one core")
    ax.set_title("CPU per process", color=ink, loc="left")
    ax.grid(axis="y", color=grid)
    ax.legend(frameon=False, fontsize=8)
    save(fig, "cpu-per-process-vs-fleet.svg")

    # 4 DB write load
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    ax.plot(hubs, [steps[c].get("wal_mb_h") for c in steps], marker="o", color=series_c[0], label="WAL MB/h")
    ax.plot(
        hubs,
        [steps[c].get("db_growth_mb_h") for c in steps],
        marker="o",
        color=series_c[1],
        label="DB growth MB/h",
    )
    ax2 = ax.twinx()
    ax2.plot(
        hubs, [steps[c].get("pg_xact_s") for c in steps], marker="s", color=series_c[2], label="commits/s"
    )
    ax.set_xlabel("fleet size (hubs)")
    ax.set_ylabel("MB per hour")
    ax2.set_ylabel("commits/s")
    ax.set_title("Postgres write load", color=ink, loc="left")
    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, frameon=False, fontsize=8)
    save(fig, "db-write-load-vs-fleet.svg")

    # 5 API p95
    fig, ax = plt.subplots(figsize=(7.5, 4.2))
    for i, name in enumerate(API_NAMES):
        ax.plot(
            hubs,
            [_value_or_nan(((steps[c].get("api") or {}).get(name) or {}).get("p95_ms")) for c in steps],
            marker="o",
            color=series_c[i % len(series_c)],
            label=name,
        )
    ax.set_xlabel("fleet size (hubs)")
    ax.set_ylabel("p95 (ms)")
    ax.set_yscale("log")
    ax.set_title("API and /og/fleet page latency (p95)", color=ink, loc="left")
    ax.grid(axis="y", color=grid)
    ax.legend(frameon=False, fontsize=8)
    save(fig, "api-p95-vs-fleet.svg")

    # 6 soak RSS
    soak = res.get("soak")
    if soak:
        fig, ax = plt.subplots(figsize=(7.5, 4.2))
        names = [p for p in PROCS if p in soak]
        xs = list(range(len(names)))
        ax.bar(
            [x - 0.2 for x in xs],
            [soak[p]["rss_start_mb"] for p in names],
            width=0.4,
            color=series_c[0],
            label="first 10 min",
        )
        ax.bar(
            [x + 0.2 for x in xs],
            [soak[p]["rss_end_mb"] for p in names],
            width=0.4,
            color=series_c[2],
            label="last 10 min",
        )
        ax.set_xticks(xs, names, rotation=30, ha="right")
        ax.set_ylabel("RSS (MB)")
        ax.set_title(f"Soak ({soak['duration_min']:.0f} min): memory at start vs end", color=ink, loc="left")
        ax.legend(frameon=False)
        save(fig, "soak-rss.svg")
    return written


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", type=Path, default=Path(__file__).resolve().parent / ".run")
    p.add_argument("--charts", type=Path, default=None, help="write SVG charts here (needs matplotlib)")
    p.add_argument("--md", type=Path, default=None, help="write the per-step Markdown table here")
    args = p.parse_args()
    res = analyze(args.run)
    (args.run / "data" / "results.json").write_text(json.dumps(res, indent=1, default=str))
    if args.md and res["steps"]:
        args.md.write_text(markdown(res))
    if args.charts and res["steps"]:
        for path in charts(res, args.charts):
            print(f"chart: {path}")
    print(json.dumps({"steps": list(res["steps"]), "knee": res.get("knee"), "aborts": len(res["aborts"])}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
