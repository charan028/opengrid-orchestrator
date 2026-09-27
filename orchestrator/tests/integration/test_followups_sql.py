"""FOLLOWUPS round (2026-09-26) SQL paths against real Postgres: K2's `og.hub.units` read
(`opengrid.invariants.queries.fetch_bank_capability_inputs`, migration 0032), the LP value read
(`opengrid.api.routers.lp_value.PgLpValueReader`, migration 0030) and the market-sim AS deployment
writes (`opengrid.contracts.as_deployment_poll.PgAsDeploymentRepo`, migration 0020). Server only
(`tools/remote.ps1`); every row it writes carries a `fups` id and is removed afterwards."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.skipif(
    not os.environ.get("OG_DB"), reason="requires the server environment (tools/remote.ps1)"
)


@pytest.fixture(scope="module")
def dsn(server_config, _migrated) -> str:  # type: ignore[no-untyped-def]
    from opengrid.platform.db import build_dsn

    return str(build_dsn(server_config))


@pytest.fixture
async def pool(dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


async def test_bank_capability_inputs_carry_hub_units(dsn: str, pool: AsyncConnectionPool) -> None:
    from opengrid.invariants import checks, queries

    now = datetime.now(UTC)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES ('fups-bank', 'LZ_NORTH', 600)")
        # No `units` given: 0032's insert trigger derives 2 for the 78.4 kWh home, 1 for the 39.2 kWh one.
        conn.execute(
            "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw) VALUES "
            "('fups-hub-1', 'fups-bank', 'LZ_NORTH', 78.4, 15.68, 24.0), "
            "('fups-hub-2', 'fups-bank', 'LZ_NORTH', 39.2, 7.84, 20.0)"
        )
        conn.execute(
            "INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, health, last_seen_at) VALUES "
            "('fups-hub-1', 78.4, 0, 'online', %(now)s), ('fups-hub-2', 39.2, 0, 'online', %(now)s)",
            {"now": now},
        )
    try:
        rows = [r for r in await queries.fetch_bank_capability_inputs(pool) if r[0] == "fups-bank"]
        assert sorted(r[10] for r in rows) == [1, 2]
        # Dual-unit home capped at 20 kW (not its 24 kW p_kw); single-unit at 11 kW (not its 20 kW p_kw).
        assert checks.compute_bank_rated_capabilities_kw(rows)["fups-bank"] == pytest.approx(31.0)
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM og.hub_state WHERE hub_id LIKE 'fups-%'")
            conn.execute("DELETE FROM og.hub WHERE hub_id LIKE 'fups-%'")
            conn.execute("DELETE FROM og.bank WHERE bank_id = 'fups-bank'")


async def test_lp_value_reader_returns_the_newest_plans(dsn: str, pool: AsyncConnectionPool) -> None:
    from opengrid.api.routers.lp_value import LpValuePlan, PgLpValueReader

    plan_id = uuid4()
    now = datetime.now(UTC) + timedelta(days=365)  # newer than anything else in the workspace DB
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO og.plan (plan_id, plan_mode, gate_kind, horizon_start, horizon_end, scenario_set, "
            "solver_status, created_at) VALUES (%s, 'L-ID', 'SCHEDULED_15MIN', %s, %s, '{}', 'OPTIMAL', %s)",
            (plan_id, now, now + timedelta(hours=1), now),
        )
        conn.execute(
            "INSERT INTO og.plan_value (plan_id, lp_net_value, rule_net_value, value_added, forgone_upside, "
            "breakdown) VALUES (%s, 120.5, 100.25, 20.25, 3, %s)",
            (plan_id, Jsonb({"energy": 20.25, "fups": True})),
        )
    try:
        rows = await PgLpValueReader(pool).latest_plan_values(limit=96)
        assert rows is not None and 1 <= len(rows) <= 96
        newest = LpValuePlan.model_validate(rows[0])
        assert newest.plan_id == plan_id
        assert newest.value_added == Decimal("20.25")
        assert newest.breakdown == {"energy": 20.25, "fups": True}
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM og.plan_value WHERE plan_id = %s", (plan_id,))
            conn.execute("DELETE FROM og.plan WHERE plan_id = %s", (plan_id,))


async def test_market_sim_deployment_open_extend_close(dsn: str, pool: AsyncConnectionPool) -> None:
    from opengrid.contracts.as_deployment_poll import (
        AsDeploymentPoller,
        PgAsDeploymentRepo,
        PollConfig,
        SimDeployment,
    )

    repo = PgAsDeploymentRepo(pool)
    poller = AsDeploymentPoller(
        http_client=None,  # type: ignore[arg-type]  -- apply() is driven directly, no HTTP
        repo=repo,
        config=PollConfig(enabled=True, base_url="http://unused", interval_s=10.0, lease_s=60.0),
    )
    now = datetime.now(UTC)
    sim = SimDeployment(sim_id="fups-1", service="RRS", deployed_mw=50.0, recall=False, declared_at=now)
    try:
        await poller.apply(sim, now=now)
        await poller.apply(sim, now=now + timedelta(seconds=10))
        with psycopg.connect(dsn) as conn:
            row = conn.execute(
                "SELECT source, obligation_id, end_at, cancelled_at FROM og.as_deployment "
                "WHERE requested_by = 'market_sim:fups-1'"
            ).fetchall()
        assert len(row) == 1
        assert row[0][0] == "MARKET_SIM" and row[0][1] is None and row[0][3] is None
        assert row[0][2] == now + timedelta(seconds=70)  # lease renewed from the second poll

        recalled = SimDeployment(
            sim_id="fups-1", service="RRS", deployed_mw=50.0, recall=True, declared_at=now
        )
        await poller.apply(recalled, now=now + timedelta(seconds=20))
        with psycopg.connect(dsn) as conn:
            cancelled = conn.execute(
                "SELECT cancelled_at FROM og.as_deployment WHERE requested_by = 'market_sim:fups-1'"
            ).fetchone()
        assert cancelled is not None and cancelled[0] == now + timedelta(seconds=20)
        assert [
            r for r in await repo.list_open(now=now + timedelta(seconds=21)) if "fups" in r.requested_by
        ] == []
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM og.as_deployment WHERE requested_by LIKE 'market_sim:fups-%'")


async def test_as_hold_inputs_query_runs_against_the_migrated_schema(pool: AsyncConnectionPool) -> None:
    """SQL shape of the reworked AS-hold read (every committed ERCOT_AS award's banks, held or deployed):
    it must execute against the real schema; the pure accounting is unit-tested."""
    from opengrid.invariants import queries

    reservations, hubs_by_bank = await queries.fetch_as_hold_inputs(pool, now=datetime.now(UTC))
    assert isinstance(reservations, list)
    assert set(hubs_by_bank) <= {r.bank_id for r in reservations}


def test_hub_asset_dates_columns_and_service_triggers(dsn: str) -> None:
    """Migration 0036: `installed_at`/`last_serviced_at` exist, and a completed calibration, a closed work
    order and an inverter replacement each move `last_serviced_at` forward -- never back."""
    t1 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    t2 = t1 + timedelta(days=10)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES ('fups-bank2', 'LZ_NORTH', 600)")
        conn.execute(
            "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw, installed_at) VALUES "
            "('fups-hub-9', 'fups-bank2', 'LZ_NORTH', 39.2, 7.84, 11.0, DATE '2025-03-14')"
        )
        try:

            def serviced() -> datetime | None:
                row = conn.execute(
                    "SELECT last_serviced_at FROM og.hub WHERE hub_id = 'fups-hub-9'"
                ).fetchone()
                return row[0] if row else None

            assert serviced() is None
            conn.execute(
                "INSERT INTO og.maintenance_work_order (hub_id, severity, evidence, status, closed_at) "
                "VALUES ('fups-hub-9', 'LOW', '{}', 'CLOSED', %s)",
                (t2,),
            )
            assert serviced() == t2
            cal = conn.execute(
                "INSERT INTO og.calibration_attempt (hub_id, reference_phase_deg, reference_freq_hz, "
                "reference_amplitude_v) VALUES ('fups-hub-9', 0, 60, 240) RETURNING calibration_id"
            ).fetchone()
            assert cal is not None
            conn.execute(
                "UPDATE og.calibration_attempt SET outcome = 'CORRECTED', verified_at = %s "
                "WHERE calibration_id = %s",
                (t1, cal[0]),
            )
            assert serviced() == t2  # an older completion never moves the date back
            conn.execute(
                "INSERT INTO og.asset_event (hub_id, event_type, occurred_at) VALUES "
                "('fups-hub-9', 'INVERTER_REPLACED', %s)",
                (t2 + timedelta(days=1),),
            )
            assert serviced() == t2 + timedelta(days=1)
            installed = conn.execute("SELECT installed_at FROM og.hub WHERE hub_id = 'fups-hub-9'").fetchone()
            assert installed is not None and str(installed[0]) == "2025-03-14"
        finally:
            conn.execute("DELETE FROM og.asset_event WHERE hub_id = 'fups-hub-9'")
            conn.execute("DELETE FROM og.calibration_attempt WHERE hub_id = 'fups-hub-9'")
            conn.execute("DELETE FROM og.maintenance_work_order WHERE hub_id = 'fups-hub-9'")
            conn.execute("DELETE FROM og.hub WHERE hub_id = 'fups-hub-9'")
            conn.execute("DELETE FROM og.bank WHERE bank_id = 'fups-bank2'")


async def test_manual_target_rows_feed_the_shared_parser(dsn: str, pool: AsyncConnectionPool) -> None:
    """The cancel route's read: a hub belongs to a MANUAL_TARGET only while that event is its newest,
    unexpired target (engine/manual.py's rule)."""
    from opengrid.api.store import PgStore

    now = datetime.now(UTC)
    first, newer = uuid4(), uuid4()
    stream = f"fups-manual-{uuid4()}"

    def row(trace_id, seq, hubs, issued, expires):  # type: ignore[no-untyped-def]
        return (
            trace_id,
            stream,
            seq,
            Jsonb(
                {
                    "hub_ids": hubs,
                    "p_kw_command": -5.0,
                    "sign_convention": "+charge/-discharge",
                    "issued_at": issued.isoformat(),
                    "expires_at": expires.isoformat(),
                }
            ),
            None if seq == 0 else f"h{seq - 1}",
            f"h{seq}",
        )

    with psycopg.connect(dsn, autocommit=True) as conn:
        for r in (
            row(first, 0, ["fups-a", "fups-b"], now - timedelta(minutes=2), now + timedelta(minutes=10)),
            row(newer, 1, ["fups-b"], now - timedelta(minutes=1), now + timedelta(minutes=10)),
        ):
            conn.execute(
                "INSERT INTO og.trace (trace_id, decision_type, event_class, stream_id, seq, payload, prev_hash, hash) "
                "VALUES (%s, 'OPERATOR_ACTION', 'MANUAL_TARGET', %s, %s, %s, %s, %s)",
                r,
            )
    try:
        from opengrid.core.manual_targets import parse_targets

        rows = [r for r in await PgStore(pool).manual_target_rows() if str(r[0]) in (str(first), str(newer))]
        live = parse_targets(rows, datetime.now(UTC))
        assert {h: (t.trace_id, t.p_kw_target) for h, t in live.items()} == {
            "fups-a": (str(first), -5.0),
            "fups-b": (str(newer), -5.0),  # taken over by the newer target
        }
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM og.trace WHERE stream_id = %s", (stream,))


async def test_charge_windows_table_store_and_topology(dsn: str, pool: AsyncConnectionPool) -> None:
    """Migration 0038 + the API store: FLEET seed, format CHECK, upsert/delete, and a hub's topology."""
    from opengrid.api.store import PgStore

    store = PgStore(pool)
    rows = {(r["scope_kind"], r["scope_ref"]): list(r["windows"]) for r in await store.list_charge_windows()}
    assert rows[("FLEET", "*")] == ["22:00-06:00"]
    with psycopg.connect(dsn, autocommit=True) as conn:
        for bad in (["25:00-01:00"], ["10:00-10:00"], ["1:00-2:00"], ["00:00-01:00"] * 5):
            with pytest.raises(psycopg.errors.CheckViolation):
                conn.execute(
                    "INSERT INTO og.owner_charge_window (scope_kind, scope_ref, windows, updated_by) "
                    "VALUES ('ZONE', 'fups-zone', %s, 't')",
                    (bad,),
                )
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO og.owner_charge_window (scope_kind, scope_ref, windows, updated_by) "
                "VALUES ('FLEET', 'x', ARRAY['01:00-02:00'], 't')"
            )
        conn.execute(
            "INSERT INTO og.bank (bank_id, zone, kva_rating, feeder_id) "
            "VALUES ('fups-bank3', 'LZ_NORTH', 600, 'fups-fdr')"
        )
        conn.execute(
            "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw) VALUES "
            "('fups-hub-3', 'fups-bank3', 'LZ_NORTH', 39.2, 7.84, 11.0)"
        )
    try:
        await store.set_charge_window("ZONE", "fups-zone", ["21:30-05:30"], updated_by="fups")
        await store.set_charge_window("ZONE", "fups-zone", [], updated_by="fups")
        rows = {
            (r["scope_kind"], r["scope_ref"]): list(r["windows"]) for r in await store.list_charge_windows()
        }
        assert rows[("ZONE", "fups-zone")] == []
        assert await store.delete_charge_window("ZONE", "fups-zone") is True
        assert await store.delete_charge_window("ZONE", "fups-zone") is False
        topo = await store.charge_window_topology(hub_id="fups-hub-3", bank_id=None)
        assert topo is not None
        assert (topo["hub_id"], topo["bank_id"], topo["zone"], topo["feeder_id"]) == (
            "fups-hub-3",
            "fups-bank3",
            "LZ_NORTH",
            "fups-fdr",
        )
        bank = await store.charge_window_topology(hub_id=None, bank_id="fups-bank3")
        assert bank is not None and bank["hub_id"] is None and bank["bank_id"] == "fups-bank3"
        assert await store.charge_window_topology(hub_id="nope", bank_id=None) is None
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM og.owner_charge_window WHERE scope_ref LIKE 'fups-%'")
            conn.execute("DELETE FROM og.hub WHERE hub_id = 'fups-hub-3'")
            conn.execute("DELETE FROM og.bank WHERE bank_id = 'fups-bank3'")


async def test_device_info_upsert_against_the_real_hub_table(dsn: str, pool: AsyncConnectionPool) -> None:
    """opengrid.fleet.device_info on migration 0036's columns, and the hub detail read (store.get_hub)."""
    from opengrid.api.store import PgStore
    from opengrid.fleet.device_info import upsert_device_info

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES ('fups-bank4', 'LZ_NORTH', 600)")
        conn.execute(
            "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw, lat, lon) VALUES "
            "('fups-hub-4', 'fups-bank4', 'LZ_NORTH', 39.2, 7.84, 11.0, 30.27, -97.74)"
        )
        conn.execute(
            "INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, health, last_seen_at) "
            "VALUES ('fups-hub-4', 20, 0, 'online', now())"
        )
    msg = {
        "hub_id": "fups-hub-4",
        "serial_number": "BP-LZ_NORTH-FUPS4",
        "manufacturer": "Base Power",
        "model": "BP-2",
        "firmware_version": "3.4.1",
        "hardware_revision": "C",
        "install_date": "2025-03-14",
        "commissioning_date": "2025-03-21",
        "asset_class": "HOME_BESS",
        "units": 2,
        "rated_kw": 20.0,
        "rated_kwh": 78.4,
        "reserve_floor_pct": 20.0,
        "lat": 30.27,
        "lon": -97.74,
        "inverter_model": "INV-20",
        "ts": "2026-09-26T18:00:00Z",
    }
    try:
        result = await upsert_device_info(pool, msg)
        assert result.found and set(result.rating_changes) == {"units", "p_kw", "e_kwh", "r_kwh"}
        hub = await PgStore(pool).get_hub("fups-hub-4")
        assert hub is not None
        assert (hub["units"], hub["rated_p_kw"], hub["e_kwh"]) == (2, 20.0, 78.4)
        assert hub["serial_number"] == "BP-LZ_NORTH-FUPS4" and hub["inverter_model"] == "INV-20"
        assert str(hub["installed_at"]) == "2025-03-14" and str(hub["commissioned_at"]) == "2025-03-21"
        assert hub["device_info_at"] is not None
        again = await upsert_device_info(pool, msg)
        assert again.rating_changes == {}  # the same report re-rates nothing
        assert (await upsert_device_info(pool, {**msg, "hub_id": "fups-nope"})).found is False
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute("DELETE FROM og.hub_state WHERE hub_id = 'fups-hub-4'")
            conn.execute("DELETE FROM og.hub WHERE hub_id = 'fups-hub-4'")
            conn.execute("DELETE FROM og.bank WHERE bank_id = 'fups-bank4'")
