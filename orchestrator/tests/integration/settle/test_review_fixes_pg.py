"""Review fixes (2026-09-26) against the real schema in the workspace database (`og_t_<ws>`):

- migration 0032 `og.hub.units`: default, CHECK, the dual-unit insert rule, and `guardian.repo.load_hub_params`
  populating `HubParams.units` so G-02's per-unit cap binds;
- M1: `PgSettleBackend.fetch_zone_charge_energy` (charging and grid-drawn charging per zone, PV surplus netted);
- AS flag: `PgSettleBackend.fetch_shortfall_risk_open` over `og.alert`;
- G-03: `PgBankStatePort` ignores SCADA readings that are not GOOD.

Every row is keyed by a unique id and removed again; nothing outside this test's own rows is touched."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.core.limits import check_hub_power
from opengrid.guardian import repo as guardian_repo
from opengrid.platform.config import load_config
from opengrid.platform.db import MIGRATIONS_DIR, build_dsn, migrate_sync
from opengrid.settle.pg_backend import PgSettleBackend

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="needs the workspace database (tools/remote.ps1)"),
]


@pytest.fixture
async def pool():
    dsn = build_dsn(load_config())
    migrate_sync(dsn)
    async with AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as opened:
        yield opened


async def _bank_with_hubs(pool, zone: str, hubs: list[tuple[str, float, float]]) -> str:
    """A bank in `zone` with `(hub_id, e_kwh, p_kw)` hubs inserted WITHOUT a units value (as the seed does)."""
    bank_id = f"bank-rvfx-{uuid4().hex[:8]}"
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES (%s, %s, 600)", (bank_id, zone)
        )
        for hub_id, e_kwh, p_kw in hubs:
            await cur.execute(
                "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw) VALUES (%s, %s, %s, %s, %s, %s)",
                (hub_id, bank_id, zone, e_kwh, e_kwh * 0.2, p_kw),
            )
    return bank_id


async def _drop(pool, bank_id: str) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "DELETE FROM og.telemetry WHERE hub_id IN (SELECT hub_id FROM og.hub WHERE bank_id = %s)",
            (bank_id,),
        )
        await cur.execute("DELETE FROM og.hub WHERE bank_id = %s", (bank_id,))
        await cur.execute("DELETE FROM og.feed_obs WHERE source = 'scada' AND product = %s", (bank_id,))
        await cur.execute("DELETE FROM og.bank WHERE bank_id = %s", (bank_id,))


# --- migration 0032 --------------------------------------------------------------------------------------------


async def test_hub_units_default_the_dual_unit_rule_and_the_check(pool):
    single, dual = f"hub-rvfx-{uuid4().hex[:8]}", f"hub-rvfx-{uuid4().hex[:8]}"
    bank_id = await _bank_with_hubs(pool, "LZ_NORTH", [(single, 39.2, 20.0), (dual, 78.4, 20.0)])
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT hub_id, units FROM og.hub WHERE bank_id = %s", (bank_id,))
            units = dict(await cur.fetchall())
        assert units == {single: 1, dual: 2}

        # guardian's own param load carries the count, so a single-unit home mis-seeded at 20 kW is vetoed
        params = await guardian_repo.load_hub_params(pool)
        assert params[single].params.units == 1 and params[dual].params.units == 2
        assert not check_hub_power(-20.0, params[single].params).ok
        assert check_hub_power(-20.0, params[dual].params).ok

        async with pool.connection() as conn, conn.cursor() as cur:
            with pytest.raises(psycopg.errors.CheckViolation):
                await cur.execute("UPDATE og.hub SET units = 3 WHERE hub_id = %s", (single,))
    finally:
        await _drop(pool, bank_id)


async def test_migration_0032_is_idempotent(pool):
    """Re-running the file (as a fresh workspace or a re-applied migration would) changes nothing and keeps a
    hand-corrected unit count."""
    hub = f"hub-rvfx-{uuid4().hex[:8]}"
    bank_id = await _bank_with_hubs(pool, "LZ_NORTH", [(hub, 78.4, 20.0)])
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("UPDATE og.hub SET units = 1 WHERE hub_id = %s", (hub,))  # a hand correction
            await cur.execute((MIGRATIONS_DIR / "0032_hub_units.sql").read_text(encoding="utf-8"))
            await cur.execute("SELECT units FROM og.hub WHERE hub_id = %s", (hub,))
            assert (await cur.fetchone())[0] == 1
    finally:
        await _drop(pool, bank_id)


# --- M1 zone charge energy ----------------------------------------------------------------------------------


async def test_zone_charge_energy_nets_the_pv_surplus(pool):
    zone = f"LZ_RVFX_{uuid4().hex[:6].upper()}"
    hub = f"hub-rvfx-{uuid4().hex[:8]}"
    bank_id = await _bank_with_hubs(pool, zone, [(hub, 39.2, 11.0)])
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(hours=2)
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            rows = [
                (0, Decimal("8"), Decimal("5"), Decimal("1")),  # 8 kW charging, PV surplus 4 -> 4 from grid
                (1, Decimal("8"), None, None),  # no PV reading: all 8 from grid
                (2, Decimal("-5"), Decimal("9"), Decimal("0")),  # discharging: not charging at all
            ]
            for i, p_kw, pv_kw, load_kw in rows:
                await cur.execute(
                    """INSERT INTO og.telemetry (hub_id, ts, soc_kwh, p_kw, seq, epoch, health, pv_kw, home_load_kw)
                       VALUES (%s, %s, 20, %s, %s, 1, 'online', %s, %s)""",
                    (hub, start + timedelta(minutes=i), p_kw, i, pv_kw, load_kw),
                )
        backend = PgSettleBackend(pool)
        energy = await backend.fetch_zone_charge_energy(zone, start, start + timedelta(hours=1))
        assert energy.charge_kw_sum == Decimal("16")
        assert energy.grid_charge_kw_sum == Decimal("12")
        assert energy.grid_share == Decimal("0.75")

        empty = await backend.fetch_zone_charge_energy("LZ_NOWHERE", start, start + timedelta(hours=1))
        assert empty.charge_kw_sum == 0 and empty.grid_share == Decimal("1")
    finally:
        await _drop(pool, bank_id)


# --- AS hold shortfall flag -----------------------------------------------------------------------------------


async def test_shortfall_risk_open_during_the_interval(pool):
    from opengrid.health.model import AlertFinding
    from opengrid.health.queries import raise_alert
    from opengrid.health.rules import evaluate_energy_shortfall_risk_alert

    obligation_id = uuid4()
    finding: AlertFinding = evaluate_energy_shortfall_risk_alert(
        obligation_id=str(obligation_id), customer_id=None, margin_kwh=-3.0, time_to_depletion_h=None
    )
    interval_start = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=30)
    alert_id = await raise_alert(pool, finding, opened_at=interval_start + timedelta(minutes=5))
    backend = PgSettleBackend(pool)
    try:
        assert await backend.fetch_shortfall_risk_open(
            obligation_id, interval_start, interval_start + timedelta(minutes=15)
        )
        # an interval that ended before the alert opened is not flagged
        assert not await backend.fetch_shortfall_risk_open(
            obligation_id, interval_start - timedelta(minutes=30), interval_start - timedelta(minutes=15)
        )
        assert not await backend.fetch_shortfall_risk_open(
            uuid4(), interval_start, interval_start + timedelta(minutes=15)
        )
    finally:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM og.alert WHERE id = %s", (alert_id,))


# --- G-03 SCADA quality -----------------------------------------------------------------------------------------


async def test_g03_ignores_scada_readings_that_are_not_good(pool):
    bank_id = await _bank_with_hubs(pool, "LZ_NORTH", [])
    now = datetime.now(UTC)
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality)
                   VALUES ('scada', %s, 'APPARENT_POWER_KVA', %s, 0, 'kVA', 'STALE')""",
                (bank_id, now),
            )
        stale_only = await guardian_repo.PgBankStatePort(pool).snapshot(bank_id)
        assert stale_only is not None and stale_only.bank_load_age_s == float("inf")

        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                """INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality)
                   VALUES ('scada', %s, 'APPARENT_POWER_KVA', %s, 250, 'kVA', 'GOOD')""",
                (bank_id, now - timedelta(seconds=5)),
            )
        good = await guardian_repo.PgBankStatePort(pool).snapshot(bank_id)
        assert good is not None and good.bank_load_kva == 250.0  # the newer STALE 0 kVA is ignored
    finally:
        await _drop(pool, bank_id)
