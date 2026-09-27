"""Migration 0025 (two-market model) and dev/seed/market_model_seed.sql against real Postgres, plus the
$/kW read side (`opengrid.market.pg_backend`). Server only (`tools/remote.ps1`)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("OG_DB"), reason="requires the server environment (tools/remote.ps1)"
)

SEED = Path(__file__).resolve().parents[3] / "dev" / "seed" / "market_model_seed.sql"
DEMO_CONTRACT = "00000000-0000-7000-8000-00000000ae0d"


@pytest.fixture(scope="module")
def dsn(server_config, _migrated):
    from opengrid.platform.db import build_dsn

    return build_dsn(server_config)


def _insert_contract(cur: psycopg.Cursor, **kw: object) -> None:
    row = {
        "contract_id": uuid4(),
        "customer_id": uuid4(),
        "service_type": "ERCOT_ENERGY",
        "market": "FREE",
        "utility_id": None,
    }
    row.update(kw)
    cur.execute(
        "INSERT INTO og.contract (contract_id, customer_id, service_type, tier, profile_ref, start_at, "
        "market, utility_id) VALUES (%(contract_id)s, %(customer_id)s, %(service_type)s, 'T1', 'p@1', now(), "
        "%(market)s, %(utility_id)s)",
        row,
    )


def test_seed_applies_idempotently(dsn: str) -> None:
    sql = SEED.read_text(encoding="utf-8")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)
        conn.execute(sql)
        utilities = conn.execute("SELECT utility_id FROM og.utility ORDER BY 1").fetchall()
        assert [u[0] for u in utilities] == ["AUSTIN_ENERGY", "CPS_ENERGY", "LCRA", "RAYBURN"]  # D-37
        contract = conn.execute(
            "SELECT service_type, market, utility_id FROM og.contract WHERE contract_id = %s",
            (DEMO_CONTRACT,),
        ).fetchone()
        assert contract == ("REGULATED_CAPACITY", "REGULATED", "AUSTIN_ENERGY")
        asset = conn.execute(
            "SELECT asset_class, zone, bank_id, status FROM og.asset WHERE asset_id = 'sub-LZ_AEN-00'"
        ).fetchone()
        assert asset == ("SUBSTATION", "LZ_AEN", "bank-sub-LZ_AEN-00", "ACTIVE")
        rule = conn.execute(
            "SELECT min_qty_kw, duration_minutes FROM og.product_rule WHERE contract_id = %s",
            (DEMO_CONTRACT,),
        ).fetchone()
        assert rule is not None and (int(rule[0]), rule[1]) == (24000, 90)
        variant = conn.execute(
            "SELECT variant FROM og.contract WHERE contract_id = %s", (DEMO_CONTRACT,)
        ).fetchone()
        assert variant == ("TOLLING",)
        price = conn.execute(
            "SELECT capacity_price_usd_per_kw FROM og.utility WHERE utility_id = 'AUSTIN_ENERGY'"
        ).fetchone()
        assert price is not None and int(price[0]) == 102
        hub = conn.execute("SELECT bank_id, p_kw FROM og.hub WHERE hub_id = 'sub-LZ_AEN-00'").fetchone()
        assert hub == ("bank-sub-LZ_AEN-00", 20000.0)


@pytest.mark.parametrize(
    "kw",
    [
        {"service_type": "REGULATED_CAPACITY"},  # FREE by default: rejected
        {"market": "REGULATED"},  # no utility
        {"utility_id": "AUSTIN_ENERGY"},  # FREE naming a utility
        {"market": "SPOT"},
        {"service_type": "NOT_A_SERVICE"},
    ],
)
def test_market_checks_reject_inconsistent_contracts(dsn: str, kw: dict[str, object]) -> None:
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        conn.execute(SEED.read_text(encoding="utf-8"))
        with pytest.raises(psycopg.errors.CheckViolation):
            _insert_contract(cur, **kw)
        conn.rollback()


@pytest.mark.parametrize(
    "service_type",
    ["PIPELINE_AC", "REGULATED_CAPACITY", "PJM_CAPACITY", "MOBILE_STORAGE", "LARGE_LOAD", "HOME"],
)
def test_service_type_check_allows_every_core_service_type(dsn: str, service_type: str) -> None:
    regulated = service_type == "REGULATED_CAPACITY"
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        conn.execute(SEED.read_text(encoding="utf-8"))
        _insert_contract(
            cur,
            service_type=service_type,
            market="REGULATED" if regulated else "FREE",
            utility_id="AUSTIN_ENERGY" if regulated else None,
        )
        conn.rollback()


def test_substation_asset_needs_poi_limits(dsn: str) -> None:
    with psycopg.connect(dsn) as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO og.asset (asset_id, asset_class, feeder_id, zone, p_kw, e_kwh, eta_rt) "
                "VALUES ('sub-bad', 'SUBSTATION', 'feeder-x', 'LZ_AEN', 20000, 40000, 0.88)"
            )
        conn.rollback()


async def test_contract_totals_read_side(server_config, _migrated) -> None:
    from opengrid.market.pg_backend import fetch_contract_totals
    from opengrid.platform.db import make_pool

    pool = await make_pool(server_config)
    try:
        end = datetime.now(UTC) + timedelta(days=1)
        totals = await fetch_contract_totals(pool, end - timedelta(days=30), end)
    finally:
        await pool.close()
    for t in totals:
        assert t.scope_kind == "CONTRACT"
        assert t.kw_basis >= 0
