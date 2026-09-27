#!/usr/bin/env python3
"""tests-perf/sampler.py -- sample the isolated perf stack every --interval seconds into JSONL.

One JSON object per sample, appended to <run>/data/samples.jsonl, tagged with the current stage (<run>/STAGE,
written by campaign.py). Everything is cumulative or instantaneous; analyze.py differences the first and last
sample of a window. Sources (endpoints from <run>/ports.env, process/disk/host figures from the Target):

- og-engine / og-guardian `/metrics`: tick-duration, ingest-lag, loop-lag, ack-latency and gate histograms (raw
  cumulative buckets), the A11 rolling-window gauge, og_hubs, flush lag;
- the perf database: pg_stat_database, WAL position, telemetry inserts, hub freshness, table sizes,
  CYCLE_LATENCY trace summaries and GUARDIAN_VERDICT latencies/outcomes since the previous sample, stop_outbox
  backlog, open alerts;
- the perf broker's $SYS tree (monitor user): messages received/sent, clients;
- per-process CPU seconds and resident memory, the database disk's counters, host CPU and memory;
- every --api-every seconds: latency of the API endpoints the console uses (health, fleet table, fleet map,
  alerts) and the /og/fleet page (server-rendered first page), --api-repeat requests each.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import paho.mqtt.client as mqtt
import psycopg
from paho.mqtt.enums import CallbackAPIVersion

from targets import Target, load_target, read_env

METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(\S+)")
KEEP = (
    "og_control_tick_duration_seconds_bucket",
    "og_control_tick_duration_seconds_count",
    "og_control_tick_duration_seconds_sum",
    "og_control_ticks_total",
    "og_engine_mqtt_ingest_lag_seconds_bucket",
    "og_engine_mqtt_ingest_lag_seconds_count",
    "og_eventloop_lag_seconds_bucket",
    "og_eventloop_lag_seconds_count",
    "og_command_ack_latency_seconds_bucket",
    "og_command_ack_latency_seconds_count",
    "og_engine_gate_duration_seconds_bucket",
    "og_engine_gate_duration_seconds_count",
    "og_engine_gate_duration_seconds_sum",
    "og_engine_cycle_latency_ms",
    "og_engine_fleet_flush_lag_seconds",
    "og_hubs",
    "og_pq_ingest_summaries_buffered",
    "og_pq_ingest_summaries_dropped_total",
    "og_guardian_verdicts_total",
    "og_commands_total",
    "process_resident_memory_bytes",
    "process_cpu_seconds_total",
    "og_mqtt_connected",
)


def parse_metrics(text: str, keep: tuple[str, ...] = KEEP) -> dict[str, float]:
    """Prometheus text exposition -> {"name{labels}": value} for the kept metric names."""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        m = METRIC_RE.match(line)
        if not m or not m.group(1).startswith(keep):
            continue
        try:
            out[m.group(1) + (m.group(2) or "")] = float(m.group(3))
        except ValueError:
            continue
    return out


class SysMonitor:
    """Latest $SYS values from the perf broker."""

    def __init__(self, port: int, user: str, password: str) -> None:
        self.values: dict[str, str] = {}
        self.client = mqtt.Client(CallbackAPIVersion.VERSION2, client_id="ogperf-sampler-sys")
        self.client.username_pw_set(user, password)
        self.client.on_connect = lambda c, *_: c.subscribe("$SYS/#")
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 5)
        self.client.connect_async("127.0.0.1", port, keepalive=20)
        self.client.loop_start()

    def _on_message(self, _c: Any, _u: Any, msg: mqtt.MQTTMessage) -> None:
        if msg.topic.startswith(("$SYS/broker/messages", "$SYS/broker/clients", "$SYS/broker/load/messages")):
            self.values[msg.topic.removeprefix("$SYS/broker/")] = msg.payload.decode(errors="replace")

    def snapshot(self) -> dict[str, str]:
        return dict(self.values)


DB_SQL = {
    "db": """select xact_commit, tup_inserted, tup_updated, tup_deleted, blks_read, blks_hit,
                    pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')::float8 as wal_bytes,
                    pg_database_size(current_database())::float8 as db_bytes
             from pg_stat_database where datname = current_database()""",
    "hubs": """select count(*) as total,
                      count(*) filter (where last_seen_at > now() - interval '25 seconds') as fresh,
                      count(*) filter (where last_seen_at > now() - interval '60 seconds') as seen_60s,
                      coalesce(max(extract(epoch from now() - last_seen_at)), 0)::float8 as oldest_s,
                      count(*) filter (where abs(p_kw) > 0.05) as nonzero_kw
               from og.hub_state""",
    "tel": """select coalesce(sum(n_tup_ins), 0)::float8 as ins from pg_stat_user_tables
              where schemaname = 'og' and relname like 'telemetry%' and relname not like 'telemetry_15m%'
                and relname not like 'telemetry_1m%'""",
    "tables": """select c.relname, pg_total_relation_size(c.oid)::float8 as bytes
                 from pg_class c join pg_namespace n on n.oid = c.relnamespace
                 where n.nspname = 'og' and c.relkind in ('r', 'p') order by 2 desc limit 15""",
    "ins": """select relname, n_tup_ins::float8, n_tup_upd::float8 from pg_stat_user_tables
              where schemaname = 'og' and relname in ('trace', 'hub_state', 'alert', 'command_batch',
                    'command_ack', 'stop_outbox', 'feed_obs', 'pq_waveform_summary', 'verdict')""",
    "misc": """select (select count(*) from og.stop_outbox where published_at is null) as stop_unpublished,
                      (select count(*) from og.alert where cleared_at is null) as alerts_open,
                      (select count(*) from og.alert where cleared_at is null and acked_by is null) as alerts_unacked,
                      (select count(*) from og.degraded_mode_state) as degraded_modes,
                      (select count(*) from og.bank where bank_id not like 'bank-truck-%'
                         and bank_id not like 'bank-sub-%') as home_banks""",
}
CYCLE_SQL = """select created_at, payload from og.trace
               where decision_type = 'RT_ALLOCATION' and event_class = 'CYCLE_LATENCY' and created_at > %s
               order by created_at"""
VERDICT_SQL = """select count(*)::float8,
                        percentile_cont(0.5) within group (order by (payload->>'latency_ms')::float8),
                        percentile_cont(0.95) within group (order by (payload->>'latency_ms')::float8),
                        percentile_cont(0.99) within group (order by (payload->>'latency_ms')::float8),
                        max((payload->>'latency_ms')::float8),
                        count(*) filter (where payload->>'outcome' = 'TIMEOUT')::float8,
                        count(*) filter (where payload->>'outcome' = 'PASS')::float8,
                        count(*) filter (where payload->>'outcome' like '%%VETO%%')::float8
                 from og.trace
                 where decision_type = 'GUARDIAN_VERDICT' and payload->>'kind' is null and created_at > %s"""
#: Dispatch load since the last sample (the DELIVERING regime's "banks with grants per cycle"). cycle_id is
#: "<epoch s>-<seq>", so a text range on its index replaces a created_at scan of og.grant.
GRANT_SQL = """select count(*)::float8, count(distinct cycle_id)::float8,
                      count(distinct (cycle_id, bank_id)) filter (
                          where bank_id not like 'bank-truck-%%' and bank_id not like 'bank-sub-%%')::float8,
                      count(distinct bank_id)::float8,
                      count(*) filter (where is_headroom)::float8
               from og.grant where cycle_id >= %s"""


def dsn_from(ports: dict[str, str], secrets: dict[str, str], app: str) -> str:
    return (
        f"host=127.0.0.1 port={ports['DB_PORT']} dbname={ports['DB_NAME']} user={ports['DB_ROLE']} "
        f"password={secrets['OG_DB_PASSWORD']} application_name={app} connect_timeout=5"
    )


class DbProbe:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self.conn: psycopg.Connection[Any] | None = None
        self.last_ts = datetime.now(UTC)

    def _connect(self) -> psycopg.Connection[Any]:
        if self.conn is None or self.conn.closed:
            self.conn = psycopg.connect(self.dsn, autocommit=True)
            self.conn.execute("set statement_timeout = '5s'")
        return self.conn

    def sample(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        try:
            conn = self._connect()
            for key, sql in DB_SQL.items():
                cur = conn.execute(sql)
                cols = [d.name for d in cur.description or []]
                rows = cur.fetchall()
                if key in ("tables", "ins"):
                    out[key] = [dict(zip(cols, r, strict=True)) for r in rows]
                else:
                    out[key] = dict(zip(cols, rows[0], strict=True)) if rows else {}
            since, now = self.last_ts, datetime.now(UTC)
            cycles = conn.execute(CYCLE_SQL, (since,)).fetchall()
            out["cycle_latency"] = [{"ts": ts.isoformat(), **payload} for ts, payload in cycles]
            v = conn.execute(VERDICT_SQL, (since,)).fetchone()
            keys = ("n", "p50_ms", "p95_ms", "p99_ms", "max_ms", "timeouts", "pass", "vetoed")
            out["verdicts"] = dict(zip(keys, v, strict=True)) if v else {}
            g = conn.execute(GRANT_SQL, (str(int(since.timestamp())),)).fetchone()
            gkeys = ("rows", "cycles", "bank_cycles", "banks", "headroom_rows")
            out["grants"] = dict(zip(gkeys, g, strict=True)) if g else {}
            self.last_ts = now
        except (psycopg.Error, OSError) as exc:
            out["error"] = f"{type(exc).__name__}: {exc}"[:300]
            self.conn = None
        return out


API_PROBES = (
    ("health", "/og/api/health"),
    ("fleet_table", "/og/api/fleet/table?limit=50"),
    ("fleet_table_soc100", "/og/api/fleet/table?limit=100&sort=soc&dir=desc"),
    ("fleet_map", "/og/api/fleet/map"),
    ("alerts", "/og/api/alerts?limit=100"),
    ("ui_fleet", "/og/fleet"),
)


def api_probe(base: str, secret: str, repeat: int) -> dict[str, Any]:
    headers = {"x-remote-user": "viewer", "x-og-proxy-auth": secret}
    out: dict[str, Any] = {}
    with httpx.Client(base_url=base, headers=headers, timeout=30.0) as client:
        for name, path in API_PROBES:
            lat: list[float] = []
            codes: dict[str, int] = {}
            size = 0
            for _ in range(repeat):
                t0 = time.perf_counter()
                try:
                    r = client.get(path)
                    size = len(r.content)
                    code = str(r.status_code)
                except httpx.HTTPError as exc:
                    code = type(exc).__name__
                lat.append((time.perf_counter() - t0) * 1000.0)
                codes[code] = codes.get(code, 0) + 1
            out[name] = {"ms": [round(x, 1) for x in lat], "codes": codes, "bytes": size}
    return out


def scrape(target: Target, proc: str, port: str) -> tuple[dict[str, float], str | None]:
    try:
        text = target.scrape(proc, port)
    except httpx.HTTPError as exc:
        return {}, type(exc).__name__
    return (parse_metrics(text), None) if text else ({}, "empty")


def run(args: argparse.Namespace) -> None:
    run_dir: Path = args.run
    ports = read_env(run_dir / "ports.env")
    secrets = read_env(run_dir / "etc" / "secrets.env")
    target = load_target(run_dir, args.repo)
    db = DbProbe(dsn_from(ports, secrets, "ogperf-sampler"))
    sysmon = SysMonitor(
        int(ports["MQTT_PORT"]), ports.get("MONITOR_USER", "og_perfmon"), secrets["OG_MQTT_PERFMON_PASSWORD"]
    )
    base = f"http://127.0.0.1:{ports['API_PORT']}"
    out_path = run_dir / "data" / "samples.jsonl"
    last_api = 0.0
    while True:
        t0 = time.monotonic()
        stage = (run_dir / "STAGE").read_text().strip() if (run_dir / "STAGE").exists() else "unknown"
        if stage in ("done", "done-failed", "done-aborted"):
            return
        rec: dict[str, Any] = {"ts": datetime.now(UTC).isoformat(), "stage": stage, "target": target.name}
        rec["engine"], rec["engine_err"] = scrape(target, "engine", ports["ENGINE_METRICS_PORT"])
        rec["guardian"], rec["guardian_err"] = scrape(target, "guardian", ports["GUARDIAN_METRICS_PORT"])
        rec["db"] = db.sample()
        rec["broker"] = sysmon.snapshot()
        rec["procs"] = target.procs()
        rec["disk"] = target.disks()
        rec["host"] = target.host()
        if time.monotonic() - last_api >= args.api_every:
            last_api = time.monotonic()
            rec["api"] = api_probe(base, secrets.get("OG_API_PROXY_SECRET", ""), args.api_repeat)
        rec["sample_ms"] = round((time.monotonic() - t0) * 1000.0, 1)
        with out_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")
        time.sleep(max(1.0, args.interval - (time.monotonic() - t0)))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--run", type=Path, default=Path(__file__).resolve().parent / ".run")
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--interval", type=float, default=15.0)
    p.add_argument("--api-every", type=float, default=60.0)
    p.add_argument("--api-repeat", type=int, default=10)
    os.umask(0o027)
    run(p.parse_args())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
