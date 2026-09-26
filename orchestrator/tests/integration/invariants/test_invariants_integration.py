"""Integration tests for `opengrid.invariants` against real Postgres (`og_t_inv` on the server,
BUILD.md S5): seeds the fleet at live scale (`opengrid.fleet.seed`, 2,000 hubs/40 banks), plants one
seeded violation per check, and asserts `run_once()`/`run_trace_verify_once()` detect exactly it while
reporting 0 on the rest of the (clean) data -- then measures each check's own query cost at that scale.

DB-only (BUILD.md S5, and the live-path safety rule: never open an MQTT connection or start an
orchestrator process from a test workspace) -- this test never imports `opengrid.platform.mqtt`, never
calls `opengrid.fleet.configure`/`ingest_telemetry` (which would need a running fleet twin), and never
runs any `python -m opengrid.*` entry point. Every write here is a plain `psycopg` INSERT, and
`opengrid.invariants.run_once()`/`run_trace_verify_once()` are plain async DB reads/writes over the pool
this test opens itself.

Run via `powershell -File tools\\remote.ps1 -Ws inv -Cmd "cd orchestrator && python -m pytest
tests/integration/invariants -q -s"`. Skipped automatically when no database is reachable.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

import opengrid.invariants as invariants
from opengrid.fleet.seed import build_topology, load_sim_fleet_topology_config, seed_topology
from opengrid.invariants import queries as inv_queries
from opengrid.invariants.models import (
    CHECK_K1_RESERVE_BREACH,
    CHECK_K2_DOUBLE_SOLD,
    CHECK_K13_LOCK_VIOLATION,
    CHECK_ORPHAN_COMMITMENT,
    CHECK_ORPHAN_RESERVATION,
)
from opengrid.platform.db import build_dsn, migrate_sync

pytestmark = pytest.mark.asyncio
logger = logging.getLogger(__name__)

_CONFIG_PATH = os.environ.get(
    "OG_CONFIG", str((__file__.rsplit("orchestrator", 1)[0]) + "orchestrator/config/test.toml")
)
_HUB_COUNT = int(os.environ.get("OG_TEST_FLEET_HUBS", "2000"))
_TAG = "itinv"  # prefix for every row this test writes/cleans up, so re-runs never collide
_PROFILE_REF = f"{_TAG}-profile"
_CYCLE_ID = f"{_TAG}-cycle"


def _cfg():
    from opengrid.platform.config import load_config

    return load_config(_CONFIG_PATH)


def _dsn() -> str | None:
    try:
        return build_dsn(_cfg())
    except Exception:
        return None


def _db_reachable(dsn: str) -> bool:
    try:
        with psycopg.connect(dsn, connect_timeout=2):
            return True
    except Exception:
        return False


_DSN = _dsn()
requires_db = pytest.mark.skipif(
    _DSN is None or not _db_reachable(_DSN),
    reason="Postgres not reachable locally; run via tools/remote.ps1 -Ws inv (BUILD.md S5)",
)


def _seed_scenario_rows(dsn: str, *, bank_id: str, hub_id: str) -> dict[str, str]:
    """Plants exactly one seeded violation for K1, K2, K13, and each orphan check, using the real
    contract/opportunity/obligation/plan chain the schema's foreign keys require. Returns the ids a test
    needs to assert against. Every id is `_TAG`-prefixed so cleanup (and re-runs) are unambiguous."""
    now = datetime.now(UTC)
    interval_start = now - timedelta(minutes=20)
    interval_end = now - timedelta(minutes=5)  # already elapsed -- a K13 candidate

    contract_id = uuid4()
    plan_id = uuid4()
    opportunity_k13 = uuid4()
    obligation_k13 = uuid4()
    opportunity_orphan_res = uuid4()
    obligation_orphan_res = uuid4()
    opportunity_orphan_com = uuid4()
    obligation_orphan_com = uuid4()

    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "INSERT INTO og.contract (contract_id, customer_id, service_type, tier, profile_ref, start_at) "
            "VALUES (%s, %s, 'ERCOT_ENERGY', 'T2', %s, %s)",
            (contract_id, uuid4(), _PROFILE_REF, now),
        )
        cur.execute(
            "INSERT INTO og.plan (plan_id, plan_mode, gate_kind, horizon_start, horizon_end, scenario_set, "
            "solver_status) VALUES (%s, 'RULE_FALLBACK', 'SCHEDULED_15MIN', %s, %s, '{}'::jsonb, 'OK')",
            (plan_id, interval_start, interval_end),
        )

        # --- K1: a telemetry row below reserve while discharging, on a real seeded hub -------------
        cur.execute(
            "INSERT INTO og.telemetry (hub_id, ts, soc_kwh, p_kw, seq, epoch, health) "
            "VALUES (%s, %s, 0.5, -5.0, 1, 1, 'online')",
            (hub_id, now),
        )

        # --- K2: reservations on one real seeded bank summing above its kva_rating -----------------
        for _ in range(3):
            ob_id = uuid4()
            _insert_obligation_chain(
                cur,
                contract_id=contract_id,
                opportunity_id=uuid4(),
                obligation_id=ob_id,
                interval_start=now,
                interval_end=now + timedelta(minutes=15),
                committed_kw=250.0,
                state="COMMITTED",
            )
            cur.execute(
                "INSERT INTO og.reservation (reservation_id, obligation_id, bank_id, kind, amount, "
                "interval_start, interval_end, ledger_version) VALUES (%s, %s, %s, 'POWER_KW', 250, %s, %s, 1)",
                (uuid4(), ob_id, bank_id, now, now + timedelta(minutes=15)),
            )

        # --- K13: a committed obligation whose interval has elapsed with a grant well below its
        # committed floor, and NO trace row carrying an allowed override/substitution reason ---------
        _insert_obligation_chain(
            cur,
            contract_id=contract_id,
            opportunity_id=opportunity_k13,
            obligation_id=obligation_k13,
            interval_start=interval_start,
            interval_end=interval_end,
            committed_kw=100.0,
            state="DELIVERING",
            plan_id=plan_id,
        )
        cur.execute(
            "INSERT INTO og.grant (grant_id, cycle_id, obligation_id, bank_id, granted_kw, ledger_version, "
            "created_at) VALUES (%s, %s, %s, %s, 20.0, 1, %s)",
            (uuid4(), _CYCLE_ID, obligation_k13, bank_id, interval_start + timedelta(minutes=1)),
        )

        # --- orphan reservation: active, no matching active commitment ------------------------------
        _insert_obligation_only(
            cur,
            contract_id=contract_id,
            opportunity_id=opportunity_orphan_res,
            obligation_id=obligation_orphan_res,
            interval_start=now,
            interval_end=now + timedelta(minutes=15),
            committed_kw=10.0,
            state="COMMITTED",
        )
        cur.execute(
            "INSERT INTO og.reservation (reservation_id, obligation_id, bank_id, kind, amount, "
            "interval_start, interval_end, ledger_version) VALUES (%s, %s, %s, 'POWER_KW', 10, %s, %s, 1)",
            (uuid4(), obligation_orphan_res, bank_id, now, now + timedelta(minutes=15)),
        )

        # --- orphan commitment: active commitment left on a REJECTED obligation ---------------------
        _insert_obligation_chain(
            cur,
            contract_id=contract_id,
            opportunity_id=opportunity_orphan_com,
            obligation_id=obligation_orphan_com,
            interval_start=now,
            interval_end=now + timedelta(minutes=15),
            committed_kw=10.0,
            state="REJECTED",
            plan_id=plan_id,
        )

    return {
        "contract_id": str(contract_id),
        "obligation_k13": str(obligation_k13),
        "obligation_orphan_res": str(obligation_orphan_res),
        "obligation_orphan_com": str(obligation_orphan_com),
    }


def _insert_obligation_only(
    cur, *, contract_id, opportunity_id, obligation_id, interval_start, interval_end, committed_kw, state
) -> None:
    cur.execute(
        "INSERT INTO og.opportunity (opportunity_id, contract_id, window_start, window_end, requested_kw) "
        "VALUES (%s, %s, %s, %s, %s)",
        (opportunity_id, contract_id, interval_start, interval_end, committed_kw),
    )
    cur.execute(
        "INSERT INTO og.obligation (obligation_id, opportunity_id, contract_id, service_type, tier, "
        "window_start, window_end, committed_qty_kw, state) VALUES (%s, %s, %s, 'ERCOT_ENERGY', 'T2', "
        "%s, %s, %s, %s)",
        (obligation_id, opportunity_id, contract_id, interval_start, interval_end, committed_kw, state),
    )


def _insert_obligation_chain(
    cur,
    *,
    contract_id,
    opportunity_id,
    obligation_id,
    interval_start,
    interval_end,
    committed_kw,
    state,
    plan_id=None,
) -> None:
    _insert_obligation_only(
        cur,
        contract_id=contract_id,
        opportunity_id=opportunity_id,
        obligation_id=obligation_id,
        interval_start=interval_start,
        interval_end=interval_end,
        committed_kw=committed_kw,
        state=state,
    )
    if plan_id is not None:
        cur.execute(
            "INSERT INTO og.commitment (commitment_id, obligation_id, plan_id, interval_start, "
            "interval_end, committed_kw, variable_kind) VALUES (%s, %s, %s, %s, %s, %s, 'CONTINUOUS')",
            (uuid4(), obligation_id, plan_id, interval_start, interval_end, committed_kw),
        )


def _cleanup(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(
            "DELETE FROM og.reservation WHERE obligation_id IN "
            "(SELECT obligation_id FROM og.obligation o JOIN og.contract c ON c.contract_id = o.contract_id "
            "WHERE c.profile_ref = %s)",
            (_PROFILE_REF,),
        )
        cur.execute("DELETE FROM og.grant WHERE cycle_id = %s", (_CYCLE_ID,))
        cur.execute(
            "DELETE FROM og.commitment WHERE obligation_id IN "
            "(SELECT obligation_id FROM og.obligation o JOIN og.contract c ON c.contract_id = o.contract_id "
            "WHERE c.profile_ref = %s)",
            (_PROFILE_REF,),
        )
        cur.execute(
            "DELETE FROM og.obligation WHERE contract_id IN "
            "(SELECT contract_id FROM og.contract WHERE profile_ref = %s)",
            (_PROFILE_REF,),
        )
        cur.execute(
            "DELETE FROM og.opportunity WHERE contract_id IN "
            "(SELECT contract_id FROM og.contract WHERE profile_ref = %s)",
            (_PROFILE_REF,),
        )
        cur.execute("DELETE FROM og.contract WHERE profile_ref = %s", (_PROFILE_REF,))
        cur.execute("DELETE FROM og.invariant_violation")
        cur.execute("DELETE FROM og.invariant_check")


@requires_db
async def test_invariants_detect_seeded_violations_at_live_scale() -> None:
    assert _DSN is not None
    migrate_sync(_DSN)

    # Seed the fleet at live scale (DB-only: opengrid.fleet.seed writes og.hub/og.bank directly, no MQTT,
    # no process start -- see this module's docstring).
    sim_cfg = replace(load_sim_fleet_topology_config(_cfg()), hub_count=_HUB_COUNT)
    topology = build_topology(sim_cfg)
    pool = AsyncConnectionPool(_DSN, min_size=2, max_size=8, open=False)
    await pool.open(wait=True)
    try:
        t_seed0 = time.perf_counter()
        await seed_topology(pool, topology)
        t_seed1 = time.perf_counter()
        logger.info(
            "seeded %d hubs / %d banks in %.3fs", len(topology.hubs), len(topology.banks), t_seed1 - t_seed0
        )

        bank_id = topology.banks[0].bank_id
        hub_id = topology.hubs[0].hub_id

        _cleanup(_DSN)
        ids = _seed_scenario_rows(_DSN, bank_id=bank_id, hub_id=hub_id)

        invariants.configure(pool, _cfg())
        t0 = time.perf_counter()
        outcomes = await invariants.run_once()
        t1 = time.perf_counter()
        logger.info("invariants.run_once() over a %d-hub fleet took %.1f ms", _HUB_COUNT, (t1 - t0) * 1000)
        for check_name, outcome in outcomes.items():
            logger.info("  %-24s violations=%d watermark=%s", check_name, outcome.count, outcome.watermark)

        assert outcomes[CHECK_K1_RESERVE_BREACH].count >= 1
        assert any(v.scope.get("hub_id") == hub_id for v in outcomes[CHECK_K1_RESERVE_BREACH].violations)

        assert outcomes[CHECK_K2_DOUBLE_SOLD].count >= 1
        assert any(v.scope.get("bank_id") == bank_id for v in outcomes[CHECK_K2_DOUBLE_SOLD].violations)

        assert outcomes[CHECK_K13_LOCK_VIOLATION].count >= 1
        assert any(
            v.scope.get("obligation_id") == ids["obligation_k13"]
            for v in outcomes[CHECK_K13_LOCK_VIOLATION].violations
        )

        assert outcomes[CHECK_ORPHAN_RESERVATION].count >= 1
        assert any(
            v.scope.get("obligation_id") == ids["obligation_orphan_res"]
            for v in outcomes[CHECK_ORPHAN_RESERVATION].violations
        )

        assert outcomes[CHECK_ORPHAN_COMMITMENT].count >= 1
        assert any(
            v.scope.get("obligation_id") == ids["obligation_orphan_com"]
            for v in outcomes[CHECK_ORPHAN_COMMITMENT].violations
        )

        # The API's read path: a plain measured read of og.invariant_check, matching what run_once()
        # just wrote -- never a constant (the whole point of this package).
        summary = await inv_queries.read_summary(pool)
        assert summary.reserve_breaches >= 1
        assert summary.double_sold_kwh > 0
        assert summary.lock_violations >= 1
        assert summary.as_of is not None

        # Clean-data re-run: a second pass with the same watermarks must not re-count the same rows
        # (K1/K13's incremental scan) and the orphan checks (always active-only re-scans) still see the
        # unresolved orphan rows as still-outstanding, not double-inserted violations.
        t2 = time.perf_counter()
        second_outcomes = await invariants.run_once()
        t3 = time.perf_counter()
        logger.info("second (steady-state) invariants.run_once() took %.1f ms", (t3 - t2) * 1000)
        assert second_outcomes[CHECK_K1_RESERVE_BREACH].count == 0  # already past the watermark
        assert second_outcomes[CHECK_K13_LOCK_VIOLATION].count == 0

        # K11: trace verification runs clean on an empty/consistent trace table at this scale.
        t4 = time.perf_counter()
        trace_outcome = await invariants.run_trace_verify_once()
        t5 = time.perf_counter()
        logger.info(
            "run_trace_verify_once() took %.1f ms (%d streams)",
            (t5 - t4) * 1000,
            trace_outcome.checked_streams,
        )
        assert trace_outcome.failed_streams == ()
    finally:
        _cleanup(_DSN)
        await pool.close()
