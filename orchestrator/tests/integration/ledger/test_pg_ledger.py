"""Integration tests for `opengrid.ledger.pg_backend` against real Postgres (`og_t_ledg` on the
server, BUILD.md S5). Proves a single writer and no double sale (K2) even with concurrent writers.

Run via `powershell -File tools\\remote.ps1 -Ws ledg -Cmd "cd orchestrator && bash tools/check.sh"`, or
directly with `OG_CONFIG`/`OG_DB` set and Postgres reachable. Skipped automatically when no database is
reachable (e.g. local Windows dev box, BUILD.md S5's "local: no DB").
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.ledger import ReservationError, ReservationLedger, encode_interval_key
from opengrid.ledger.pg_backend import PgLedgerBackend
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync

pytestmark = pytest.mark.asyncio

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)


def _dsn() -> str | None:
    try:
        cfg = load_config(_CONFIG_PATH)
        return build_dsn(cfg)
    except Exception:
        return None


def _db_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


_DSN = _dsn()
_SKIP_REASON = "Postgres not reachable locally; run via tools/remote.ps1 -Ws ledg (BUILD.md S5)"
requires_db = pytest.mark.skipif(_DSN is None or not _db_reachable(_DSN), reason=_SKIP_REASON)


class _FixedCapability:
    def __init__(self, kw: Decimal) -> None:
        self._kw = kw

    async def capability_kw(self, bank_id: str, interval_start: datetime) -> Decimal:
        _ = bank_id, interval_start
        return self._kw


async def _seed_obligation(dsn: str) -> tuple[str, str]:
    """Insert a minimal contract/opportunity/obligation chain so `og.reservation`'s FK is satisfiable."""
    contract_id, opportunity_id, obligation_id = str(uuid4()), str(uuid4()), str(uuid4())
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute(
            """
            INSERT INTO og.contract (contract_id, customer_id, service_type, tier, profile_ref, start_at)
            VALUES (%s, %s, 'ERCOT_ENERGY', 'T2', 'test-profile', now())
            """,
            (contract_id, str(uuid4())),
        )
        await conn.execute(
            """
            INSERT INTO og.opportunity
                (opportunity_id, contract_id, window_start, window_end, requested_kw, admitted_at)
            VALUES (%s, %s, now(), now() + interval '15 min', 10, now())
            """,
            (opportunity_id, contract_id),
        )
        await conn.execute(
            """
            INSERT INTO og.obligation
                (obligation_id, opportunity_id, contract_id, service_type, tier,
                 window_start, window_end, committed_qty_kw)
            VALUES (%s, %s, %s, 'ERCOT_ENERGY', 'T2', now(), now() + interval '15 min', 10)
            """,
            (obligation_id, opportunity_id, contract_id),
        )
        await conn.commit()
    return obligation_id, opportunity_id


async def _seed_bank(dsn: str) -> str:
    """`og.reservation.bank_id` FK-references `og.bank` (migration 0004), so tests need a real bank."""
    bank_id = f"bank-{uuid4()}"
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute(
            "INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES (%s, 'test-zone', 600)", (bank_id,)
        )
        await conn.commit()
    return bank_id


async def _seed_plan(dsn: str) -> str:
    """`og.commitment.plan_id` FK-references `og.plan`, so every reserve() needs a real plan row."""
    plan_id = str(uuid4())
    async with await psycopg.AsyncConnection.connect(dsn) as conn:
        await conn.execute(
            """
            INSERT INTO og.plan (plan_id, plan_mode, gate_kind, horizon_start, horizon_end, scenario_set,
                                 solver_status)
            VALUES (%s, 'L-ID', 'ADMISSION', now(), now() + interval '1 day', '[]', 'OPTIMAL')
            """,
            (plan_id,),
        )
        await conn.commit()
    return plan_id


@requires_db
async def test_ts_05_03_reserve_writes_commitments_and_orphans_are_released() -> None:
    """Regression (live 2026-09-26): reserve() must write `og.commitment` rows with the reservations,
    and `release_uncommitted()` must free reservations that have no commitment."""
    assert _DSN is not None
    migrate_sync(_DSN)
    bank_id = await _seed_bank(_DSN)
    t0 = datetime(2030, 1, 2, tzinfo=UTC)
    t1 = datetime(2030, 1, 2, 0, 15, tzinfo=UTC)
    pool = AsyncConnectionPool(_DSN, min_size=1, max_size=4, open=False)
    await pool.open(wait=True)
    try:
        ledger = ReservationLedger(PgLedgerBackend(pool), _FixedCapability(Decimal(100)))
        committed_id, _ = await _seed_obligation(_DSN)
        orphan_id, _ = await _seed_obligation(_DSN)
        plan_id = await _seed_plan(_DSN)
        await ledger.reserve(
            committed_id, {encode_interval_key(bank_id, t0, t1): Decimal(30)}, plan_id, variable_kind="BINARY"
        )
        async with pool.connection() as conn:
            await conn.execute(
                "INSERT INTO og.reservation (reservation_id, obligation_id, bank_id, kind, amount, "
                "interval_start, interval_end, ledger_version) VALUES (%s, %s, %s, 'POWER_KW', 20, %s, %s, 1)",
                (uuid4(), orphan_id, bank_id, t0, t1),
            )
            cur = await conn.execute(
                "SELECT committed_kw, variable_kind FROM og.commitment WHERE obligation_id = %s",
                (committed_id,),
            )
            assert await cur.fetchall() == [(Decimal("30.000"), "BINARY")]

        assert await ledger.release_uncommitted() >= 1

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT obligation_id::text FROM og.reservation WHERE bank_id = %s AND released_at IS NULL",
                (bank_id,),
            )
            assert [r[0] for r in await cur.fetchall()] == [committed_id]
    finally:
        await pool.close()


@requires_db
async def test_single_writer_no_double_sale_under_concurrency() -> None:
    """TS-05-02/ES05-S05: two concurrent writers reserving the same bank/interval beyond capability --
    only enough succeed to stay within capability; nothing is ever double-sold."""
    assert _DSN is not None
    migrate_sync(_DSN)
    bank_id = await _seed_bank(_DSN)
    interval_start = datetime(2030, 1, 1, tzinfo=UTC)
    interval_end = datetime(2030, 1, 1, 0, 15, tzinfo=UTC)
    capability_kw = Decimal(50)

    pool = AsyncConnectionPool(_DSN, min_size=2, max_size=8, open=False)
    await pool.open(wait=True)
    try:
        backend = PgLedgerBackend(pool)
        ledger_a = ReservationLedger(backend, _FixedCapability(capability_kw))
        ledger_b = ReservationLedger(backend, _FixedCapability(capability_kw))

        obligation_ids = []
        results = []
        for _ in range(4):  # 4 * 20kW = 80kW requested against 50kW capability
            obligation_id, _opportunity_id = await _seed_obligation(_DSN)
            obligation_ids.append(obligation_id)
        plan_id = await _seed_plan(_DSN)

        async def _attempt(ledger: ReservationLedger, obligation_id: str) -> bool:
            key = encode_interval_key(bank_id, interval_start, interval_end)
            try:
                await ledger.reserve(obligation_id, {key: Decimal(20)}, plan_id)
            except ReservationError:
                return False
            return True

        results = await asyncio.gather(
            _attempt(ledger_a, obligation_ids[0]),
            _attempt(ledger_b, obligation_ids[1]),
            _attempt(ledger_a, obligation_ids[2]),
            _attempt(ledger_b, obligation_ids[3]),
        )

        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT COALESCE(SUM(amount), 0) FROM og.reservation "
                "WHERE bank_id = %s AND interval_start = %s AND released_at IS NULL",
                (bank_id, interval_start),
            )
            row = await cur.fetchone()
            total_reserved = Decimal(str(row[0]))

        assert total_reserved <= capability_kw
        assert total_reserved == Decimal(20) * sum(1 for ok in results if ok)
    finally:
        await pool.close()
