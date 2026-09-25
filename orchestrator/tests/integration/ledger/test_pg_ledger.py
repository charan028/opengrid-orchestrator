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


@requires_db
async def test_single_writer_no_double_sale_under_concurrency() -> None:
    """TS-05-02/ES05-S05: two concurrent writers reserving the same bank/interval beyond capability --
    only enough succeed to stay within capability; nothing is ever double-sold."""
    assert _DSN is not None
    migrate_sync(_DSN)
    bank_id = f"bank-{uuid4()}"
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

        async def _attempt(ledger: ReservationLedger, obligation_id: str) -> bool:
            key = encode_interval_key(bank_id, interval_start, interval_end)
            try:
                await ledger.reserve(obligation_id, {key: Decimal(20)}, uuid4())
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
