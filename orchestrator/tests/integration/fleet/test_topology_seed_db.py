"""dev/seed/topology_seed.py against real Postgres (test cluster only, never 5432): bootstrap phase e's seeds in
their order on a fresh database -- fleet (production's zone blocks: LZ_AEN + LZ_LCRA + LZ_RAYBN), market model
(the 20 MW substation set), trucks, topology -- then every `opengrid.fleet.topology_audit` count is 0 and the
guardian's own topology adapter (`PgGridTopologyPort`, the read behind ALR-XFMR-UNMAPPED and
ALR-BANK-UNMAPPED-TOPOLOGY) finds every hub on a rated transformer and every bank on a feeder. Then the
production backfill (`--only-missing`) from r3.4's state: only the missing rows, nothing existing rewritten.

Server only, 5433 test cluster (`OG_DB_PORT=5433`). Runs in its own fresh database `<OG_DB>_topo` (created,
migrated and dropped by the module), so it is independent of what other tests leave in `OG_DB`."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

pytestmark = [
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="requires the server environment (OG_DB)"),
    pytest.mark.skipif(
        os.environ.get("PGPORT", os.environ.get("OG_DB_PORT", "5432")) == "5432",
        reason="writes and deletes topology rows: test cluster (5433) only",
    ),
]

REPO = Path(__file__).resolve().parents[4]
SEED_DIR = REPO / "dev" / "seed"
# The unit module loads dev/seed/topology_seed.py (as `ts`) and builds production's sim configs.
_spec = importlib.util.spec_from_file_location(
    "unit_test_topology_seed", REPO / "orchestrator" / "tests" / "unit" / "fleet" / "test_topology_seed.py"
)
assert _spec is not None and _spec.loader is not None
_unit = importlib.util.module_from_spec(_spec)
sys.modules["unit_test_topology_seed"] = _unit
_spec.loader.exec_module(_unit)
ts = _unit.ts
production_like_configs = _unit.production_like_configs

TRUCKS = 8


def _bank_hub_fingerprint(conn: psycopg.Connection[Any]) -> tuple[str, str]:
    row = conn.execute(
        "SELECT (SELECT md5(string_agg(to_jsonb(b)::text, ',' ORDER BY bank_id)) FROM og.bank b),"
        " (SELECT md5(string_agg((to_jsonb(h) - 'transformer_id')::text, ',' ORDER BY hub_id)) FROM og.hub h)"
    ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def _unmapped(dsn: str) -> dict[str, int]:
    from opengrid.fleet.topology_audit import count_unmapped

    with psycopg.connect(dsn, autocommit=True) as conn:
        return count_unmapped(conn)


@pytest.fixture(scope="module")
def own_config(server_config) -> Iterator[Any]:
    """Isolation: a database of this module's own (`<OG_DB>_topo`, created fresh and migrated, dropped
    afterwards), so rows other tests leave in the shared `OG_DB` never reach the global unmapped counts."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    from opengrid.platform.config import Config
    from opengrid.platform.db import build_dsn, migrate_sync

    name = f"{server_config.postgres_database}_topo"
    params = conninfo_to_dict(build_dsn(server_config))
    admin = make_conninfo(**{**params, "dbname": "postgres"})

    def _drop() -> None:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')

    _drop()
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    cfg = Config(
        {
            "postgres": {
                "host": server_config.get("postgres.host", "127.0.0.1"),
                "port": int(server_config.get("postgres.port", 5432)),
                "database": name,
                "pool_min": 1,
                "pool_max": 4,
            }
        }
    )
    migrate_sync(build_dsn(cfg))
    try:
        yield cfg
    finally:
        _drop()


@pytest.fixture(scope="module")
def dsn(own_config) -> str:
    from opengrid.platform.db import build_dsn

    return str(build_dsn(own_config))


@pytest.fixture(scope="module")
def configs(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    return production_like_configs(tmp_path_factory.mktemp("sim"))


@pytest.fixture(scope="module")
def seeded(dsn: str, own_config, configs: tuple[Path, Path]) -> str:
    """Phase e up to the topology seed on the fresh database: fleet, market model, trucks."""
    from opengrid.fleet.seed import build_topology, seed_topology
    from opengrid.platform.db import make_pool

    fleet, scada = configs

    async def _fleet() -> None:
        pool = await make_pool(own_config)
        try:
            await seed_topology(pool, build_topology(ts.load_fleet_config(fleet, scada)))
        finally:
            await pool.close()

    asyncio.run(_fleet())
    with psycopg.connect(dsn, autocommit=True) as conn:
        for name in (
            "market_model_seed.sql",
            "noie_switch_seed.sql",
            "mobile_trucks_seed.sql",
        ):  # phase e order
            conn.execute((SEED_DIR / name).read_text(encoding="utf-8").encode("utf-8"))
    return dsn


def _run_seed(dsn: str, configs: tuple[Path, Path], *extra: str) -> int:
    fleet, scada = configs
    return int(ts.main(["--fleet-config", str(fleet), "--scada-config", str(scada), "--dsn", dsn, *extra]))


def test_fresh_bootstrap_has_zero_unmapped_and_a_clean_guardian_topology(
    seeded: str, configs: tuple[Path, Path], own_config
) -> None:
    before = _unmapped(seeded)
    assert before["hubs without a service transformer (ALR-XFMR-UNMAPPED)"] == 3500 + 1 + TRUCKS
    assert _run_seed(seeded, configs) == 0
    assert set(_unmapped(seeded).values()) == {0}

    with psycopg.connect(seeded, autocommit=True) as conn:
        dedicated = dict(
            conn.execute(
                "SELECT h.hub_id, st.rating_kva FROM og.hub h JOIN og.service_transformer st"
                " ON st.transformer_id = h.transformer_id WHERE h.hub_id LIKE 'truck-%' OR h.hub_id LIKE 'sub-%'"
            ).fetchall()
        )
        truck_feeders = conn.execute(
            "SELECT count(*) FROM og.feeder_limit WHERE feeder_id LIKE 'feeder-truck-%' AND reverse_kw = 600"
        ).fetchone()
        sub_feeder = conn.execute(
            "SELECT thermal_kw, reverse_kw FROM og.feeder_limit WHERE feeder_id = 'feeder-sub-LZ_AEN-00'"
        ).fetchone()
        lcra = conn.execute(
            "SELECT DISTINCT a.zone, a.utility_id FROM og.asset a WHERE a.bank_id BETWEEN 'bank-050' AND 'bank-059'"
        ).fetchall()
        off_rating = conn.execute(
            "SELECT count(*) FROM og.bank b JOIN (SELECT bank_id, sum(rating_kva) AS kva FROM og.service_transformer"
            " GROUP BY bank_id) t ON t.bank_id = b.bank_id WHERE abs(t.kva - b.kva_rating) > 0.001"
        ).fetchone()
        hub_ids = [r[0] for r in conn.execute("SELECT hub_id FROM og.hub ORDER BY 1").fetchall()]
        bank_ids = [r[0] for r in conn.execute("SELECT bank_id FROM og.bank ORDER BY 1").fetchall()]
    assert len(dedicated) == 1 + TRUCKS
    assert dedicated["sub-LZ_AEN-00"] == pytest.approx(20408.0)
    assert {v for k, v in dedicated.items() if k.startswith("truck-")} == {600.0}
    assert truck_feeders == (TRUCKS,)
    assert sub_feeder == (24000.0, 20000.0)  # market_model_seed.sql's row kept
    assert lcra == [("LZ_LCRA", "LCRA")]  # D-37: regulated territory, not the ERCOT competitive area
    assert off_rating == (0,)  # D-36: every bank's transformers sum to its og.bank.kva_rating

    # The guardian's own read: the conditions under which it raises ALR-XFMR-UNMAPPED /
    # ALR-BANK-UNMAPPED-TOPOLOGY (guardian.service._check_transformers / _check_unmapped_bank).
    from opengrid.guardian.config import GuardianConfig
    from opengrid.guardian.flow_repo import PgGridTopologyPort
    from opengrid.platform.db import make_pool

    async def _guardian_view() -> tuple[list[str], list[str]]:
        pool = await make_pool(own_config)
        try:
            port = PgGridTopologyPort(pool, GuardianConfig(key_path="unused"), {"LZ_AEN": "AUSTIN_ENERGY"})
            unmapped_hubs = []
            for hub_id in hub_ids:
                site = await port.hub_site(hub_id)
                xfmr = await port.transformer(site.transformer_id) if site and site.transformer_id else None
                if xfmr is None or hub_id not in xfmr.members:
                    unmapped_hubs.append(hub_id)
            topology = await port._current()
            unmapped_banks = [b for b in bank_ids if not any(b in m for m in topology.feeder_banks.values())]
            return unmapped_hubs, unmapped_banks
        finally:
            await pool.close()

    assert asyncio.run(_guardian_view()) == ([], [])

    # Idempotent: a second run and an --only-missing dry run change nothing.
    with psycopg.connect(seeded, autocommit=True) as conn:
        fingerprint = _bank_hub_fingerprint(conn)
    assert _run_seed(seeded, configs) == 0
    assert _run_seed(seeded, configs, "--only-missing", "--dry-run") == 0
    with psycopg.connect(seeded, autocommit=True) as conn:
        assert _bank_hub_fingerprint(conn) == fingerprint


def test_only_missing_backfills_r34_state_without_rewriting_a_row(
    seeded: str, configs: tuple[Path, Path], capsys: pytest.CaptureFixture[str]
) -> None:
    """r3.4 production: no transformers, no HOME_BANK assets, only the substation set's limits; one
    operator-edited limit row must survive untouched."""
    with psycopg.connect(seeded, autocommit=True) as conn:
        conn.execute("UPDATE og.hub SET transformer_id = NULL")
        conn.execute("DELETE FROM og.service_transformer")
        conn.execute("DELETE FROM og.asset WHERE asset_class = 'HOME_BANK'")
        conn.execute(
            "DELETE FROM og.feeder_limit WHERE feeder_id NOT IN ('feeder-sub-LZ_AEN-00', 'feeder-LZ_WEST-00')"
        )
        conn.execute("UPDATE og.feeder_limit SET thermal_kw = 7777 WHERE feeder_id = 'feeder-LZ_WEST-00'")
        conn.execute("DELETE FROM og.substation_limit WHERE substation_id <> 'sub-LZ_AEN-00'")
        fingerprint = _bank_hub_fingerprint(conn)
    assert _unmapped(seeded)["hubs without a service transformer (ALR-XFMR-UNMAPPED)"] == 3509

    capsys.readouterr()
    assert _run_seed(seeded, configs, "--only-missing", "--dry-run") == 0
    plan = capsys.readouterr().out
    assert "planned (dry run, rolled back):" in plan and "existing rows unchanged: OK" in plan
    assert _unmapped(seeded)["hubs without a service transformer (ALR-XFMR-UNMAPPED)"] == 3509  # rolled back

    assert _run_seed(seeded, configs, "--only-missing") == 0
    assert set(_unmapped(seeded).values()) == {0}
    with psycopg.connect(seeded, autocommit=True) as conn:
        assert _bank_hub_fingerprint(conn) == fingerprint
        assert conn.execute(
            "SELECT thermal_kw FROM og.feeder_limit WHERE feeder_id = 'feeder-LZ_WEST-00'"
        ).fetchone() == (7777.0,)

    capsys.readouterr()
    assert _run_seed(seeded, configs, "--only-missing") == 0
    counts = [
        line.split()[0]
        for line in capsys.readouterr().out.splitlines()
        if line.startswith("  ") and line.split()[0].isdigit()
    ]
    assert counts and set(counts) == {"0"}  # a second backfill inserts nothing
