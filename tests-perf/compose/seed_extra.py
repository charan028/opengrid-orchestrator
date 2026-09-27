#!/usr/bin/env python3
"""tests-perf/compose/seed_extra.py -- the rest of the production seed order on the compose perf stack.

The dev stack's `migrate` service seeds only the fleet (`opengrid.fleet.seed`). The base bootstrap
(deploy/scripts/bootstrap_from_scratch.sh phase e) then also loads the market model, customer services,
services, the topology and the trucks, which production has. This runs those same seeds, in the same order,
inside the `migrate` container (tests-perf/compose/docker-compose.perf.yml), so the perf fleet has the
production shape. It uses psycopg only, because the dev image has no psql.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import psycopg

APP = Path("/app")
GEN = APP / "tests-perf" / "compose" / "generated"


def main() -> int:
    dsn = (
        f"host={os.environ.get('OG_PERF_DB_HOST', 'postgres')} port=5432 dbname={os.environ.get('OG_DB', 'og')} "
        f"user={os.environ.get('POSTGRES_USER', 'opengrid')} password={os.environ['OG_DB_PASSWORD']}"
    )
    seed = APP / "dev" / "seed"
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name in ("market_model_seed.sql", "customer_services_seed.sql", "services_seed.sql"):
            conn.execute(seed.joinpath(name).read_text(encoding="utf-8"))
            print(f"seed_extra: {name} applied")
    subprocess.run(
        [
            sys.executable,
            str(seed / "topology_seed.py"),
            "--fleet-config",
            str(GEN / "fleet.perf.yaml"),
            "--scada-config",
            str(GEN / "scada.perf.yaml"),
            "--dsn",
            dsn,
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    print("seed_extra: topology_seed.py applied")
    trucks = seed / "mobile_trucks_seed.sql"
    with psycopg.connect(dsn, autocommit=True) as conn:
        if trucks.exists():
            conn.execute(trucks.read_text(encoding="utf-8"))
            print("seed_extra: mobile_trucks_seed.sql applied")
        hubs = conn.execute("select count(*) from og.hub").fetchone()
        print(f"seed_extra: og.hub rows = {hubs[0] if hubs else 0}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
