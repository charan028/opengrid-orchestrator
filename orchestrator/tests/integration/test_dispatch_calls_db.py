"""D-33 dispatch calls against real Postgres (migration 0047, `opengrid.calls.PgCallStore`): a utility
toll call is accepted and deployed (og.as_deployment source UTILITY, signed requested kW, call ledger row,
operator_action audit, alert), the refusals persist, an overlapping call is refused under the per-obligation
lock, idempotent replay, utility scoping, cancel/shorten, and the engine's called-kW read. Disposable test
cluster only (5433; OG_DB/OG_DB_PORT set by the runner); every row it writes carries a `d33it` marker and is
removed afterwards."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.skipif(
    not os.environ.get("OG_DB"), reason="requires the server environment (the 5433 test cluster)"
)

PRINCIPAL = "d33it-og-util-aen"
AEN = "AUSTIN_ENERGY"
CPS = "CPS_ENERGY"
_UTILITY_ROW = (
    "INSERT INTO og.utility (utility_id, name, territory_zones, capacity_product, payment_basis, "
    "capacity_price_usd_per_kw, charging_tariff_kind, off_peak_rate_usd_per_kwh, tariff_ref) "
    "VALUES (%s, %s, %s, 'UTILITY_TOLLING', 'USD_PER_KW_YEAR', 102, 'TOU_OFF_PEAK', 0.03, 'd33it') "
    "ON CONFLICT (utility_id) DO NOTHING"
)


@pytest.fixture(scope="module")
def dsn(server_config, _migrated) -> str:  # type: ignore[no-untyped-def]
    from opengrid.platform.db import build_dsn

    return str(build_dsn(server_config))


def _toll(conn: psycopg.Connection, utility_id: str, start: datetime, end: datetime) -> UUID:
    contract, opportunity, obligation, rule = uuid4(), uuid4(), uuid4(), uuid4()
    conn.execute(
        "INSERT INTO og.contract (contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, "
        "market, utility_id) VALUES (%s, %s, 'REGULATED_CAPACITY', 'TOLLING', 'T1', 'd33it', %s, 'REGULATED', %s)",
        (contract, uuid4(), start - timedelta(days=1), utility_id),
    )
    conn.execute(
        "INSERT INTO og.product_rule (product_rule_id, contract_id, product_code, min_qty_kw, duration_minutes, "
        "variable_kind) VALUES (%s, %s, 'TOLL90', 26000, 90, 'CONTINUOUS')",
        (rule, contract),
    )
    conn.execute(
        "INSERT INTO og.opportunity (opportunity_id, contract_id, product_rule_id, window_start, window_end, "
        "requested_kw, state) VALUES (%s, %s, %s, %s, %s, 26000, 'SELECTED')",
        (opportunity, contract, rule, start, end),
    )
    conn.execute(
        "INSERT INTO og.obligation (obligation_id, opportunity_id, contract_id, service_type, tier, window_start, "
        "window_end, committed_qty_kw, state) VALUES (%s, %s, %s, 'REGULATED_CAPACITY', 'T1', %s, %s, 26000, "
        "'COMMITTED')",
        (obligation, opportunity, contract, start, end),
    )
    return obligation


@pytest.fixture
def tolls(dsn: str) -> Iterator[dict[str, UUID]]:
    now = datetime.now(UTC)
    start, end = now - timedelta(minutes=5), now + timedelta(minutes=85)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(_UTILITY_ROW, (AEN, "Austin Energy", ["LZ_AEN"]))
        conn.execute(_UTILITY_ROW, (CPS, "CPS Energy", ["LZ_CPS"]))
        ids = {AEN: _toll(conn, AEN, start, end), CPS: _toll(conn, CPS, start, end)}
    try:
        yield ids
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            oblig = list(ids.values())
            conn.execute(
                "DELETE FROM og.dispatch_call WHERE obligation_id = ANY(%s) OR principal LIKE 'd33it%%'",
                (oblig,),
            )
            conn.execute("DELETE FROM og.as_deployment WHERE obligation_id = ANY(%s)", (oblig,))
            conn.execute("DELETE FROM og.operator_action WHERE operator_ref LIKE 'd33it%%'")
            conn.execute(
                "DELETE FROM og.alert WHERE detail->>'principal' LIKE 'd33it%%' "
                "OR scope_ref LIKE 'd33it%%' OR detail->>'obligation_id' = ANY(%s)",
                ([str(o) for o in oblig],),
            )
            rows = conn.execute(
                "SELECT contract_id FROM og.obligation WHERE obligation_id = ANY(%s)", (oblig,)
            )
            contracts = [r[0] for r in rows.fetchall()]
            conn.execute("DELETE FROM og.obligation WHERE obligation_id = ANY(%s)", (oblig,))
            conn.execute("DELETE FROM og.opportunity WHERE contract_id = ANY(%s)", (contracts,))
            conn.execute("DELETE FROM og.product_rule WHERE contract_id = ANY(%s)", (contracts,))
            conn.execute("DELETE FROM og.contract WHERE contract_id = ANY(%s)", (contracts,))
            conn.execute("DELETE FROM og.utility WHERE tariff_ref = 'd33it'")


@pytest.fixture
async def pool(dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(dsn, min_size=1, max_size=3, open=False)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


def _trace(pool: AsyncConnectionPool):  # type: ignore[no-untyped-def]
    """The real Postgres trace chain: og.operator_action.trace_id is a foreign key into og.trace."""
    from opengrid.api.trace_backend import PgTraceBackend
    from opengrid.trace.store import TraceStore

    return TraceStore(PgTraceBackend(pool))


def _request(**kw):  # type: ignore[no-untyped-def]
    from opengrid.calls import CallOrigin, CallRequest

    base = {
        "origin": CallOrigin.UTILITY,
        "principal": PRINCIPAL,
        "reason": "d33it peak",
        "duration_minutes": 30,
        "utility_id": AEN,
        "requested_kw": -20_000.0,
        "idempotency_key": f"d33it-{uuid4()}",
    }
    return CallRequest(**{**base, **kw})


async def test_ts_d33_it_01_accept_persist_audit_and_engine_read(pool, tolls, dsn) -> None:  # type: ignore[no-untyped-def]
    from opengrid.calls import CallLimits, PgCallStore, issue_call

    store, trace = PgCallStore(pool), _trace(pool)
    record = await issue_call(store, trace, _request(), limits=CallLimits())
    assert record.obligation_id == tolls[AEN]  # resolved from the utility's window
    with psycopg.connect(dsn) as conn:
        dep = conn.execute(
            "SELECT source, requested_kw, call_id, requested_by FROM og.as_deployment WHERE deployment_id = %s",
            (record.deployment_id,),
        ).fetchone()
        assert dep is not None and dep[0] == "UTILITY" and float(dep[1]) == -20_000.0
        assert dep[2] == record.call_id and dep[3] == PRINCIPAL
        ledger = conn.execute(
            "SELECT outcome, origin FROM og.dispatch_call WHERE call_id = %s", (record.call_id,)
        )
        assert ledger.fetchone() == ("ACCEPTED", "UTILITY")
        action = conn.execute(
            "SELECT target_ref FROM og.operator_action WHERE operator_ref = %s", (PRINCIPAL,)
        ).fetchone()
        assert action == (f"UTILITY_CALL:{tolls[AEN]}",)
        alert = conn.execute(
            "SELECT 1 FROM og.alert WHERE rule = 'ALR-UTILITY-CALL' AND scope_ref = %s",
            (str(record.call_id),),
        ).fetchone()
        assert alert is not None
        # The engine caps the called obligation at the requested kW (gateways._ACTIVE_CALLS_SQL deploy_kw).
        deploy_kw = conn.execute(
            "SELECT MIN(-d.requested_kw) FROM og.as_deployment d WHERE d.obligation_id = %s "
            "AND d.cancelled_at IS NULL AND d.start_at <= now() AND d.end_at > now()",
            (tolls[AEN],),
        ).fetchone()
        assert deploy_kw is not None and float(deploy_kw[0]) == 20_000.0


async def test_ts_d33_it_02_overlap_idempotency_scope_and_refusals_persist(pool, tolls) -> None:  # type: ignore[no-untyped-def]
    from opengrid.calls import CallLimits, CallRefused, PgCallStore, issue_call, list_calls

    store, trace, limits = PgCallStore(pool), _trace(pool), CallLimits()
    first = await issue_call(store, trace, _request(idempotency_key="d33it-k1"), limits=limits)
    replay = await issue_call(store, trace, _request(idempotency_key="d33it-k1"), limits=limits)
    assert replay.replayed and replay.call_id == first.call_id
    for bad, code in (
        (_request(), "R-CALL-OVERLAP"),
        (_request(requested_kw=500.0), "R-CALL-CHARGE-REFUSED"),
        (_request(duration_minutes=91), "R-CALL-DURATION-CAP"),
        (_request(obligation_id=tolls[CPS]), "R-CALL-NOT-FOUND"),
    ):
        with pytest.raises(CallRefused) as info:
            await issue_call(store, trace, bad, limits=limits)
        assert info.value.reason_code == code
    history = await list_calls(store, utility_id=AEN, principal=PRINCIPAL)
    assert sorted(c.outcome.value for c in history) == ["ACCEPTED"] + ["REFUSED"] * 4
    async with pool.connection() as conn, conn.cursor() as cur:  # the CPS obligation reach, audited
        await cur.execute(
            "SELECT count(*) FROM og.trace WHERE stream_id = %s AND event_class = %s",
            (f"authz_deny:{PRINCIPAL}", "TRACE_AUTHZ_DENY"),
        )
        row = await cur.fetchone()
    assert row is not None and row[0] >= 1


async def test_ts_d33_it_03_cancel_and_shorten(pool, tolls) -> None:  # type: ignore[no-untyped-def]
    from opengrid.calls import (
        CallLimits,
        CallOrigin,
        CallState,
        PgCallStore,
        call_status,
        cancel_call,
        issue_call,
    )

    store, trace = PgCallStore(pool), _trace(pool)
    record = await issue_call(store, trace, _request(), limits=CallLimits())
    shorter = record.end_at - timedelta(minutes=10)
    kw = {"origin": CallOrigin.UTILITY, "principal": PRINCIPAL, "utility_id": AEN}
    shortened = await cancel_call(store, trace, record.call_id, end_at=shorter, **kw)
    assert shortened.end_at == shorter and shortened.cancelled_at is None
    status = await call_status(store, trace, record.call_id, principal=PRINCIPAL, utility_id=AEN)
    assert status.state is CallState.ACTIVE and status.public()["delivery_measured"] is False
    ended = await cancel_call(store, trace, record.call_id, **kw)
    assert ended.cancelled_at is not None
    status = await call_status(store, trace, record.call_id, principal=PRINCIPAL, utility_id=AEN)
    assert status.state is CallState.COMPLETED
