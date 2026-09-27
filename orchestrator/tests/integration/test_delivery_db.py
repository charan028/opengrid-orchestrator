"""D-38 delivery verification against real Postgres (migration 0050): the job's SQL reads measured
telemetry, signed command batches and the SCADA meter, and persists `og.delivery_record`; the operator
store reads it back. Server only (`OG_DB`); every row carries a `dvit` id and is removed afterwards."""

from __future__ import annotations

import contextlib
import os
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from opengrid.delivery import store
from opengrid.delivery.job import DeliveryJob, DeliverySettings
from opengrid.trace.store import TraceRecordRef

pytestmark = pytest.mark.skipif(not os.environ.get("OG_DB"), reason="requires the server environment")

CALL_S = 300
STEP_S = 10


class _Trace:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, dict[str, Any]]] = []

    async def append(
        self, stream_id: str, decision_type: str, event_class: str, payload: dict[str, Any], reason_codes=None
    ):  # type: ignore[no-untyped-def]
        self.rows.append((stream_id, event_class, payload))
        return TraceRecordRef(uuid4(), stream_id, len(self.rows), "h")


@pytest.fixture(scope="module")
def dsn(server_config, _migrated) -> str:  # type: ignore[no-untyped-def]
    from opengrid.platform.db import build_dsn

    return str(build_dsn(server_config))


@pytest.fixture
async def pool(dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(dsn, min_size=1, max_size=3, open=False)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


def _seed(
    conn: psycopg.Connection[Any],
    tag: str,
    *,
    delivered_kw: float,
    meter_kw: float,
    outcome: str,
    start: datetime,
) -> UUID:
    bank, hub = f"dvit-{tag}-bank", f"dvit-{tag}-hub"
    contract, opp, obligation, deployment = uuid4(), uuid4(), uuid4(), uuid4()
    end = start + timedelta(seconds=CALL_S)
    conn.execute("INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES (%s, 'LZ_AEN', 2000)", (bank,))
    conn.execute(
        "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw) VALUES (%s, %s, 'LZ_AEN', 4000, 800, 1000)",
        (hub, bank),
    )
    conn.execute(
        """INSERT INTO og.contract (contract_id, customer_id, service_type, variant, tier, profile_ref, start_at,
               penalty_alpha, penalty_beta, penalty_theta, degradation_cost)
           VALUES (%s, %s, 'ERCOT_AS', 'ECRS', 'T2', 'dvit@1', now() - interval '1 day', 0.02, 0.3, 0.1, 0.03)""",
        (contract, uuid4()),
    )
    conn.execute(
        """INSERT INTO og.opportunity (opportunity_id, contract_id, window_start, window_end, requested_kw, value_per_mwh)
           VALUES (%s, %s, %s, %s, 1000, 10)""",
        (opp, contract, start - timedelta(hours=1), end + timedelta(hours=1)),
    )
    conn.execute(
        """INSERT INTO og.obligation (obligation_id, opportunity_id, contract_id, service_type, tier, window_start,
               window_end, committed_qty_kw, state)
           VALUES (%s, %s, %s, 'ERCOT_AS', 'T2', %s, %s, 1000, 'DELIVERING')""",
        (obligation, opp, contract, start - timedelta(hours=1), end + timedelta(hours=1)),
    )
    conn.execute(
        """INSERT INTO og.as_deployment (deployment_id, obligation_id, start_at, end_at, source, reason)
           VALUES (%s, %s, %s, %s, 'OPERATOR', 'dvit')""",
        (deployment, obligation, start, end),
    )
    for i in range(-12, CALL_S // STEP_S):
        ts = start + timedelta(seconds=i * STEP_S + 1)
        during = i >= 0
        conn.execute(
            "INSERT INTO og.telemetry (hub_id, ts, soc_kwh, p_kw, seq, epoch, health) VALUES (%s, %s, 2000, %s, %s, 1, 'online')",
            (hub, ts, delivered_kw if during else 0.0, 1000 + i),
        )
        conn.execute(
            """INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality)
               VALUES ('scada', %s, 'REAL_POWER_KW', %s, %s, 'kW', 'GOOD')""",
            (bank, ts, meter_kw if during else 0.0),
        )
        if not during:
            continue
        batch, cycle = uuid4(), f"dvit-{tag}-{i}"
        conn.execute(
            """INSERT INTO og.grant (grant_id, cycle_id, obligation_id, bank_id, granted_kw, ledger_version, created_at)
               VALUES (%s, %s, %s, %s, 1000, 1, %s)""",
            (uuid4(), cycle, obligation, bank, ts),
        )
        conn.execute(
            """INSERT INTO og.command_batch (command_batch_id, cycle_id, ledger_version, submission_id, command_count,
                   merkle_root, created_at) VALUES (%s, %s, 1, %s, 1, 'x', %s)""",
            (batch, cycle, cycle, ts),
        )
        conn.execute(
            """INSERT INTO og.verdict (verdict_id, command_batch_id, outcome, vetoed_rule_ids, latency_ms, inputs_hash)
               VALUES (%s, %s, %s, %s, 5, 'x')""",
            (uuid4(), batch, outcome, ["G-04"] if outcome != "PASS" else None),
        )
        items = [{"hub_id": hub, "p_kw_setpoint": -1000.0, "obligation_id": str(obligation)}]
        conn.execute(
            """INSERT INTO og.trace (trace_id, decision_type, event_class, stream_id, seq, payload, hash, created_at)
               VALUES (%s, 'RT_ALLOCATION', 'RT_ALLOCATION', %s, %s, %s, %s, %s)""",
            (
                uuid4(),
                f"allocator-{bank}",
                i,
                Jsonb({"command_batch_id": str(batch), "items": items}),
                f"dvit{uuid4()}",
                ts,
            ),
        )
    return deployment


def _cleanup(conn: psycopg.Connection[Any]) -> None:
    for sql in (
        "DELETE FROM og.delivery_record WHERE bank_ids[1] LIKE 'dvit-%'",
        "DELETE FROM og.alert WHERE detail->>'call_id' IN (SELECT deployment_id::text FROM og.as_deployment WHERE reason = 'dvit')",
        "DELETE FROM og.trace WHERE stream_id LIKE 'allocator-dvit-%'",
        "DELETE FROM og.verdict WHERE command_batch_id IN (SELECT command_batch_id FROM og.command_batch WHERE cycle_id LIKE 'dvit-%')",
        "DELETE FROM og.command_batch WHERE cycle_id LIKE 'dvit-%'",
        "DELETE FROM og.grant WHERE cycle_id LIKE 'dvit-%'",
        "DELETE FROM og.feed_obs WHERE source = 'scada' AND product LIKE 'dvit-%'",
        "DELETE FROM og.telemetry WHERE hub_id LIKE 'dvit-%'",
        "DELETE FROM og.as_deployment WHERE reason = 'dvit'",
        "DELETE FROM og.obligation WHERE contract_id IN (SELECT contract_id FROM og.contract WHERE profile_ref = 'dvit@1')",
        "DELETE FROM og.opportunity WHERE contract_id IN (SELECT contract_id FROM og.contract WHERE profile_ref = 'dvit@1')",
        "DELETE FROM og.contract WHERE profile_ref = 'dvit@1'",
        "DELETE FROM og.hub WHERE hub_id LIKE 'dvit-%'",
        "DELETE FROM og.bank WHERE bank_id LIKE 'dvit-%'",
    ):
        with contextlib.suppress(psycopg.Error):
            conn.execute(sql)


@pytest.fixture
def seeded(dsn: str) -> Iterator[dict[str, UUID]]:
    start = datetime.now(UTC).replace(microsecond=0) - timedelta(seconds=CALL_S + 120)
    with psycopg.connect(dsn, autocommit=True) as conn:
        _cleanup(conn)
        ids = {
            "ok": _seed(conn, "ok", delivered_kw=-1000.0, meter_kw=-990.0, outcome="PASS", start=start),
            "mismatch": _seed(conn, "mm", delivered_kw=-1000.0, meter_kw=-400.0, outcome="PASS", start=start),
            "vetoed": _seed(conn, "veto", delivered_kw=0.0, meter_kw=0.0, outcome="VETOED", start=start),
        }
    try:
        yield ids
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            _cleanup(conn)


async def test_the_job_records_measured_delivery_meter_checks_and_vetoes(
    pool: AsyncConnectionPool, seeded: dict[str, UUID]
) -> None:
    settings = DeliverySettings(
        bucket_s=30.0,
        telemetry_lag_s=0.0,
        meter_bank_ids=tuple(f"dvit-{t}-bank" for t in ("ok", "mm", "veto")),
    )
    trace = _Trace()
    job = DeliveryJob(pool, trace, settings)  # type: ignore[arg-type]

    await job.run_once()

    ok = await store.fetch_record(pool, str(seeded["ok"]))
    assert ok is not None and ok.final and ok.result == "PASS", ok and ok.reasons
    assert ok.delivered_kw_last == pytest.approx(-1000.0) and ok.commanded_kw_last == pytest.approx(-1000.0)
    assert ok.discharged_kwh == pytest.approx(1000.0 * CALL_S / 3600.0, rel=0.05)
    assert ok.meter_status == "CORROBORATED" and ok.trace_id is not None

    mismatch = await store.fetch_record(pool, str(seeded["mismatch"]))
    assert mismatch is not None and mismatch.meter_status == "UNCORROBORATED"

    vetoed = await store.fetch_record(pool, str(seeded["vetoed"]))
    assert vetoed is not None and vetoed.result == "FAIL"
    assert {"VETOED", "NO_DELIVERY"} <= set(vetoed.reasons)

    listed = await store.list_records(
        pool, service_type="ERCOT_AS", call_ids=[str(v) for v in seeded.values()]
    )
    assert {r.call_id for r in listed} == {str(v) for v in seeded.values()} and all(
        not r.series for r in listed
    )
    events = {(stream, cls) for stream, cls, _ in trace.rows}
    assert ("delivery", "DELIVERY_VERIFICATION") in events and ("delivery", "DELIVERY_ALERT") in events
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT count(*) FROM og.alert WHERE rule = 'ALR-DELIVERY-METER-MISMATCH' AND detail->>'call_id' = %s",
            (str(seeded["mismatch"]),),
        )
        (count,) = await cur.fetchone()  # type: ignore[misc]
    assert count == 1

    await job.run_once()  # final records are not re-verified or re-alerted
    assert len([r for r in trace.rows if r[1] == "DELIVERY_VERIFICATION"]) == 3


async def test_restart_and_meter_queries_run_on_postgres(
    pool: AsyncConnectionPool, seeded: dict[str, UUID], dsn: str
) -> None:
    settings = DeliverySettings(
        bucket_s=30.0, telemetry_lag_s=0.0, meter_bank_ids=("dvit-ok-bank", "dvit-mm-bank")
    )
    await DeliveryJob(pool, _Trace(), settings).run_once()  # type: ignore[arg-type]
    since = datetime.now(UTC) - timedelta(hours=1)
    async with pool.connection() as conn:
        latest = await store.latest_meter_status(conn, ["dvit-ok-bank", "dvit-mm-bank"], since=since)
    assert latest["dvit-ok-bank"][0] == "CORROBORATED" and latest["dvit-mm-bank"][0] == "UNCORROBORATED"

    with psycopg.connect(dsn, autocommit=True) as conn:
        (obligation_id,) = conn.execute(
            "SELECT obligation_id FROM og.as_deployment WHERE deployment_id = %s", (seeded["vetoed"],)
        ).fetchone()  # type: ignore[misc]
        conn.execute("UPDATE og.obligation SET at_risk = true WHERE obligation_id = %s", (obligation_id,))
        conn.execute(
            """INSERT INTO og.trace (trace_id, decision_type, event_class, stream_id, seq, payload, hash)
               VALUES (%s, 'ALERT', 'AT_RISK', %s, 0, %s, %s)""",
            (
                uuid4(),
                f"obligation-{obligation_id}",
                Jsonb({"cause": "measured_delivery", "call_id": "dvit-call"}),
                f"dvit{uuid4()}",
            ),
        )
    try:
        async with pool.connection() as conn:
            flags = await store.delivery_at_risk_flags(conn, since=since)
        assert (obligation_id, "dvit-call") in flags
        cleared: list[Any] = []

        async def set_at_risk(oid: Any, at_risk: bool, **_kw: Any) -> None:
            cleared.append((oid, at_risk))

        await DeliveryJob(pool, _Trace(), settings, set_at_risk=set_at_risk).run_once()  # type: ignore[arg-type]
        assert (obligation_id, False) in cleared  # the call is not running short: the stale flag is cleared
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM og.trace WHERE stream_id = %s", (f"obligation-{obligation_id}",))


async def test_retention_keeps_the_summary_and_prunes_old_series(
    pool: AsyncConnectionPool, seeded: dict[str, UUID], dsn: str
) -> None:
    settings = DeliverySettings(bucket_s=30.0, telemetry_lag_s=0.0)
    await DeliveryJob(pool, _Trace(), settings).run_once()  # type: ignore[arg-type]
    call_id = str(seeded["ok"])
    with psycopg.connect(dsn, autocommit=True) as conn:
        policy = conn.execute(
            "SELECT mode, keep_days, protected FROM og.data_retention WHERE table_name = 'delivery_record'"
        ).fetchone()
        assert policy == ("NONE", None, False)  # kept like og.as_deployment
        conn.execute(
            "UPDATE og.delivery_record SET window_end = window_end - interval '90 days' WHERE call_id = %s",
            (call_id,),
        )
    now = datetime.now(UTC)
    async with pool.connection() as conn:
        pruned = await store.prune_series(conn, now=now, keep_days=60, batch=500)
        await conn.commit()
    assert pruned >= 1
    record = await store.fetch_record(pool, call_id)
    assert record is not None and record.series == [] and record.series_pruned_at is not None
    assert record.result == "PASS" and record.discharged_kwh > 0  # the summary stays
    recent = await store.fetch_record(pool, str(seeded["mismatch"]))
    assert recent is not None and recent.series and recent.series_pruned_at is None
