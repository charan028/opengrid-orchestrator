"""Integration test for `settle()` against real Postgres (`og_t_settle` on the server, BUILD.md S5).

Run via: `powershell -File tools\\remote.ps1 -Ws settle -Cmd "cd orchestrator && bash tools/check.sh"`
(or directly: `pytest tests/integration/settle -q` inside the `settle` workspace, where `OG_DB`/
`OG_DB_PASSWORD` are already set by `remote.ps1`). Skipped automatically wherever Postgres is not
reachable (e.g. this repo's local Windows dev box, which has no server -- BUILD.md S5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest

import opengrid.settle as settle_module
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, make_pool, migrate_sync
from opengrid.settle import settle
from opengrid.settle.pg_backend import PgSettleBackend
from opengrid.trace import TraceStore
from opengrid.trace.pg_backend import PgTraceBackend


def _try_connect_dsn() -> str | None:
    try:
        cfg = load_config()
    except Exception:
        return None
    dsn = build_dsn(cfg)
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return dsn
    except Exception:
        return None


_DSN = _try_connect_dsn()
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(_DSN is None, reason="Postgres not reachable (BUILD.md S5)"),
]


@pytest.fixture
async def pg_pool():
    migrate_sync(_DSN)
    cfg = load_config()
    pool = await make_pool(cfg)
    yield pool
    await pool.close()


async def _insert_fixture(pool, *, committed_kw: Decimal) -> tuple:
    contract_id = uuid4()
    opportunity_id = uuid4()
    obligation_id = uuid4()
    # Unique per test run: a shared bank/hub let an earlier run's telemetry and grants in overlapping
    # minutes leak into this run's metering.
    bank_id = f"bank-settle-it-{uuid4().hex[:8]}"
    hub_id = f"hub-settle-it-{uuid4().hex[:8]}"

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """INSERT INTO og.contract
                (contract_id, customer_id, service_type, tier, profile_ref, start_at,
                 penalty_alpha, penalty_beta, penalty_theta, degradation_cost)
               VALUES (%s, %s, 'DIST_DEFERRAL', 'T1', 'it-profile@1', now(), 0.01, 0.5, 0.05, 0.03)""",
            (contract_id, uuid4()),
        )
        await cur.execute(
            """INSERT INTO og.opportunity
                (opportunity_id, contract_id, window_start, window_end, requested_kw, value_per_mwh)
               VALUES (%s, %s, now() - interval '1 hour', now() + interval '1 hour', %s, 100.0)""",
            (opportunity_id, contract_id, committed_kw),
        )
        await cur.execute(
            """INSERT INTO og.obligation
                (obligation_id, opportunity_id, contract_id, service_type, tier, window_start,
                 window_end, committed_qty_kw, state)
               VALUES (%s, %s, %s, 'DIST_DEFERRAL', 'T1', now() - interval '1 hour',
                       now() + interval '1 hour', %s, 'DELIVERING')""",
            (obligation_id, opportunity_id, contract_id, committed_kw),
        )
        await cur.execute(
            """INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES (%s, 'zone-it', 500)
               ON CONFLICT (bank_id) DO NOTHING""",
            (bank_id,),
        )
        await cur.execute(
            """INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw)
               VALUES (%s, %s, 'zone-it', 39.2, 7.84, 11) ON CONFLICT (hub_id) DO NOTHING""",
            (hub_id, bank_id),
        )
        await cur.execute(
            """INSERT INTO og.reservation
                (reservation_id, obligation_id, bank_id, kind, amount, interval_start, interval_end,
                 ledger_version)
               VALUES (%s, %s, %s, 'POWER_KW', %s, now() - interval '1 hour', now() + interval '1 hour', 1)""",
            (uuid4(), obligation_id, bank_id, committed_kw),
        )
    return obligation_id, hub_id


async def _insert_zoned_fixture(pool, *, zone: str, value_per_mwh: Decimal, committed_kw: Decimal) -> tuple:
    """A realistic ERCOT_ENERGY obligation on one bank in `zone` (a unique LZ_* name per test)."""
    contract_id, opportunity_id, obligation_id = uuid4(), uuid4(), uuid4()
    bank_id = f"bank-spp-it-{uuid4().hex[:8]}"
    hub_id = f"hub-spp-it-{uuid4().hex[:8]}"
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """INSERT INTO og.contract
                (contract_id, customer_id, service_type, tier, profile_ref, start_at,
                 penalty_alpha, penalty_beta, penalty_theta, degradation_cost)
               VALUES (%s, %s, 'ERCOT_ENERGY', 'T2', 'it-profile@1', now(), 0.01, 0.5, 0.05, 0.02)""",
            (contract_id, uuid4()),
        )
        await cur.execute(
            """INSERT INTO og.opportunity
                (opportunity_id, contract_id, window_start, window_end, requested_kw, value_per_mwh)
               VALUES (%s, %s, now() - interval '3 hours', now() + interval '1 hour', %s, %s)""",
            (opportunity_id, contract_id, committed_kw, value_per_mwh),
        )
        await cur.execute(
            """INSERT INTO og.obligation
                (obligation_id, opportunity_id, contract_id, service_type, tier, window_start,
                 window_end, committed_qty_kw, state)
               VALUES (%s, %s, %s, 'ERCOT_ENERGY', 'T2', now() - interval '3 hours',
                       now() + interval '1 hour', %s, 'DELIVERING')""",
            (obligation_id, opportunity_id, contract_id, committed_kw),
        )
        await cur.execute(
            "INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES (%s, %s, 500)", (bank_id, zone)
        )
        await cur.execute(
            """INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw)
               VALUES (%s, %s, %s, 39.2, 7.84, 11)""",
            (hub_id, bank_id, zone),
        )
        await cur.execute(
            """INSERT INTO og.reservation
                (reservation_id, obligation_id, bank_id, kind, amount, interval_start, interval_end,
                 ledger_version)
               VALUES (%s, %s, %s, 'POWER_KW', %s, now() - interval '3 hours', now() + interval '1 hour', 1)""",
            (uuid4(), obligation_id, bank_id, committed_kw),
        )
    return obligation_id, hub_id


async def _insert_spp(pool, zone: str, ts: datetime, usd_per_mwh: float) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality)
               VALUES ('ERCOT', 'np6-905-cd', %s, %s, %s, 'usd_per_mwh', 'GOOD')""",
            (zone, ts, usd_per_mwh),
        )


def _quarter(minutes_ago: int) -> datetime:
    now = datetime.now(UTC).replace(second=0, microsecond=0)
    return now.replace(minute=now.minute - now.minute % 15) - timedelta(minutes=minutes_ago)


async def test_wholesale_price_is_the_zone_spp_not_the_contract_price(pg_pool):
    """Regression (2026-09-26): wholesale mirrored the contract's value_per_mwh, so energy_cost =
    revenue / eta_d > revenue and every delivery settled at a loss. Wholesale must be the bank zone's
    real-time SPP for the interval (NP6-905-CD), independent of the contract price."""
    zone = f"LZ_IT_{uuid4().hex[:6].upper()}"
    obligation_id, hub_id = await _insert_zoned_fixture(
        pg_pool, zone=zone, value_per_mwh=Decimal("45.00"), committed_kw=Decimal("10")
    )
    interval_start = _quarter(60)
    interval_end = interval_start + timedelta(minutes=15)
    await _insert_spp(pg_pool, zone, interval_start - timedelta(minutes=15), 99.0)  # older: not used
    await _insert_spp(pg_pool, zone, interval_start, 16.47)
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("-10"))
    async with pg_pool.connection() as conn, conn.cursor() as cur:
        for i in range(15):  # the obligation holds the whole bank grant each minute
            await cur.execute(
                """INSERT INTO og.grant
                    (grant_id, cycle_id, obligation_id, bank_id, granted_kw, ledger_version, created_at)
                   SELECT %s, %s, %s, bank_id, 10, 1, %s FROM og.hub WHERE hub_id = %s""",
                (
                    uuid4(),
                    f"it-{uuid4().hex[:8]}",
                    obligation_id,
                    interval_start + timedelta(minutes=i),
                    hub_id,
                ),
            )

    backend = PgSettleBackend(pg_pool)
    ctx = await backend.fetch_context(obligation_id, interval_start)
    assert ctx.price_per_kwh == Decimal("0.045")
    assert ctx.wholesale_price_per_kwh == Decimal("0.01647")
    assert ctx.wholesale_price_per_kwh != ctx.price_per_kwh
    assert ctx.wholesale_price_flag == "SPP"

    settle_module.configure(backend, TraceStore(PgTraceBackend(pg_pool)), trace_pool=pg_pool)
    await settle(obligation_id, interval_start, interval_end)
    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT revenue, energy_cost FROM og.pnl WHERE obligation_id = %s AND superseded_by IS NULL",
            (obligation_id,),
        )
        revenue, energy_cost = await cur.fetchone()
    # D-10: ERCOT_ENERGY revenue is delivered kWh x the zone SPP at delivery -- never the contract price.
    delivered_kwh = Decimal("2.5")  # 10 kW for the whole 15-minute interval
    assert revenue == (delivered_kwh * Decimal("0.01647")).quantize(Decimal("0.000001"))
    assert revenue != (delivered_kwh * ctx.price_per_kwh).quantize(Decimal("0.000001"))
    # G4: energy cost is what was paid to CHARGE (the trailing off-peak SPP proxy) / eta_d, not the
    # discharge-interval SPP. Its size depends on the wall-clock hour (the 99 $/MWh row above counts as
    # off-peak at night), which is why the old `energy_cost < revenue` check failed overnight.
    expected_cost = (ctx.charging_cost_per_kwh / ctx.eta_d * delivered_kwh).quantize(Decimal("0.000001"))
    assert abs(energy_cost - expected_cost) <= Decimal("0.000001")


async def test_wholesale_price_falls_back_to_prior_spp_within_one_hour_then_missing(pg_pool):
    zone = f"LZ_IT_{uuid4().hex[:6].upper()}"
    obligation_id, _hub_id = await _insert_zoned_fixture(
        pg_pool, zone=zone, value_per_mwh=Decimal("45.00"), committed_kw=Decimal("10")
    )
    interval_start = _quarter(60)
    await _insert_spp(pg_pool, zone, interval_start - timedelta(minutes=30), 18.25)
    await _insert_spp(pg_pool, zone, interval_start - timedelta(minutes=90), 50.0)  # beyond 1 h

    backend = PgSettleBackend(pg_pool)
    prior = await backend.fetch_context(obligation_id, interval_start)
    assert prior.wholesale_price_per_kwh == Decimal("0.01825")
    assert prior.wholesale_price_flag == "SPP_PRIOR"

    # 3 h earlier: the only SPPs are after that interval or more than 1 h before it.
    missing = await backend.fetch_context(obligation_id, interval_start - timedelta(minutes=180))
    assert missing.wholesale_price_per_kwh == Decimal("0")
    assert missing.wholesale_price_flag == "MISSING"


async def _insert_telemetry(
    pool, hub_id: str, interval_start: datetime, kw: Decimal, count: int = 15
) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        for i in range(count):
            await cur.execute(
                """INSERT INTO og.telemetry (hub_id, ts, soc_kwh, p_kw, seq, epoch, health)
                   VALUES (%s, %s, 20, %s, %s, 1, 'online')""",
                (hub_id, interval_start + timedelta(minutes=i), kw, i),
            )


async def _insert_grants(pool, obligation_id, hub_id: str, interval_start: datetime, kw: Decimal) -> None:
    """One og.grant per minute giving the obligation the whole bank grant: `_FETCH_TELEMETRY_SQL`
    attributes a bank's measured discharge to an obligation by its share of that minute's grants, so an
    interval with no grant meters 0 kWh however much the hubs discharged."""
    async with pool.connection() as conn, conn.cursor() as cur:
        for i in range(15):
            await cur.execute(
                """INSERT INTO og.grant
                    (grant_id, cycle_id, obligation_id, bank_id, granted_kw, ledger_version, created_at)
                   SELECT %s, %s, %s, bank_id, %s, 1, %s FROM og.hub WHERE hub_id = %s""",
                (
                    uuid4(),
                    f"it-{uuid4().hex[:8]}",
                    obligation_id,
                    kw,
                    interval_start + timedelta(minutes=i),
                    hub_id,
                ),
            )


async def _insert_as_fixture(pool, *, committed_kw: Decimal, mcpc_usd_per_mwh: Decimal) -> tuple:
    """An ERCOT_AS obligation held at 0 kW (no deployment) -- the live-bug fixture (2026-09-26): every
    AS settlement wrote `og.performance.compliance_pct = NULL`, violating that column's NOT NULL
    constraint (0001_init.sql), so no AS interval settled from the 13:00 deploy onward."""
    contract_id, opportunity_id, obligation_id = uuid4(), uuid4(), uuid4()
    bank_id = f"bank-as-it-{uuid4().hex[:8]}"
    hub_id = f"hub-as-it-{uuid4().hex[:8]}"
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """INSERT INTO og.contract
                (contract_id, customer_id, service_type, tier, profile_ref, start_at,
                 penalty_alpha, penalty_beta, penalty_theta, degradation_cost)
               VALUES (%s, %s, 'ERCOT_AS', 'T2', 'it-profile@1', now(), 0.02, 0.30, 0.10, 0.03)""",
            (contract_id, uuid4()),
        )
        await cur.execute(
            """INSERT INTO og.opportunity
                (opportunity_id, contract_id, window_start, window_end, requested_kw, value_per_mwh)
               VALUES (%s, %s, now() - interval '1 hour', now() + interval '3 hours', %s, %s)""",
            (opportunity_id, contract_id, committed_kw, mcpc_usd_per_mwh),
        )
        await cur.execute(
            """INSERT INTO og.obligation
                (obligation_id, opportunity_id, contract_id, service_type, tier, window_start,
                 window_end, committed_qty_kw, state)
               VALUES (%s, %s, %s, 'ERCOT_AS', 'T2', now() - interval '1 hour',
                       now() + interval '3 hours', %s, 'DELIVERING')""",
            (obligation_id, opportunity_id, contract_id, committed_kw),
        )
        await cur.execute(
            "INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES (%s, 'zone-as-it', 500)", (bank_id,)
        )
        await cur.execute(
            """INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw)
               VALUES (%s, %s, 'zone-as-it', 39.2, 7.84, 11)""",
            (hub_id, bank_id),
        )
        await cur.execute(
            """INSERT INTO og.reservation
                (reservation_id, obligation_id, bank_id, kind, amount, interval_start, interval_end,
                 ledger_version)
               VALUES (%s, %s, %s, 'POWER_KW', %s, now() - interval '1 hour', now() + interval '3 hours', 1)""",
            (uuid4(), obligation_id, bank_id, committed_kw),
        )
    return obligation_id, hub_id


async def test_ercot_as_held_writes_numeric_compliance_pct_not_null(pg_pool):
    """Regression for the live bug (2026-09-26, obligation 8e1cdcde's 12:45 interval and every AS
    interval since the 13:00 deploy): a held-not-deployed AS interval (zero telemetry, delivered_kwh
    = 0) must still write a NUMERIC `og.performance.compliance_pct` -- the column is NOT NULL -- or
    `settle()` raises and no AS obligation ever settles again."""
    obligation_id, _hub_id = await _insert_as_fixture(
        pg_pool, committed_kw=Decimal("500"), mcpc_usd_per_mwh=Decimal("8.50")
    )
    interval_start = _quarter(20)
    interval_end = interval_start + timedelta(minutes=15)
    # No telemetry inserted at all: held, never deployed this interval (delivered_kwh settles as 0).

    settle_module.configure(PgSettleBackend(pg_pool), TraceStore(PgTraceBackend(pg_pool)), trace_pool=pg_pool)
    await settle(obligation_id, interval_start, interval_end)  # must not raise (the live bug did)

    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT compliance_pct, passed_threshold FROM og.performance WHERE obligation_id = %s",
            (obligation_id,),
        )
        compliance_pct, passed_threshold = await cur.fetchone()
        await cur.execute(
            "SELECT revenue, penalty FROM og.pnl WHERE obligation_id = %s AND superseded_by IS NULL",
            (obligation_id,),
        )
        revenue, penalty = await cur.fetchone()
        await cur.execute(
            "SELECT amount FROM og.invoice_line WHERE obligation_id = %s AND line_type = 'CAPACITY_PAYMENT'",
            (obligation_id,),
        )
        (capacity_amount,) = await cur.fetchone()

    assert compliance_pct is not None
    assert compliance_pct == Decimal("1.0000")
    assert passed_threshold is True
    assert penalty == Decimal("0")
    # committed_kwh = 500 kW * 0.25 h = 125 kWh; MCPC $8.50/MWh = $0.0085/kWh -> 125 * 0.0085 = 1.0625.
    assert revenue == Decimal("1.0625")
    assert capacity_amount == Decimal("1.0625")


async def test_settle_persists_meter_performance_invoice_and_pnl(pg_pool):
    obligation_id, hub_id = await _insert_fixture(pg_pool, committed_kw=Decimal("4"))
    interval_start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=20)
    interval_end = interval_start + timedelta(minutes=15)
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("4"))

    settle_module.configure(PgSettleBackend(pg_pool), TraceStore(PgTraceBackend(pg_pool)), trace_pool=pg_pool)
    await settle(obligation_id, interval_start, interval_end)

    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT delivered_kwh FROM og.meter_interval WHERE obligation_id = %s", (obligation_id,)
        )
        meter_rows = await cur.fetchall()
        await cur.execute("SELECT net_value FROM og.pnl WHERE obligation_id = %s", (obligation_id,))
        pnl_rows = await cur.fetchall()
        await cur.execute(
            "SELECT amount, status FROM og.invoice_line WHERE obligation_id = %s", (obligation_id,)
        )
        invoice_rows = await cur.fetchall()

    assert len(meter_rows) == 1
    assert len(pnl_rows) == 1
    assert len(invoice_rows) >= 1


async def test_rerunning_settle_is_idempotent_no_duplicate_rows(pg_pool):
    """TS-08-01 applied at the settle layer: re-running settlement for an unchanged interval never
    inserts a second row (BUILD.md: "re-running a period produces no duplicates")."""
    obligation_id, hub_id = await _insert_fixture(pg_pool, committed_kw=Decimal("4"))
    interval_start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=40)
    interval_end = interval_start + timedelta(minutes=15)
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("4"))

    settle_module.configure(PgSettleBackend(pg_pool), TraceStore(PgTraceBackend(pg_pool)), trace_pool=pg_pool)
    await settle(obligation_id, interval_start, interval_end)
    await settle(obligation_id, interval_start, interval_end)
    await settle(obligation_id, interval_start, interval_end)

    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT count(*) FROM og.meter_interval WHERE obligation_id = %s", (obligation_id,))
        (meter_count,) = await cur.fetchone()
        await cur.execute("SELECT count(*) FROM og.pnl WHERE obligation_id = %s", (obligation_id,))
        (pnl_count,) = await cur.fetchone()

    assert meter_count == 1
    assert pnl_count == 1


async def test_correction_supersedes_the_original_meter_interval(pg_pool):
    """A telemetry correction re-meters the interval. Discharge is NEGATIVE p_kw and is attributed by the
    obligation's og.grant share (`_FETCH_TELEMETRY_SQL`): the old fixture wrote positive (charging) kW and no
    grants, so both runs metered 0 kWh, nothing changed, and no correction row was ever written."""
    obligation_id, hub_id = await _insert_fixture(pg_pool, committed_kw=Decimal("4"))
    interval_start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=60)
    interval_end = interval_start + timedelta(minutes=15)
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("-4"))
    await _insert_grants(pg_pool, obligation_id, hub_id, interval_start, Decimal("4"))

    settle_module.configure(PgSettleBackend(pg_pool), TraceStore(PgTraceBackend(pg_pool)), trace_pool=pg_pool)
    await settle(obligation_id, interval_start, interval_end)

    # a correction: replace telemetry with a different value and re-settle
    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM og.telemetry WHERE hub_id = %s", (hub_id,))
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("-6"))
    await settle(obligation_id, interval_start, interval_end)

    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT version, superseded_by, delivered_kwh FROM og.meter_interval WHERE obligation_id = %s "
            "ORDER BY version",
            (obligation_id,),
        )
        rows = await cur.fetchall()

    assert [r[2] for r in rows] == [Decimal("1.000000"), Decimal("1.500000")]  # 4 kW then 6 kW x 0.25 h
    assert len(rows) == 2
    assert rows[0][1] is not None  # the original is now superseded
    assert rows[1][1] is None  # the correction is the active row


async def test_correction_supersedes_the_original_pnl_row(pg_pool):
    """`og.pnl` follows the same insert-only + version/superseded_by pattern as `meter_interval`
    (migration 0006): a corrected net_value adds a new versioned row and supersedes the original,
    never mutating it in place and never leaving two active rows."""
    obligation_id, hub_id = await _insert_fixture(pg_pool, committed_kw=Decimal("4"))
    # Inside the fixture's reservation (now - 1 h .. now + 1 h): 80 minutes ago lay before it, so the
    # obligation had no bank that interval and metered 0 kWh on both runs (no correction to write).
    interval_start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=45)
    interval_end = interval_start + timedelta(minutes=15)
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("-4"))  # discharge is negative p_kw
    await _insert_grants(pg_pool, obligation_id, hub_id, interval_start, Decimal("4"))

    settle_module.configure(PgSettleBackend(pg_pool), TraceStore(PgTraceBackend(pg_pool)), trace_pool=pg_pool)
    await settle(obligation_id, interval_start, interval_end)

    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM og.telemetry WHERE hub_id = %s", (hub_id,))
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("-6"))
    await settle(obligation_id, interval_start, interval_end)

    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT version, superseded_by FROM og.pnl WHERE obligation_id = %s ORDER BY version",
            (obligation_id,),
        )
        rows = await cur.fetchall()
        await cur.execute(
            "SELECT count(*) FROM og.pnl WHERE obligation_id = %s AND superseded_by IS NULL",
            (obligation_id,),
        )
        (active_count,) = await cur.fetchone()

    assert len(rows) == 2
    assert rows[0][1] is not None  # the original is now superseded
    assert rows[1][1] is None  # the correction is the active row
    assert active_count == 1  # never more than one active pnl row per obligation-interval


async def test_ercot_as_capacity_is_priced_at_the_cleared_mcpc_not_the_opportunity(pg_pool):
    """r3.4 spot-check: a 0.5 MW ECRS award held for 15 minutes settled 0.125 (the opportunity's 1.00 $/MW-h)
    instead of 0.035 (the cleared 0.28 $/MW-h DAM MCPC for ECRS in that hour). Capacity revenue must price at
    the market's cleared MCPC for the award's product and hour, like D-10's zone SPP for energy."""
    obligation_id, _hub_id = await _insert_as_fixture(
        pg_pool, committed_kw=Decimal("500"), mcpc_usd_per_mwh=Decimal("1.00")
    )
    product_rule_id = uuid4()
    interval_start = _quarter(20)
    hour_start = interval_start.replace(minute=0)
    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            """INSERT INTO og.product_rule
                (product_rule_id, contract_id, product_code, duration_minutes, variable_kind)
               SELECT %s, contract_id, 'ECRS', 60, 'CONTINUOUS' FROM og.obligation WHERE obligation_id = %s""",
            (product_rule_id, obligation_id),
        )
        await cur.execute(
            """UPDATE og.opportunity SET product_rule_id = %s
               WHERE opportunity_id = (SELECT opportunity_id FROM og.obligation WHERE obligation_id = %s)""",
            (product_rule_id, obligation_id),
        )
        await cur.execute(
            """INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality)
               VALUES ('ERCOT', 'np4-188-cd', 'ECRS', %s, 0.28, 'usd_per_mwh', 'GOOD')
               ON CONFLICT (source, product, series, ts) DO UPDATE SET value = EXCLUDED.value""",
            (hour_start,),
        )

    backend = PgSettleBackend(pg_pool)
    ctx = await backend.fetch_context(obligation_id, interval_start)
    assert ctx.price_per_kwh == Decimal("0.00028") and ctx.price_flag == "MCPC"

    settle_module.configure(backend, TraceStore(PgTraceBackend(pg_pool)), trace_pool=pg_pool)
    await settle(obligation_id, interval_start, interval_start + timedelta(minutes=15))
    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT revenue FROM og.pnl WHERE obligation_id = %s AND superseded_by IS NULL", (obligation_id,)
        )
        (revenue,) = await cur.fetchone()
    assert revenue == Decimal("0.035000")  # 500 kW x 0.25 h x 0.28 $/MW-h (held, nothing deployed)


async def test_ercot_as_without_an_mcpc_observation_falls_back_to_the_opportunity_flagged(pg_pool):
    obligation_id, _hub_id = await _insert_as_fixture(
        pg_pool, committed_kw=Decimal("500"), mcpc_usd_per_mwh=Decimal("1.00")
    )
    ctx = await PgSettleBackend(pg_pool).fetch_context(obligation_id, _quarter(20))
    assert ctx.price_per_kwh == Decimal("0.001") and ctx.price_flag == "OPPORTUNITY_PRICE"
