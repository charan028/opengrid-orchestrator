#!/usr/bin/env python3
"""deploy/scripts/bootstrap_check.py -- assert that a bootstrapped database holds the complete seeded solution.

Run by `bootstrap_from_scratch.sh` after phase (e) and by `make bootstrap-check`. Read-only: it only SELECTs.
Expected values are derived, never hard-coded twice: the fleet from `opengrid.fleet.seed.build_topology` over the
same generated sim config the seed used (`OG_FLEET_SIM_CONFIG`), the migrations from `orchestrator/migrations/`,
the firmware catalogue from `[firmware.catalogue]` in `OG_CONFIG`.

Environment: OG_CONFIG, OG_DB, OG_DB_PORT, OG_DB_USER, OG_DB_PASSWORD (the DSN is `opengrid.platform.db.build_dsn`),
OG_FLEET_SIM_CONFIG. `--live` adds the post-start checks (fresh hubs, invariants 0, degraded modes).
Exit 0 when every check passes, 1 otherwise. Prints counts only, never a credential.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

from opengrid.fleet.seed import build_topology, load_sim_fleet_topology_config
from opengrid.platform.config import load_config
from opengrid.platform.db import MIGRATIONS_DIR, build_dsn

SUBSTATION_ASSET = "sub-LZ_AEN-00"
TOLL_CONTRACT = "00000000-0000-7000-8000-00000000ae0d"
UTILITIES = ("AUSTIN_ENERGY", "CPS_ENERGY")
# 0002 demo + customer_services_seed.sql + services_seed.sql (the customer ids in [api.roles.customer]).
SEEDED_CONTRACTS = (
    "00000000-0000-7000-8000-000000000d02",
    "00000000-0000-7000-8000-000000000d04",
    "00000000-0000-7000-8000-000000000d05",
    "00000000-0000-7000-8000-0000000000d6",
    "00000000-0000-7000-8000-0000000000d7",
    "00000000-0000-7000-8000-0000000000d8",
    "00000000-0000-7000-8000-0000000000d9",
    "00000000-0000-7000-8000-0000000000da",
)


class Checker:
    def __init__(self, conn: psycopg.Connection[Any]) -> None:
        self.conn = conn
        self.failures = 0

    def scalar(self, query: Any, params: tuple[Any, ...] = ()) -> Any:
        row = self.conn.execute(query, params).fetchone()
        return None if row is None else row[0]

    def table_exists(self, name: str) -> bool:
        return bool(self.scalar("SELECT to_regclass(%s) IS NOT NULL", (name,)))

    def check(self, label: str, actual: Any, expected: Any, ok: bool | None = None) -> None:
        passed = (actual == expected) if ok is None else ok
        self.failures += 0 if passed else 1
        print(f"  [{'OK' if passed else 'FAIL'}] {label}: {actual} (expected {expected})")


def check_seeded(c: Checker, cfg: Any, trucks_expected: bool) -> None:
    files = sorted(p.name for p in Path(MIGRATIONS_DIR).glob("[0-9][0-9][0-9][0-9]_*.sql"))
    applied = {r[0] for r in c.conn.execute("SELECT filename FROM og.schema_migrations").fetchall()}
    c.check(
        "migrations applied",
        f"{len(set(files) & applied)}/{len(files)} (latest {files[-1]})",
        "all",
        ok=set(files) <= applied,
    )

    topo = build_topology(load_sim_fleet_topology_config(cfg))
    want_hubs = Counter(h.zone for h in topo.hubs)
    want_banks = len(topo.banks)
    have = dict(
        c.conn.execute(
            "SELECT zone, count(*) FROM og.hub WHERE hub_id LIKE 'hub-%%' GROUP BY zone"
        ).fetchall()
    )
    for zone in sorted(set(want_hubs) | set(have)):
        c.check(f"home hubs {zone}", have.get(zone, 0), want_hubs.get(zone, 0))
    c.check("home hubs total", sum(have.values()), len(topo.hubs))
    c.check("home banks", c.scalar("SELECT count(*) FROM og.bank WHERE bank_id LIKE 'bank-___'"), want_banks)
    c.check(
        "dual-unit hubs (20% rule)",
        c.scalar("SELECT count(*) FROM og.hub WHERE hub_id LIKE 'hub-%%' AND p_kw > 11"),
        sum(1 for h in topo.hubs if h.p_kw > 11),
    )

    c.check(
        "substation asset ACTIVE",
        c.scalar("SELECT status FROM og.asset WHERE asset_id = %s", (SUBSTATION_ASSET,)),
        "ACTIVE",
    )
    c.check(
        "substation bank + hub",
        c.scalar(
            "SELECT count(*) FROM og.hub h JOIN og.bank b USING (bank_id) WHERE b.bank_id = %s",
            (f"bank-{SUBSTATION_ASSET}",),
        ),
        1,
    )
    c.check(
        "utilities",
        sorted(
            r[0]
            for r in c.conn.execute(
                "SELECT utility_id FROM og.utility WHERE utility_id = ANY(%s)", (list(UTILITIES),)
            ).fetchall()
        ),
        sorted(UTILITIES),
    )
    c.check(
        "toll contract (REGULATED_CAPACITY/TOLLING)",
        c.scalar(
            "SELECT service_type || '/' || variant FROM og.contract WHERE contract_id = %s", (TOLL_CONTRACT,)
        ),
        "REGULATED_CAPACITY/TOLLING",
    )
    c.check(
        "AUSTIN_ENERGY capacity price $/kW-yr",
        c.scalar(
            "SELECT capacity_price_usd_per_kw::float FROM og.utility WHERE utility_id = 'AUSTIN_ENERGY'"
        ),
        102.0,
    )
    c.check(
        "seeded customer contracts",
        c.scalar(
            "SELECT count(*) FROM og.contract WHERE contract_id = ANY(%s::uuid[])", (list(SEEDED_CONTRACTS),)
        ),
        len(SEEDED_CONTRACTS),
    )
    n_contracts = c.scalar("SELECT count(*) FROM og.contract")
    print(f"  [info] contracts total: {n_contracts}")

    c.check(
        "hubs without a service transformer",
        c.scalar("SELECT count(*) FROM og.hub WHERE hub_id LIKE 'hub-%%' AND transformer_id IS NULL"),
        0,
    )
    feeders = {b.feeder_id for b in topo.banks if b.feeder_id}
    n_limits = c.scalar("SELECT count(*) FROM og.feeder_limit")
    c.check("feeder limits", n_limits, f">= {len(feeders)}", ok=n_limits >= len(feeders))
    n_tx = c.scalar("SELECT count(*) FROM og.service_transformer")
    n_sub = c.scalar("SELECT count(*) FROM og.substation_limit")
    n_assets = c.scalar("SELECT count(*) FROM og.asset")
    print(f"  [info] service transformers: {n_tx}, substation limits: {n_sub}, assets: {n_assets}")

    c.check(
        "charge window default (FLEET *)",
        c.scalar(
            "SELECT array_to_string(windows, ',') FROM og.owner_charge_window WHERE scope_kind = 'FLEET' AND scope_ref = '*'"
        ),
        "22:00-06:00",
    )
    catalogue = cfg.get("firmware.catalogue", []) or []
    c.check(
        "firmware catalogue (config entries; og.firmware_catalogue present)",
        len(catalogue),
        ">= 1",
        ok=len(catalogue) >= 1 and c.table_exists("og.firmware_catalogue"),
    )

    truck_table = next(
        (t for t in ("mobile_unit", "truck", "mobile_truck") if c.table_exists(f"og.{t}")), None
    )
    if truck_table:
        n = c.scalar(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier("og", truck_table)))
        c.check(
            f"trucks (og.{truck_table})",
            n,
            ">= 1" if trucks_expected else "any",
            ok=n >= 1 or not trucks_expected,
        )
    else:
        print(
            f"  [{'FAIL' if trucks_expected else 'SKIP'}] trucks: no truck table (mobile_trucks seed not in this release)"
        )
        c.failures += 1 if trucks_expected else 0


def check_live(c: Checker, stale_s: int) -> None:
    hubs = c.scalar("SELECT count(*) FROM og.hub")
    fresh = c.scalar(
        "SELECT count(*) FROM og.hub_state WHERE last_seen_at > now() - make_interval(secs => %s)", (stale_s,)
    )
    c.check(f"hubs fresh within {stale_s}s", f"{fresh}/{hubs}", "all", ok=hubs > 0 and fresh == hubs)
    c.check(
        "invariant violations (last run)",
        c.scalar("SELECT coalesce(sum(last_violations), 0) FROM og.invariant_check"),
        0,
    )
    modes = [r[0] for r in c.conn.execute("SELECT mode FROM og.degraded_mode_state ORDER BY 1").fetchall()]
    print(
        f"  [info] degraded modes: {', '.join(modes) or 'none'} (a fresh DB without backfill shows forecast modes)"
    )


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--live", action="store_true", help="also run the post-start checks")
    p.add_argument("--expect-trucks", action="store_true", help="fail when the trucks seed is absent")
    p.add_argument("--stale-s", type=int, default=25)
    args = p.parse_args(argv)
    cfg = load_config()
    with psycopg.connect(build_dsn(cfg), autocommit=True) as conn:
        c = Checker(conn)
        print(f"bootstrap check: database {cfg.postgres_database}")
        check_seeded(c, cfg, args.expect_trucks)
        if args.live:
            check_live(c, args.stale_s)
    print(f"bootstrap check: {'PASS' if c.failures == 0 else f'FAIL ({c.failures})'}")
    return 0 if c.failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
