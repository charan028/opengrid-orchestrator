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
    bank_id = "bank-settle-it"
    hub_id = "hub-settle-it"

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
    obligation_id, hub_id = await _insert_fixture(pg_pool, committed_kw=Decimal("4"))
    interval_start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=60)
    interval_end = interval_start + timedelta(minutes=15)
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("4"))

    settle_module.configure(PgSettleBackend(pg_pool), TraceStore(PgTraceBackend(pg_pool)), trace_pool=pg_pool)
    await settle(obligation_id, interval_start, interval_end)

    # a correction: replace telemetry with a different value and re-settle
    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM og.telemetry WHERE hub_id = %s", (hub_id,))
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("6"))
    await settle(obligation_id, interval_start, interval_end)

    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT version, superseded_by FROM og.meter_interval WHERE obligation_id = %s ORDER BY version",
            (obligation_id,),
        )
        rows = await cur.fetchall()

    assert len(rows) == 2
    assert rows[0][1] is not None  # the original is now superseded
    assert rows[1][1] is None  # the correction is the active row


async def test_correction_supersedes_the_original_pnl_row(pg_pool):
    """`og.pnl` follows the same insert-only + version/superseded_by pattern as `meter_interval`
    (migration 0006): a corrected net_value adds a new versioned row and supersedes the original,
    never mutating it in place and never leaving two active rows."""
    obligation_id, hub_id = await _insert_fixture(pg_pool, committed_kw=Decimal("4"))
    interval_start = datetime.now(UTC).replace(second=0, microsecond=0) - timedelta(minutes=80)
    interval_end = interval_start + timedelta(minutes=15)
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("4"))

    settle_module.configure(PgSettleBackend(pg_pool), TraceStore(PgTraceBackend(pg_pool)), trace_pool=pg_pool)
    await settle(obligation_id, interval_start, interval_end)

    async with pg_pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("DELETE FROM og.telemetry WHERE hub_id = %s", (hub_id,))
    await _insert_telemetry(pg_pool, hub_id, interval_start, Decimal("6"))
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
