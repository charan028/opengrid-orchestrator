"""`opengrid.api.views_ext.PgExtViews` (Gitea #19) against real Postgres: every query runs on the migrated
schema, returns the documented shapes, and the fleet-map query answers for 2,000 hubs inside the 300 ms
budget. Server only (`tools/remote.ps1`); seeds rows under a `ui19-` prefix and removes them afterwards."""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg.types.json import Jsonb

pytestmark = pytest.mark.skipif(
    not os.environ.get("OG_DB"), reason="requires the server environment (tools/remote.ps1)"
)

HUBS = 2000
BANKS = 40
ZONES = ("LZ_NORTH", "LZ_SOUTH", "LZ_HOUSTON", "LZ_WEST")
PREFIX = "ui19"


def _bank(i: int) -> str:
    return f"{PREFIX}-bank-{i:03d}"


def _hub(i: int) -> str:
    return f"{PREFIX}-hub-{i:05d}"


@pytest.fixture(scope="module")
def dsn(server_config, _migrated) -> str:
    from opengrid.platform.db import build_dsn

    return build_dsn(server_config)


@pytest.fixture(scope="module")
def seeded(dsn: str):
    now = datetime.now(UTC)
    ids = {
        "contract": uuid4(),
        "customer": uuid4(),
        "opportunity": uuid4(),
        "rejected_opportunity": uuid4(),
        "obligation": uuid4(),
        "rejected_obligation": uuid4(),
        "plan": uuid4(),
        "commitment": uuid4(),
        "reservation": uuid4(),
        "grant": uuid4(),
        "trace": uuid4(),
        "cycle": f"{int(now.timestamp())}-999999",
    }
    with psycopg.connect(dsn, autocommit=True) as conn:
        _cleanup(conn, ids)
        with conn.transaction():
            cur = conn.cursor()
            cur.executemany(
                "INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES (%s, %s, 600)",
                [(_bank(b), ZONES[b % 4]) for b in range(BANKS)],
            )
            cur.executemany(
                "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw) VALUES (%s, %s, %s, 39.2, 7.84, 11)",
                [(_hub(i), _bank(i % BANKS), ZONES[(i % BANKS) % 4]) for i in range(HUBS)],
            )
            cur.executemany(
                "INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, health, last_seen_at) VALUES (%s, 20, %s, %s, now())",
                [
                    (_hub(i), 5.0 if i % BANKS == 0 else 0.0, "fault" if i == 3 else "online")
                    for i in range(HUBS)
                ],
            )
            cur.execute(
                "INSERT INTO og.contract (contract_id, customer_id, service_type, tier, profile_ref, start_at) "
                "VALUES (%s, %s, 'ERCOT_ENERGY', 'T2', 'p@1', now())",
                (ids["contract"], ids["customer"]),
            )
            cur.execute(
                "INSERT INTO og.plan (plan_id, plan_mode, gate_kind, horizon_start, horizon_end, scenario_set, "
                "solver_status) VALUES (%s, 'L-DA', 'SCHEDULED_15MIN', %s, %s, %s, 'OPTIMAL')",
                (ids["plan"], now - timedelta(hours=1), now + timedelta(hours=1), Jsonb({})),
            )
            for opp, obl, opp_state, obl_state in (
                (ids["opportunity"], ids["obligation"], "SELECTED", "DELIVERING"),
                (ids["rejected_opportunity"], ids["rejected_obligation"], "REJECTED", "REJECTED"),
            ):
                cur.execute(
                    "INSERT INTO og.opportunity (opportunity_id, contract_id, window_start, window_end, requested_kw, "
                    "state, reason_code) VALUES (%s, %s, %s, %s, 50, %s, %s)",
                    (
                        opp,
                        ids["contract"],
                        now - timedelta(hours=1),
                        now + timedelta(hours=1),
                        opp_state,
                        "R-GATE-REJECT" if opp_state == "REJECTED" else None,
                    ),
                )
                cur.execute(
                    "INSERT INTO og.obligation (obligation_id, opportunity_id, contract_id, service_type, tier, "
                    "window_start, window_end, committed_qty_kw, state) "
                    "VALUES (%s, %s, %s, 'ERCOT_ENERGY', 'T2', %s, %s, 50, %s)",
                    (
                        obl,
                        opp,
                        ids["contract"],
                        now - timedelta(hours=1),
                        now + timedelta(hours=1),
                        obl_state,
                    ),
                )
            cur.execute(
                "INSERT INTO og.commitment (commitment_id, obligation_id, plan_id, interval_start, interval_end, "
                "committed_kw, variable_kind) VALUES (%s, %s, %s, %s, %s, 40, 'CONTINUOUS')",
                (
                    ids["commitment"],
                    ids["obligation"],
                    ids["plan"],
                    now - timedelta(hours=1),
                    now + timedelta(hours=1),
                ),
            )
            cur.execute(
                "INSERT INTO og.reservation (reservation_id, obligation_id, bank_id, kind, amount, interval_start, "
                "interval_end, ledger_version) VALUES (%s, %s, %s, 'POWER_KW', 40, %s, %s, 1)",
                (
                    ids["reservation"],
                    ids["obligation"],
                    _bank(0),
                    now - timedelta(hours=1),
                    now + timedelta(hours=1),
                ),
            )
            cur.execute(
                "INSERT INTO og.grant (grant_id, cycle_id, obligation_id, bank_id, granted_kw, ledger_version) "
                "VALUES (%s, %s, %s, %s, 40, 1)",
                (ids["grant"], ids["cycle"], ids["obligation"], _bank(0)),
            )
            cur.execute(
                "INSERT INTO og.feed_obs (source, product, series, ts, value, unit, quality) VALUES "
                "('ERCOT', 'np6-345-cd', 'coast', %s, 17000, 'mw', 'GOOD'), "
                "('scada', %s, 'REAL_POWER_KW', %s, 420, 'kw', 'GOOD')",
                (now - timedelta(minutes=30), _bank(0), now - timedelta(seconds=5)),
            )
            cur.execute(
                "INSERT INTO og.customer_site_meter_reading (customer_id, site_id, ts, p_kw, q_kvar, v_rms_a_v, "
                "v_rms_b_v, v_rms_c_v, i_rms_a_a, i_rms_b_a, i_rms_c_a, freq_hz, pf, thd_v_pct, thd_i_pct, quality) "
                "VALUES (%s, 'ui19-site', now(), 1200, 0, 277, 277, 277, 1, 1, 1, 60, 1, 1, 1, 'GOOD')",
                (str(ids["customer"]),),
            )
            cur.execute(
                "INSERT INTO og.trace (trace_id, decision_type, event_class, stream_id, seq, payload, reason_codes, "
                "hash) VALUES (%s, 'ADMISSION', 'ADMISSION', %s, 0, %s, ARRAY['R-ADMIT-REJECT'], 'ui19')",
                (
                    ids["trace"],
                    f"{PREFIX}-admission-{ids['trace']}",
                    Jsonb({"contract_id": str(ids["contract"])}),
                ),
            )
    yield ids
    with psycopg.connect(dsn, autocommit=True) as conn:
        _cleanup(conn, ids)


def _cleanup(conn: psycopg.Connection, ids: dict) -> None:
    with conn.transaction():
        cur = conn.cursor()
        cur.execute("DELETE FROM og.trace WHERE stream_id LIKE %s", (f"{PREFIX}-admission-%",))
        cur.execute("DELETE FROM og.customer_site_meter_reading WHERE site_id = 'ui19-site'")
        cur.execute("DELETE FROM og.feed_obs WHERE (source = 'scada' AND product LIKE %s)", (f"{PREFIX}-%",))
        cur.execute("DELETE FROM og.grant WHERE bank_id LIKE %s", (f"{PREFIX}-%",))
        cur.execute("DELETE FROM og.reservation WHERE bank_id LIKE %s", (f"{PREFIX}-%",))
        cur.execute(
            "DELETE FROM og.commitment WHERE obligation_id IN "
            "(SELECT obligation_id FROM og.obligation WHERE contract_id = %s)",
            (ids["contract"],),
        )
        cur.execute("DELETE FROM og.obligation WHERE contract_id = %s", (ids["contract"],))
        cur.execute("DELETE FROM og.opportunity WHERE contract_id = %s", (ids["contract"],))
        cur.execute("DELETE FROM og.plan WHERE plan_id = %s", (ids["plan"],))
        cur.execute("DELETE FROM og.contract WHERE contract_id = %s", (ids["contract"],))
        cur.execute("DELETE FROM og.hub_state WHERE hub_id LIKE %s", (f"{PREFIX}-%",))
        cur.execute("DELETE FROM og.hub WHERE hub_id LIKE %s", (f"{PREFIX}-%",))
        cur.execute("DELETE FROM og.bank WHERE bank_id LIKE %s", (f"{PREFIX}-%",))


@pytest.fixture
async def views(server_config, seeded) -> AsyncIterator:
    from opengrid.api.views_ext import PgExtViews
    from opengrid.platform.db import make_pool

    pool = await make_pool(server_config)
    try:
        yield PgExtViews(pool)
    finally:
        await pool.close()


async def test_fleet_map_rows_shape_grants_and_speed(views, seeded) -> None:
    await views.fleet_map_rows(grant_fresh_s=10)  # warm the column cache and the connection
    started = time.perf_counter()
    rows = await views.fleet_map_rows(grant_fresh_s=10)
    elapsed_ms = (time.perf_counter() - started) * 1000
    ours = {r["hub_id"]: r for r in rows if r["hub_id"].startswith(PREFIX)}
    assert len(ours) == HUBS
    assert elapsed_ms < 300, f"fleet map query took {elapsed_ms:.0f} ms"
    delivering = ours[_hub(0)]
    assert delivering["bank_has_grant"] is True and delivering["bank_granted_kw"] == 40.0
    assert [o["obligation_id"] for o in delivering["serving_obligations"]] == [str(seeded["obligation"])]
    assert delivering["serving_obligations"][0]["state"] == "DELIVERING"
    assert ours[_hub(1)]["bank_has_grant"] is False and ours[_hub(1)]["serving_obligations"] == []
    assert ours[_hub(3)]["health"] == "fault"
    assert {"home_load_kw", "meter_kw", "pv_kw"} <= set(delivering)


async def test_small_reads(views, seeded) -> None:
    markets = await views.active_contract_markets()
    assert any(m.service_type == "ERCOT_ENERGY" for m in markets)
    assert isinstance(await views.critical_alert_scopes(), set)
    readings = await views.customer_site_readings(max_age_s=600)
    assert any(r["site_id"] == "ui19-site" and r["p_kw"] == 1200 for r in readings)
    services = await views.customer_contract_services()
    assert services[str(seeded["customer"])] == ["ERCOT_ENERGY"]
    assert (await views.weather_zone_load_mw())["coast"][0] >= 0
    assert (await views.bank_scada_load_kw())[_bank(0)][0] == 420.0


async def test_ledger_slices(views, seeded) -> None:
    now = datetime.now(UTC)
    reservations, commitments = await views.ledger_slices(
        t0=now - timedelta(hours=2), t1=now + timedelta(hours=2)
    )
    assert any(r.obligation_id == seeded["obligation"] and r.bank_id == _bank(0) for r in reservations)
    assert any(c.obligation_id == seeded["obligation"] and c.committed_kw == 40.0 for c in commitments)


async def test_funnel_rows(views, seeded) -> None:
    now = datetime.now(UTC)
    rows = await views.funnel_rows(t0=now - timedelta(days=1), t1=now + timedelta(minutes=1), bucket="hour")
    states = {(r.opportunity_state, r.obligation_state, r.reason_code) for r in rows}
    assert ("SELECTED", "DELIVERING", None) in states
    assert ("REJECTED", "REJECTED", "R-GATE-REJECT") in states
    assert (None, None, "R-ADMIT-REJECT") in states
    assert await views.mms_funnel_rows(t0=now - timedelta(days=1), t1=now, bucket="hour") is None


async def test_contract_totals(views, seeded) -> None:
    now = datetime.now(UTC)
    totals = await views.contract_totals(start=now - timedelta(days=1), end=now + timedelta(days=1))
    assert any(t.scope_ref == str(seeded["contract"]) for t in totals)
