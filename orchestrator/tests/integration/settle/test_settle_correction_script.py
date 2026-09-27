"""deploy/scripts/r343_settle_correct_as.py against real Postgres: an ERCOT_AS interval settled at the
opportunity price (the pre-c641a0f bug) is re-priced at the cleared MCPC by an insert-only superseding
og.pnl row; the dry run writes nothing; a second apply changes nothing."""

from __future__ import annotations

import importlib.util
import os
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest

import opengrid.settle as settle_module
from opengrid.settle import settle
from opengrid.settle.pg_backend import PgSettleBackend
from opengrid.trace.pg_backend import PgTraceBackend
from opengrid.trace.store import TraceStore

from .test_settle_pg import _insert_as_fixture, _quarter, pg_pool  # noqa: F401 -- pg_pool is a fixture

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("OG_DB"), reason="requires the server environment (tools/remote.ps1)"
    ),
]

_SCRIPT = Path(__file__).resolve().parents[4] / "deploy" / "scripts" / "r343_settle_correct_as.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("r343_settle_correct_as", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


async def _live_revenues(pool, obligation_id):
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT revenue, superseded_by IS NULL FROM og.pnl WHERE obligation_id = %s ORDER BY version",
            (obligation_id,),
        )
        return await cur.fetchall()


async def test_the_correction_reprices_at_the_mcpc_insert_only(pg_pool, tmp_path) -> None:  # noqa: F811
    script = _load_script()
    obligation_id, _hub = await _insert_as_fixture(
        pg_pool, committed_kw=Decimal("500"), mcpc_usd_per_mwh=Decimal("1.00")
    )
    interval_start = _quarter(20)
    backend = PgSettleBackend(pg_pool)
    settle_module.configure(
        backend, TraceStore(PgTraceBackend(pg_pool, journal_path=tmp_path / "j.jsonl")), trace_pool=pg_pool
    )
    # Settled before the MCPC existed (the pre-fix outcome): 500 kW x 0.25 h x 1.00 $/MW-h.
    await settle(obligation_id, interval_start, interval_start + timedelta(minutes=15))
    assert await _live_revenues(pg_pool, obligation_id) == [(Decimal("0.125000"), True)]

    product_rule_id = uuid4()
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
            (interval_start.replace(minute=0),),
        )
    window = {"start": interval_start, "end": interval_start + timedelta(minutes=15)}

    dry = await script.correct(pg_pool, backend, **window, apply=False)
    mine = [p for p in dry["changed"] if p["obligation_id"] == obligation_id]
    assert [(p["old_revenue"], p["new_revenue"], p["price_flag"]) for p in mine] == [
        (Decimal("0.125000"), Decimal("0.035000"), "MCPC")
    ]
    assert await _live_revenues(pg_pool, obligation_id) == [(Decimal("0.125000"), True)]  # dry run: untouched

    await script.correct(pg_pool, backend, **window, apply=True)
    assert await _live_revenues(pg_pool, obligation_id) == [
        (Decimal("0.125000"), False),  # the original stays, now superseded
        (Decimal("0.035000"), True),
    ]
    again = await script.correct(pg_pool, backend, **window, apply=True)
    assert [p for p in again["changed"] if p["obligation_id"] == obligation_id] == []
