#!/usr/bin/env python3
"""tests-perf/dispatch.py -- the DELIVERING regime: enough committed obligations that about `frac` of the
fleet's home banks carry grants every cycle for one delivery window.

Production dispatches in delivery windows: for example the ECRS window from 00:00 CT (about 22 AS awards of
500 kW, one bank each) and the daily toll. Between windows it is idle. So every campaign size is measured twice:
IDLE (`step-<homes>`) and DELIVERING (`deliver-<homes>`).

Everything goes through og-api's own admission, like dev/scripts/seed_demo_customers.py and tests-e2e:
- a perf ERCOT_ENERGY contract;
- one opportunity per bank still missing, each about one bank's worth of kW, for the window;
- the offer value is set in the database right after admission, because the API has no field for it;
- the selector's own gates decide.

Nothing here writes an obligation, commitment or reservation (K2), and no credential is printed. The demo
seeder runs first for the same window, so the mix also has DIST_DEFERRAL and PARTNER_CAPACITY, not only
ERCOT_ENERGY.
"""

from __future__ import annotations

import math
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import psycopg

INTERVAL = timedelta(minutes=15)
#: The single-hub banks (the substation set and the trucks) are not home banks; the target is a share of home banks.
HOME_BANKS_SQL = (
    "select count(*) from og.bank where bank_id not like 'bank-truck-%' and bank_id not like 'bank-sub-%'"
)
#: Home banks holding any POWER_KW reservation that overlaps the window (any obligation: the intake's own AS
#: awards, held at 0 kW until deployed, also give their banks a grant every cycle of their windows).
RESERVED_BANKS_SQL = """select count(distinct r.bank_id) from og.reservation r
                        where r.kind = 'POWER_KW' and r.released_at is null
                          and r.interval_start < %s and r.interval_end > %s
                          and r.bank_id not like 'bank-truck-%%' and r.bank_id not like 'bank-sub-%%'"""
STATES_SQL = """select o.state, count(*) from og.obligation o
                where o.opportunity_id = any(%s) group by 1"""
LAST_ADMISSION_PLAN_SQL = """select max(p.created_at), now() from og.plan p
                             where p.gate_kind = 'ADMISSION' and p.created_at > %s"""


def window_for(now: datetime, lead_min: float, length_min: float) -> tuple[datetime, datetime]:
    """The delivery window. It starts on the first quarter hour at least `lead_min` after `now`, because offers need
    time to be admitted and committed before it starts. It lasts at least `length_min`, rounded up to whole quarters."""
    earliest = now + timedelta(minutes=lead_min)
    floored = earliest.replace(minute=earliest.minute - earliest.minute % 15, second=0, microsecond=0)
    start = floored if floored >= earliest else floored + INTERVAL
    return start, start + INTERVAL * max(1, math.ceil(length_min / 15.0))


def offers_needed(target_banks: int, reserved_banks: int) -> int:
    """One offer of about a bank's kW per home bank still missing from the target."""
    return max(0, target_banks - reserved_banks)


@dataclass(frozen=True)
class Api:
    base: str
    secret: str
    user: str = "operator"

    def headers(self) -> dict[str, str]:
        return {"X-Remote-User": self.user, "X-OG-Proxy-Auth": self.secret}


def create_contract(http: httpx.Client, api: Api) -> str:
    body = {
        "customer_id": str(uuid4()),
        "service_type": "ERCOT_ENERGY",
        "variant": None,
        "tier": "T2",
        "profile_ref": "perf-ercot_energy@1",
        "start_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "renomination_allowed": False,
        "penalty_alpha": "0.01",
        "penalty_beta": "0.25",
        "penalty_theta": "0.10",
    }
    resp = http.post(f"{api.base}/contracts", json=body, headers=api.headers())
    if resp.status_code != 201:
        raise RuntimeError(f"contract create: HTTP {resp.status_code} {resp.text[:200]}")
    return str(resp.json()["contract_id"])


def offer(
    http: httpx.Client,
    api: Api,
    conn: psycopg.Connection[Any],
    contract_id: str,
    window: tuple[datetime, datetime],
    kw: float,
    value_per_mwh: float,
) -> str:
    resp = http.post(
        f"{api.base}/opportunities",
        json={
            "contract_id": contract_id,
            "window_start": window[0].isoformat(),
            "window_end": window[1].isoformat(),
            "requested_kw": str(kw),
        },
        headers=api.headers(),
    )
    if resp.status_code != 201:
        raise RuntimeError(f"offer: HTTP {resp.status_code} {resp.text[:200]}")
    opportunity_id = str(resp.json()["opportunity_id"])
    conn.execute(
        "update og.opportunity set value_per_mwh = %s where opportunity_id = %s",
        (value_per_mwh, opportunity_id),
    )
    return opportunity_id


def _scalar(conn: psycopg.Connection[Any], sql: str, params: tuple[Any, ...] | None = None) -> int:
    # params=None, not (): with any params psycopg reads `%` as a placeholder (HOME_BANKS_SQL's LIKE patterns).
    row = conn.execute(sql, params).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def wait_gates_quiet(
    conn: psycopg.Connection[Any], since: datetime, quiet_s: float, timeout_s: float
) -> None:
    """Until no ADMISSION plan has been written for `quiet_s` (gates run one at a time), or none at all for 30 s
    (a declined offer writes none), or `timeout_s`."""
    start = time.time()
    while time.time() - start < timeout_s:
        last, db_now = conn.execute(LAST_ADMISSION_PLAN_SQL, (since,)).fetchone() or (None, None)
        if last is not None and db_now is not None and (db_now - last).total_seconds() > quiet_s:
            return
        if last is None and time.time() - start > 30:
            return
        time.sleep(2)


def seed_demo(repo: Path, api: Api, dsn: str, window: tuple[datetime, datetime], log: Any) -> None:
    """dev/scripts/seed_demo_customers.py for the same window (server mode: everything from the environment)."""
    env = {**os.environ, "OG_SEED_API": api.base, "OG_SEED_DSN": dsn, "OG_API_PROXY_SECRET": api.secret}
    minutes = int((window[1] - window[0]).total_seconds() // 60)
    argv = [
        sys.executable,
        str(repo / "dev" / "scripts" / "seed_demo_customers.py"),
        "--start",
        window[0].strftime("%Y-%m-%dT%H:%M:%SZ"),
        "--minutes",
        str(minutes),
        "--wait",
        "240",
    ]
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=600, check=False)
    for line in (done.stdout or "").strip().splitlines()[-8:]:
        log(f"demo seed: {line}")
    if done.returncode != 0:
        log(f"demo seed: exit {done.returncode} {(done.stderr or '').strip()[-300:]}")


def drive(
    repo: Path,
    api: Api,
    dsn: str,
    window: tuple[datetime, datetime],
    frac: float,
    log: Any,
    *,
    kw_per_offer: float = 450.0,
    value_per_mwh: float = 180.0,
    max_rounds: int = 5,
) -> dict[str, Any]:
    """Commit obligations for `window` until `frac` of the home banks hold a POWER_KW reservation for its first
    interval (or `max_rounds`). Returns what was reached, for the report."""
    seed_demo(repo, api, dsn, window, log)
    opportunity_ids: list[str] = []
    with httpx.Client(timeout=30.0) as http, psycopg.connect(dsn, autocommit=True) as conn:
        home_banks = _scalar(conn, HOME_BANKS_SQL)
        target = math.ceil(frac * home_banks)
        contract_id = create_contract(http, api)
        span = (window[1], window[0])  # RESERVED_BANKS_SQL: overlaps [start, end)
        for rnd in range(1, max_rounds + 1):
            have = _scalar(conn, RESERVED_BANKS_SQL, span)
            need = offers_needed(target, have)
            if need == 0 or datetime.now(UTC) > window[0] - timedelta(minutes=1):
                break
            since = datetime.now(UTC)
            for _ in range(need):
                opportunity_ids.append(
                    offer(http, api, conn, contract_id, window, kw_per_offer, value_per_mwh)
                )
            wait_gates_quiet(conn, since, quiet_s=6.0, timeout_s=300.0)
            log(
                f"dispatch round {rnd}: offered {need} x {kw_per_offer:.0f} kW, reserved banks "
                f"{_scalar(conn, RESERVED_BANKS_SQL, span)}/{home_banks} (target {target})"
            )
        reached = _scalar(conn, RESERVED_BANKS_SQL, span)
        states = {s: int(n) for s, n in conn.execute(STATES_SQL, (opportunity_ids,)).fetchall()}
    return {
        "window_start": window[0].isoformat(),
        "window_end": window[1].isoformat(),
        "home_banks": home_banks,
        "target_banks": target,
        "reserved_banks": reached,
        "reserved_frac": round(reached / home_banks, 3) if home_banks else None,
        "offers": len(opportunity_ids),
        "kw_per_offer": kw_per_offer,
        "states": states,
    }
