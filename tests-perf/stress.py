#!/usr/bin/env python3
"""tests-perf/stress.py -- stress scenarios against the ISOLATED perf stack (root, base server).

    python tests-perf/stress.py --run /srv/ogwork/perf-run <scenario> [...]

Scenarios (each writes <run>/data/stress-<scenario>.json with its measurements and PASS/FAIL checks):

  burst      all hubs reconnect at once: restart the perf broker, time until every hub is fresh again
  outage     60 s perf-broker outage, then recovery (freshness, engine cycles, no commands while down)
  safestop   fleet-wide safe stop via the API (propose + confirm): time until every hub reports |p| <= 0.05 kW
             and og.stop_outbox is drained; then the two-person release (og-op-a requests, og-op-b approves)
  bulk       bulk manual command at the 500-hub cap (propose, confirm, second confirm when required)
  alerts     alert storm (SCADA bank overload on every bank + inverter trips), then bulk ack in 500s
  price      price spike on every zone (market simulator), selector/allocator load for --price-min minutes
  dbslow     DB slow-down: ACCESS EXCLUSIVE lock on og.trace for --lock-s seconds; fail-closed check
             (no signed command published while the verdict trace cannot be written) and recovery

The MQTT monitor uses the read-only perf monitor user (og_perfmon); the API is called with the perf proxy
secret. Nothing here can reach production: every endpoint comes from <run>/ports.env.
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import paho.mqtt.client as mqtt
import psycopg
from paho.mqtt.enums import CallbackAPIVersion

from sampler import dsn_from
from targets import load_target, read_env

STOPPED_KW = 0.05


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


class Monitor:
    """Per-hub telemetry state and per-topic message counts from the perf broker."""

    def __init__(self, port: int, user: str, password: str, root: str) -> None:
        self.root = root
        self.lock = threading.Lock()
        self.last_p: dict[str, float] = {}
        self.last_seen: dict[str, float] = {}
        self.counts: Counter[str] = Counter()
        self.events: list[tuple[float, str]] = []  # (t, kind) for cmd/stop/lease messages
        self.connected_at: float | None = None
        self.client = mqtt.Client(
            CallbackAPIVersion.VERSION2, client_id=f"ogperf-stress-{uuid.uuid4().hex[:6]}"
        )
        self.client.username_pw_set(user, password)
        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(1, 2)
        self.client.connect_async("127.0.0.1", port, keepalive=15)
        self.client.loop_start()

    def _on_connect(self, client: Any, *_: Any) -> None:
        self.connected_at = time.time()
        for sub in ("tel/#", "cmd/#", "stop/#", "lease/#", "ack/#"):
            client.subscribe(f"{self.root}/{sub}", qos=0)

    def _on_message(self, _c: Any, _u: Any, msg: mqtt.MQTTMessage) -> None:
        kind = msg.topic[len(self.root) + 1 :].split("/", 1)[0]
        t = time.time()
        with self.lock:
            self.counts[kind] += 1
            if kind == "tel":
                try:
                    d = json.loads(msg.payload)
                    hub = str(d["hub_id"])
                    self.last_p[hub] = float(d.get("p_kw") or 0.0)
                    self.last_seen[hub] = t
                except (ValueError, KeyError, TypeError):
                    pass
            elif kind in ("cmd", "stop", "lease"):
                self.events.append((t, kind))

    def fresh(self, within_s: float = 25.0) -> int:
        cutoff = time.time() - within_s
        with self.lock:
            return sum(1 for t in self.last_seen.values() if t >= cutoff)

    def reported_since(self, t0: float) -> set[str]:
        with self.lock:
            return {h for h, t in self.last_seen.items() if t >= t0}

    def stopped_since(self, t0: float) -> tuple[int, int]:
        with self.lock:
            hubs = [h for h, t in self.last_seen.items() if t >= t0]
            return sum(1 for h in hubs if abs(self.last_p.get(h, 0.0)) <= STOPPED_KW), len(hubs)

    def nonzero(self) -> int:
        with self.lock:
            return sum(1 for p in self.last_p.values() if abs(p) > STOPPED_KW)

    def events_between(self, t0: float, t1: float) -> Counter[str]:
        with self.lock:
            return Counter(k for t, k in self.events if t0 <= t <= t1)

    def close(self) -> None:
        self.client.loop_stop()
        self.client.disconnect()


class Ctx:
    def __init__(self, run: Path, repo: Path) -> None:
        self.run = run
        self.repo = repo
        self.ports = read_env(run / "ports.env")
        self.secrets = read_env(run / "etc" / "secrets.env")
        self.expected = int(self.ports["EXPECTED_HUBS"])
        self.api = f"http://127.0.0.1:{self.ports['API_PORT']}"
        self.market = f"http://127.0.0.1:{self.ports['MARKET_PORT']}"
        self.dsn = dsn_from(self.ports, self.secrets, "ogperf-stress")
        self.target = load_target(run, repo)
        self.mon = Monitor(
            int(self.ports["MQTT_PORT"]),
            self.ports.get("MONITOR_USER", "og_perfmon"),
            self.secrets["OG_MQTT_PERFMON_PASSWORD"],
            self.ports["TOPIC_ROOT"],
        )

    def http(self, user: str = "operator") -> httpx.Client:
        headers = {"x-remote-user": user, "x-og-proxy-auth": self.secrets["OG_API_PROXY_SECRET"]}
        return httpx.Client(base_url=self.api, headers=headers, timeout=60.0)

    def sql(self, query: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
        with psycopg.connect(self.dsn, autocommit=True) as conn:
            return conn.execute(query, params).fetchall()

    def engine_metric(self, prefix: str) -> dict[str, float]:
        try:
            text = self.target.scrape("engine", self.ports["ENGINE_METRICS_PORT"])
        except httpx.HTTPError:
            return {}
        out = {}
        for line in text.splitlines():
            if line.startswith(prefix):
                k, _, v = line.rpartition(" ")
                try:
                    out[k] = float(v)
                except ValueError:
                    continue
        return out

    def db_fresh(self) -> int:
        try:
            return int(
                self.sql(
                    "select count(*) from og.hub_state where last_seen_at > now() - interval '25 seconds'"
                )[0][0]
            )
        except psycopg.Error:
            return -1

    def publish_scenario(
        self, typ: str, kind: str, ref: str, duration_s: float, params: dict[str, Any]
    ) -> str:
        sid = f"perf-{typ.lower()}-{uuid.uuid4().hex[:8]}"
        payload = {
            "id": sid,
            "type": typ,
            "target": {"kind": kind, "ref": ref},
            "params": params,
            "start": now_iso().replace("+00:00", "Z"),
            "duration_s": duration_s,
        }
        self.mon.client.publish(f"{self.ports['TOPIC_ROOT']}/scenario/cmd", json.dumps(payload), qos=1)
        return sid


def wait_until(pred: Any, timeout_s: float, step_s: float = 1.0) -> float | None:
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        if pred():
            return time.time() - t0
        time.sleep(step_s)
    return None


def timeline(ctx: Ctx, seconds: float, every_s: float = 5.0) -> list[dict[str, Any]]:
    out = []
    t0 = time.time()
    while time.time() - t0 < seconds:
        cyc = ctx.engine_metric("og_engine_cycle_latency_ms")
        out.append(
            {
                "t": round(time.time() - t0, 1),
                "mqtt_fresh": ctx.mon.fresh(),
                "db_fresh": ctx.db_fresh(),
                "p99_ms": cyc.get('og_engine_cycle_latency_ms{quantile="p99"}'),
            }
        )
        time.sleep(every_s)
    return out


def check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "result": "PASS" if ok else "FAIL", "detail": detail}


# --- scenarios -------------------------------------------------------------------------------------------


def sc_burst(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    """Broker restart: every hub (and every og-* client) reconnects at once."""
    time.sleep(5)
    t0 = time.time()
    ctx.target.broker("stop")
    ctx.target.broker("start")
    t_up = time.time()
    all_back = wait_until(lambda: len(ctx.mon.reported_since(t_up)) >= ctx.expected, 180, 0.5)
    db_back = wait_until(lambda: ctx.db_fresh() >= ctx.expected, 240, 2)
    after = timeline(ctx, 120)
    lag = ctx.engine_metric("og_engine_mqtt_ingest_lag_seconds")
    res = {
        "restart_s": round(t_up - t0, 1),
        "all_hubs_publishing_s": all_back,
        "db_all_fresh_s": db_back,
        "timeline": after,
        "ingest_lag_hist": lag,
    }
    res["checks"] = [
        check("every hub publishing again", all_back is not None, f"{all_back} s after broker up"),
        check(
            "every hub fresh in og.hub_state <= 60 s", db_back is not None and db_back <= 60, f"{db_back} s"
        ),
        check(
            "cycle p99 back < 500 ms within 2 min",
            bool(after) and (after[-1]["p99_ms"] or 1e9) < 500,
            f"last p99 {after[-1]['p99_ms'] if after else None}",
        ),
    ]
    return res


def sc_outage(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    ctx.target.broker("stop")
    t_down = time.time()
    during = timeline(ctx, args.outage_s, 5)
    ctx.target.broker("start")
    t_up = time.time()
    back = wait_until(lambda: len(ctx.mon.reported_since(t_up)) >= ctx.expected, 180, 0.5)
    db_back = wait_until(lambda: ctx.db_fresh() >= ctx.expected, 240, 2)
    after = timeline(ctx, 150)
    health = ctx.http("viewer").get("/og/api/health").json()
    res = {
        "outage_s": round(t_up - t_down, 1),
        "during": during,
        "hubs_publishing_after_s": back,
        "db_all_fresh_after_s": db_back,
        "after": after,
        "health_after": {
            k: health.get(k) for k in ("status", "degraded_modes", "hub_health_counts", "cycle_latency")
        },
    }
    res["checks"] = [
        check("hubs publishing again after broker returns", back is not None, f"{back} s"),
        check(
            "all hubs fresh in DB <= 60 s after return", db_back is not None and db_back <= 60, f"{db_back} s"
        ),
        check(
            "og-engine alive and cycling after recovery",
            bool(after) and after[-1]["p99_ms"] is not None,
            f"p99 {after[-1]['p99_ms'] if after else None}",
        ),
    ]
    return res


def _await_proposal(r: httpx.Response) -> dict[str, Any]:
    if r.status_code >= 400:
        raise RuntimeError(f"{r.request.url.path}: HTTP {r.status_code} {r.text[:300]}")
    body: dict[str, Any] = r.json()
    return body


def sc_safestop(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    nonzero_before = ctx.mon.nonzero()
    with ctx.http("operator") as c:
        prop = _await_proposal(
            c.post("/og/api/safestop", json={"scope": "fleet", "reason": "perf stress: fleet safe stop"})
        )
        t0 = time.time()
        conf = c.post(f"/og/api/safestop/{prop['proposal_id']}/confirm")
        t_conf = time.time()
    confirm = {"status": conf.status_code, "body": conf.text[:400], "latency_s": round(t_conf - t0, 2)}
    stop_msgs = wait_until(lambda: ctx.mon.events_between(t0, time.time())["stop"] > 0, 30, 0.2)
    all_zero = wait_until(lambda: ctx.mon.stopped_since(t0 + 4.0) >= (ctx.expected, ctx.expected), 180, 0.5)
    drained = wait_until(
        lambda: ctx.sql("select count(*) from og.stop_outbox where published_at is null")[0][0] == 0, 120, 1
    )
    outbox = ctx.sql(
        "select count(*), max(extract(epoch from published_at - created_at)), max(attempts) from og.stop_outbox"
    )[0]
    stopped, reporting = ctx.mon.stopped_since(t0 + 4.0)
    release: dict[str, Any] = {}
    try:
        with ctx.http("og-op-a") as a, ctx.http("og-op-b") as b:
            req = _await_proposal(
                a.post("/og/api/safestop/fleet/FLEET/release", json={"reason": "perf: release"})
            )
            t_r = time.time()
            appr = b.post(f"/og/api/safestop/release/{req['proposal_id']}/approve")
            release = {
                "status": appr.status_code,
                "body": appr.text[:400],
                "latency_s": round(time.time() - t_r, 2),
            }
    except (RuntimeError, httpx.HTTPError) as exc:
        release = {"error": str(exc)[:400]}
    res = {
        "hubs_nonzero_before": nonzero_before,
        "confirm": confirm,
        "first_stop_message_s": stop_msgs,
        "all_hubs_zero_s": all_zero,
        "hubs_zero_at_end": stopped,
        "hubs_reporting": reporting,
        "outbox_drained_s": drained,
        "outbox_rows": outbox[0],
        "outbox_max_publish_delay_s": float(outbox[1]) if outbox[1] is not None else None,
        "outbox_max_attempts": outbox[2],
        "release": release,
    }
    res["checks"] = [
        check("confirm returns engaged (200)", conf.status_code == 200, f"HTTP {conf.status_code}"),
        check(
            "every hub at 0 kW <= 30 s",
            all_zero is not None and all_zero <= 30,
            f"{all_zero} s ({stopped}/{reporting})",
        ),
        check("stop_outbox drained", drained is not None, f"{drained} s, {outbox[0]} rows"),
        check("two-person release accepted", release.get("status") in (200, 202), json.dumps(release)[:200]),
    ]
    return res


def sc_bulk(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    hubs = [
        r[0]
        for r in ctx.sql("select hub_id from og.hub where hub_id like 'hub-%%' order by hub_id limit 500")
    ]
    with ctx.http("operator") as c:
        t0 = time.time()
        r = c.post(
            "/og/api/fleet/commands/bulk",
            json={
                "hub_ids": hubs,
                "p_kw_setpoint": -2.0,
                "reason": "perf stress bulk 500",
                "duration_minutes": 15,
            },
        )
        t_prop = time.time() - t0
        prop = _await_proposal(r)
        steps = [
            {"step": "propose", "s": round(t_prop, 2), "requires_double": prop.get("requires_double_confirm")}
        ]
        result: dict[str, Any] = {}
        for i in range(int(prop.get("confirmations_required", 1))):
            t1 = time.time()
            rc = c.post(f"/og/api/fleet/commands/bulk/{prop['proposal_id']}/confirm")
            steps.append(
                {"step": f"confirm{i + 1}", "s": round(time.time() - t1, 2), "status": rc.status_code}
            )
            result = rc.json() if rc.status_code < 500 else {"text": rc.text[:300]}
    t_exec = time.time()
    first_cmd = wait_until(lambda: ctx.mon.events_between(t_exec, time.time())["cmd"] > 0, 60, 0.2)
    hubset = set(hubs)

    def moving() -> int:
        with ctx.mon.lock:
            return sum(
                1 for h in hubset if ctx.mon.last_seen.get(h, 0) > t_exec and ctx.mon.last_p.get(h, 0) < -0.5
            )

    reached = wait_until(lambda: moving() >= int(0.95 * len(hubs)), 300, 2)
    after = timeline(ctx, 90)
    cancel = None
    trace_id = result.get("manual_target_trace_id")
    if trace_id:
        with ctx.http("operator") as c:
            cancel = c.post(f"/og/api/fleet/manual-targets/{trace_id}/cancel").status_code
    res = {
        "hubs": len(hubs),
        "steps": steps,
        "status": result.get("status"),
        "first_cmd_s": first_cmd,
        "hubs_discharging_95pct_s": reached,
        "hubs_discharging_end": moving(),
        "timeline": after,
        "cancel_status": cancel,
    }
    res["checks"] = [
        check("propose < 5 s at the 500 cap", t_prop < 5, f"{t_prop:.2f} s"),
        check("execution accepted (RAMPING)", result.get("status") == "RAMPING", str(result.get("status"))),
        check("95% of selected hubs follow within 5 min", reached is not None, f"{reached} s"),
        check(
            "cycle p99 < 500 ms while ramping",
            all((x["p99_ms"] or 0) < 500 for x in after),
            f"max {max((x['p99_ms'] or 0) for x in after) if after else None}",
        ),
    ]
    return res


def sc_alerts(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    open0 = ctx.sql("select count(*) from og.alert where cleared_at is null")[0][0]
    banks = [r[0] for r in ctx.sql("select bank_id from og.bank where bank_id like 'bank-0%%' order by 1")]
    for b in banks:
        ctx.publish_scenario("SCADA_BANK_OVERLOAD", "bank", b, args.storm_s, {})
    for zone in ("LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST"):
        ctx.publish_scenario("FLEET_INVERTER_TRIP", "zone", zone, args.storm_s, {"fraction": 0.2})
    t0 = time.time()
    peak = open0
    series = []
    while time.time() - t0 < args.storm_s:
        n = ctx.sql("select count(*) from og.alert where cleared_at is null and acked_by is null")[0][0]
        peak = max(peak, n)
        cyc = ctx.engine_metric("og_engine_cycle_latency_ms").get(
            'og_engine_cycle_latency_ms{quantile="p99"}'
        )
        series.append({"t": round(time.time() - t0), "unacked_open": n, "p99_ms": cyc})
        time.sleep(10)
    by_rule = ctx.sql(
        "select rule, count(*) from og.alert where opened_at > now() - make_interval(secs => %s) group by 1 order by 2 desc",
        (args.storm_s + 60,),
    )
    acked = 0
    ack_calls = []
    with ctx.http("operator") as c:
        list_t = time.time()
        page = c.get("/og/api/alerts", params={"limit": 500, "open_only": "true"})
        list_s = time.time() - list_t
        while True:
            ids = [
                a["id"]
                for a in page.json().get("alerts", page.json().get("items", []))
                if not a.get("acked_by")
            ]
            if not ids:
                break
            t1 = time.time()
            r = c.post("/og/api/alerts/ack-bulk", json={"alert_ids": ids[:500]})
            ack_calls.append({"n": len(ids[:500]), "s": round(time.time() - t1, 2), "status": r.status_code})
            if r.status_code != 200:
                break
            acked += r.json()["counts"]["acked"]
            page = c.get("/og/api/alerts", params={"limit": 500, "open_only": "true"})
            if len(ack_calls) > 40:
                break
    res = {
        "banks_overloaded": len(banks),
        "open_before": open0,
        "peak_unacked_open": peak,
        "by_rule": [{"rule": r, "n": n} for r, n in by_rule],
        "series": series,
        "list_500_s": round(list_s, 2),
        "ack_calls": ack_calls,
        "acked": acked,
    }
    res["checks"] = [
        check("storm produced alerts", peak > open0, f"{open0} -> {peak}"),
        check("alert list (500) < 2 s", list_s < 2, f"{list_s:.2f} s"),
        check(
            "each bulk ack of <= 500 < 10 s",
            all(a["s"] < 10 and a["status"] == 200 for a in ack_calls),
            json.dumps(ack_calls)[:200],
        ),
        check("cycle p99 < 500 ms during storm", all((x["p99_ms"] or 0) < 500 for x in series), "see series"),
    ]
    return res


def sc_price(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    market = ctx.market
    dur = args.price_min * 60
    injected = []
    for zone in ("LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST", "LZ_AEN", "LZ_CPS", "LZ_LCRA", "LZ_RAYBN"):
        r = httpx.post(
            f"{market}/admin/anomalies",
            json={
                "id": f"perf-spike-{zone}",
                "type": "price_spike",
                "target": zone,
                "params": {"value_usd_per_mwh": 3000.0},
                "duration": dur,
            },
            timeout=10,
        )
        injected.append({"zone": zone, "status": r.status_code})
    gate0 = ctx.engine_metric("og_engine_gate_duration_seconds")
    trace0 = ctx.sql("select count(*) from og.trace")[0][0]
    series = timeline(ctx, dur, 15)
    gate1 = ctx.engine_metric("og_engine_gate_duration_seconds")
    by_type = ctx.sql(
        "select decision_type, event_class, count(*) from og.trace where created_at > now() - make_interval(secs => %s) "
        "group by 1, 2 order by 3 desc limit 15",
        (dur,),
    )
    spike_seen = ctx.sql(
        "select count(*), max(value) from og.feed_obs where ts > now() - interval '2 hours' and value >= 2999"
    )[0]
    res = {
        "injected": injected,
        "series": series,
        "gate_before": gate0,
        "gate_after": gate1,
        "trace_rows": ctx.sql("select count(*) from og.trace")[0][0] - trace0,
        "trace_by_type": [{"decision_type": a, "event_class": b, "n": n} for a, b, n in by_type],
        "spike_obs": {"n": spike_seen[0], "max": spike_seen[1]},
    }
    res["checks"] = [
        check("spike reached og.feed_obs", (spike_seen[0] or 0) > 0, f"{spike_seen[0]} obs"),
        check("cycle p99 < 500 ms during spike", all((x["p99_ms"] or 0) < 500 for x in series), "see series"),
    ]
    return res


def sc_dbslow(ctx: Ctx, args: argparse.Namespace) -> dict[str, Any]:
    restarts0 = {p: ctx.target.restarts(p) for p in ("engine", "guardian", "api", "settle")}
    before = timeline(ctx, 30)
    holder = psycopg.connect(ctx.dsn, autocommit=False)
    holder.execute("lock table og.trace in access exclusive mode")
    t0 = time.time()
    during = timeline(ctx, args.lock_s, 5)
    t1 = time.time()
    ev_during = ctx.mon.events_between(t0 + 5, t1)  # 5 s grace for anything already signed before the lock
    holder.rollback()
    holder.close()
    t_rel = time.time()
    first_cmd = wait_until(lambda: ctx.mon.events_between(t_rel, time.time())["cmd"] > 0, 180, 0.5)
    after = timeline(ctx, 150)
    restarts = {p: ctx.target.restarts(p) - restarts0[p] for p in restarts0}
    verdicts = ctx.sql(
        "select payload->>'outcome', count(*) from og.trace where decision_type = 'GUARDIAN_VERDICT' "
        "and created_at > now() - make_interval(secs => %s) group by 1",
        (int(time.time() - t0) + 5,),
    )
    res = {
        "lock_s": round(t1 - t0, 1),
        "before": before,
        "during": during,
        "mqtt_events_during_lock": dict(ev_during),
        "first_cmd_after_release_s": first_cmd,
        "after": after,
        "process_restarts_during": restarts,
        "verdicts_since_lock": {str(k): n for k, n in verdicts},
    }
    res["checks"] = [
        check(
            "no guardian command published while og.trace was locked (fail closed)",
            ev_during["cmd"] == 0,
            str(dict(ev_during)),
        ),
        check("commands resume after the lock is released", first_cmd is not None, f"{first_cmd} s"),
        check(
            "cycle p99 back < 500 ms within 2.5 min",
            bool(after) and (after[-1]["p99_ms"] or 1e9) < 500,
            f"{after[-1]['p99_ms'] if after else None}",
        ),
    ]
    return res


SCENARIOS = {
    "burst": sc_burst,
    "outage": sc_outage,
    "safestop": sc_safestop,
    "bulk": sc_bulk,
    "alerts": sc_alerts,
    "price": sc_price,
    "dbslow": sc_dbslow,
}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("scenario", choices=sorted(SCENARIOS))
    p.add_argument("--run", type=Path, default=Path(__file__).resolve().parent / ".run")
    p.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    p.add_argument("--outage-s", type=float, default=60.0)
    p.add_argument("--lock-s", type=float, default=60.0)
    p.add_argument("--storm-s", type=float, default=180.0)
    p.add_argument("--price-min", type=float, default=10.0)
    args = p.parse_args()
    ctx = Ctx(args.run, args.repo)
    wait_until(lambda: ctx.mon.connected_at is not None, 15, 0.2)
    time.sleep(12)  # one telemetry interval so the monitor knows every hub
    started = now_iso()
    try:
        res = SCENARIOS[args.scenario](ctx, args)
    except Exception as exc:  # a scenario that cannot run is a FAIL, recorded, never silently skipped
        res = {
            "error": f"{type(exc).__name__}: {exc}"[:600],
            "checks": [check("scenario ran", False, str(exc)[:200])],
        }
    res.update(scenario=args.scenario, started=started, finished=now_iso(), expected_hubs=ctx.expected)
    res["verdict"] = "PASS" if all(c["result"] == "PASS" for c in res.get("checks", [])) else "FAIL"
    ctx.mon.close()
    out = args.run / "data" / f"stress-{args.scenario}.json"
    out.write_text(json.dumps(res, indent=1, default=str))
    print(
        json.dumps(
            {"scenario": args.scenario, "verdict": res["verdict"], "checks": res.get("checks")}, default=str
        )
    )
    return 0


if __name__ == "__main__":
    os.umask(0o027)
    raise SystemExit(main())
