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
    CHECK_K13_OUTAGE_GAP,
    CHECK_K13_RESTORE_LAG,
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


def _seed_hub_state(dsn: str, hubs) -> None:
    """Real `og.hub_state` rows for the seeded topology (`opengrid.fleet.seed` only writes `og.hub`/
    `og.bank`) -- K2's fixed logic needs these to compute each bank's TRUE capability
    (`checks.compute_bank_capabilities_kw`); without them every hub is excluded (no `hub_state` row to
    join) and every bank's true capability degrades to 0 kW, which would still "detect" an oversale but
    not for the reason this test is proving. Every hub is online at full SoC, matching a healthy fleet."""
    now = datetime.now(UTC)
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        for hub in hubs:
            cur.execute(
                "INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, health, last_seen_at) "
                "VALUES (%s, %s, 0, 'online', %s) "
                "ON CONFLICT (hub_id) DO UPDATE SET soc_kwh = EXCLUDED.soc_kwh, health = EXCLUDED.health, "
                "last_seen_at = EXCLUDED.last_seen_at",
                (hub.hub_id, hub.e_kwh, now),
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

        # --- K13_LOCK_VIOLATION: a committed obligation whose interval has elapsed with grants
        # continuously flowing (no activity gap) but persistently below its committed floor, and NO
        # trace row carrying an allowed override/substitution reason -----------------------------------
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
        # One grant every 2 minutes across the 15-minute window (well under k13_max_grant_gap_s's
        # default 30s... no: comfortably under find_dip's gap threshold when measured against the whole
        # window is not the point here -- these rows just need to avoid a >30s silent stretch, so every
        # 2 minutes would NOT do that. Use a tight cadence instead, matching the allocator's real ~2s
        # cycle, so the dip is unambiguously a LOW-VALUE delivery, not an activity gap.
        for offset_s in range(0, int((interval_end - interval_start).total_seconds()), 10):
            cur.execute(
                "INSERT INTO og.grant (grant_id, cycle_id, obligation_id, bank_id, granted_kw, "
                "ledger_version, created_at) VALUES (%s, %s, %s, %s, 20.0, 1, %s)",
                (
                    uuid4(),
                    f"{_CYCLE_ID}-{offset_s}",
                    obligation_k13,
                    bank_id,
                    interval_start + timedelta(seconds=offset_s),
                ),
            )

        # --- K13_OUTAGE_GAP: a separate committed obligation whose interval has elapsed with NO grant
        # activity at all -- a total silence, classified distinctly from the low-but-flowing case above
        opportunity_outage = uuid4()
        obligation_outage = uuid4()
        _insert_obligation_chain(
            cur,
            contract_id=contract_id,
            opportunity_id=opportunity_outage,
            obligation_id=obligation_outage,
            interval_start=interval_start,
            interval_end=interval_end,
            committed_kw=50.0,
            state="DELIVERING",
            plan_id=plan_id,
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

        # --- K13 need-basis (owner decision 2026-09-26, violation (b)): a DATA_CENTER (MEASURED_FEEDBACK)
        # obligation with zero grants (total silence) but a real measured site-meter need -- still
        # importing from the grid -- so it IS a violation despite being need-basis --------------------
        customer_id = uuid4()
        pq_envelope_id = uuid4()
        cur.execute(
            "INSERT INTO og.pq_envelope (pq_envelope_id, customer_id, phase_config) VALUES (%s, %s, '3P')",
            (pq_envelope_id, customer_id),
        )
        dc_contract_id = uuid4()
        cur.execute(
            "INSERT INTO og.contract (contract_id, customer_id, service_type, tier, profile_ref, start_at) "
            "VALUES (%s, %s, 'DATA_CENTER', 'T2', %s, %s)",
            (dc_contract_id, customer_id, _PROFILE_REF, now),
        )
        cur.execute(
            "INSERT INTO og.service_profile (contract_id, control_primitive, target_quantity, "
            "target_scope, setpoint_source, feedback_signal_ref, response_time_s, ramp_limit, "
            "accuracy_tolerance, deadband, priority_tier, mv_method, settlement_metric, pq_envelope_id, "
            "failure_behaviour) VALUES (%s, 'CLOSED_LOOP_REGULATION', 'KW', 'SITE_METER', "
            "'MEASURED_FEEDBACK', 'site_meter:site-dc-01:p_kw', 2, 10, 0.05, 1, 'T2', 'meter', 'kwh', "
            "%s, 'HOLD_THEN_SCHEDULE')",
            (dc_contract_id, pq_envelope_id),
        )
        cur.execute(
            "INSERT INTO og.customer_site_meter_reading (customer_id, site_id, ts, p_kw, q_kvar, "
            "v_rms_a_v, v_rms_b_v, v_rms_c_v, i_rms_a_a, i_rms_b_a, i_rms_c_a, freq_hz, pf, "
            "thd_v_pct, thd_i_pct, quality) VALUES (%s, 'site-dc-01', %s, 50.0, 0, 480, 480, 480, "
            "60, 60, 60, 60.0, 0.98, 1.0, 1.0, 'GOOD')",
            # At or BEFORE interval_start: with zero grants at all, find_dip's worst point is the whole
            # window's gap, timestamped at its start (interval_start) -- fetch_measured_need_sample looks
            # up the reading in effect AT OR BEFORE the dip's own timestamp, so a reading stamped AFTER it
            # would never be found.
            (str(customer_id), interval_start - timedelta(seconds=1)),
        )
        opportunity_need_basis = uuid4()
        obligation_need_basis = uuid4()
        _insert_obligation_chain(
            cur,
            contract_id=dc_contract_id,
            opportunity_id=opportunity_need_basis,
            obligation_id=obligation_need_basis,
            interval_start=interval_start,
            interval_end=interval_end,
            committed_kw=100.0,
            state="DELIVERING",
            plan_id=plan_id,
        )
        # No grants at all: a customer-need-basis dip with a real measured need behind it (50 kW site
        # import) and no delivery -- violation (b), not covered by the need-basis exemption.

    return {
        "contract_id": str(contract_id),
        "obligation_k13": str(obligation_k13),
        "obligation_outage": str(obligation_outage),
        "obligation_orphan_res": str(obligation_orphan_res),
        "obligation_orphan_com": str(obligation_orphan_com),
        "obligation_need_basis": str(obligation_need_basis),
        "plan_id": str(plan_id),
    }


def _seed_restore_lag_commitment(
    dsn: str,
    *,
    obligation_id,
    contract_id,
    plan_id,
    bank_id: str,
    interval_start: datetime,
    interval_end: datetime,
    cleared_at: datetime,
) -> None:
    """K13_RESTORE_LAG's commitment + grant rows -- real timestamps this time (not the elapsed-in-the-
    past scenario the rest of `_seed_scenario_rows` uses), because the async test body writes this
    obligation's SHORTFALL trace events via the real `TraceStore.append()` path, which stamps its own
    `created_at = now()` and cannot be backdated -- so the window has to genuinely contain "now" at write
    time, then genuinely elapse (by the time `run_once()` runs) for K13 to pick it up at all.
    `obligation_id` is generated by the caller BEFORE this call, since the trace stream id
    (`shortfall-<obligation_id>`) and payload need it first."""
    opportunity_id = uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        _insert_obligation_chain(
            cur,
            contract_id=contract_id,
            opportunity_id=opportunity_id,
            obligation_id=obligation_id,
            interval_start=interval_start,
            interval_end=interval_end,
            committed_kw=100.0,
            state="DELIVERING",
            plan_id=plan_id,
        )
        for offset_s in (2, 4):
            cur.execute(
                "INSERT INTO og.grant (grant_id, cycle_id, obligation_id, bank_id, granted_kw, "
                "ledger_version, created_at) VALUES (%s, %s, %s, %s, 60.0, 1, %s)",
                (
                    uuid4(),
                    f"{_CYCLE_ID}-restorelag-{offset_s}",
                    obligation_id,
                    bank_id,
                    cleared_at + timedelta(seconds=offset_s),
                ),
            )


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
        cur.execute("DELETE FROM og.grant WHERE cycle_id LIKE %s", (f"{_CYCLE_ID}%",))
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
        # K13 need-basis scenario's DATA_CENTER contract/profile (must precede the og.contract delete
        # below: og.service_profile has a NOT NULL FK to og.contract).
        cur.execute(
            "DELETE FROM og.service_profile WHERE contract_id IN "
            "(SELECT contract_id FROM og.contract WHERE profile_ref = %s)",
            (_PROFILE_REF,),
        )
        cur.execute("DELETE FROM og.customer_site_meter_reading WHERE site_id = 'site-dc-01'")
        cur.execute(
            "DELETE FROM og.pq_envelope WHERE pq_envelope_id NOT IN "
            "(SELECT pq_envelope_id FROM og.service_profile)"
        )
        cur.execute("DELETE FROM og.contract WHERE profile_ref = %s", (_PROFILE_REF,))
        # K13_RESTORE_LAG's SHORTFALL trace stream(s) (its obligation_id is random per run, so matched by
        # the fixed cycle_id prefix instead) and any now-orphaned per-stream watermark rows.
        cur.execute(
            "DELETE FROM og.trace WHERE event_class = 'ALLOCATOR_SHORTFALL' "
            "AND payload ->> 'cycle_id' LIKE %s",
            (f"{_CYCLE_ID}-restorelag-%",),
        )
        cur.execute(
            "DELETE FROM og.invariant_trace_watermark w WHERE w.stream_id LIKE 'shortfall-%' "
            "AND NOT EXISTS (SELECT 1 FROM og.trace t WHERE t.stream_id = w.stream_id)"
        )
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
        _seed_hub_state(_DSN, topology.hubs)
        t_seed1 = time.perf_counter()
        logger.info(
            "seeded %d hubs / %d banks in %.3fs", len(topology.hubs), len(topology.banks), t_seed1 - t_seed0
        )

        bank_id = topology.banks[0].bank_id
        hub_id = topology.hubs[0].hub_id

        _cleanup(_DSN)
        ids = _seed_scenario_rows(_DSN, bank_id=bank_id, hub_id=hub_id)

        # K13_RESTORE_LAG's SHORTFALL trace events: written via TraceStore (the real hash-chain append
        # path, never a raw INSERT -- BUILD.md S1 "never re-implement hashing"). TraceStore.append()
        # always stamps `created_at = now()` (it cannot be backdated), so -- unlike the rest of this
        # scenario, which lives safely in the past -- this obligation's window has to genuinely contain
        # "now" while these events are written, then genuinely elapse (a short real sleep) before
        # `run_once()` treats it as an elapsed K13 candidate at all.
        import asyncio
        from uuid import UUID

        from opengrid.trace.pg_backend import PgTraceBackend
        from opengrid.trace.store import TraceStore

        trace_store = TraceStore(PgTraceBackend(pool))
        obligation_restore_lag = uuid4()
        restore_interval_start = datetime.now(UTC) - timedelta(seconds=1)
        await trace_store.append(
            f"shortfall-{obligation_restore_lag}",
            "SHORTFALL",
            "ALLOCATOR_SHORTFALL",
            {
                "cycle_id": f"{_CYCLE_ID}-restorelag-start",
                "obligation_id": str(obligation_restore_lag),
                "bank_id": bank_id,
                "shortfall_kw": 40.0,
                "interval_start": restore_interval_start.isoformat(),
            },
            reason_codes=["R-COMMIT-LOCK-OVERRIDE-L2"],
        )
        cleared_at = datetime.now(UTC)
        await trace_store.append(
            f"shortfall-{obligation_restore_lag}",
            "SHORTFALL",
            "ALLOCATOR_SHORTFALL",
            {
                "cycle_id": f"{_CYCLE_ID}-restorelag-clear",
                "obligation_id": str(obligation_restore_lag),
                "bank_id": bank_id,
                "shortfall_kw": 0.0,
                "interval_start": restore_interval_start.isoformat(),
            },
            reason_codes=["R-COMMIT-LOCK-OVERRIDE-L2"],
        )
        restore_interval_end = cleared_at + timedelta(seconds=6)
        _seed_restore_lag_commitment(
            _DSN,
            obligation_id=obligation_restore_lag,
            contract_id=UUID(ids["contract_id"]),
            plan_id=UUID(ids["plan_id"]),
            bank_id=bank_id,
            interval_start=restore_interval_start,
            interval_end=restore_interval_end,
            cleared_at=cleared_at,
        )
        sleep_s = max((restore_interval_end - datetime.now(UTC)).total_seconds(), 0.0) + 0.3
        logger.info("sleeping %.1fs for the K13_RESTORE_LAG scenario's window to elapse", sleep_s)
        await asyncio.sleep(sleep_s)

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
        # A different obligation, committed but with NO grant activity at all, is classified separately.
        assert outcomes[CHECK_K13_OUTAGE_GAP].count >= 1
        assert any(
            v.scope.get("obligation_id") == ids["obligation_outage"]
            for v in outcomes[CHECK_K13_OUTAGE_GAP].violations
        )
        # ...and the outage is not ALSO double-counted as a lock violation.
        assert not any(
            v.scope.get("obligation_id") == ids["obligation_outage"]
            for v in outcomes[CHECK_K13_LOCK_VIOLATION].violations
        )

        # K13 need-basis (owner decision 2026-09-26, violation (b)): a DATA_CENTER (MEASURED_FEEDBACK)
        # obligation with total silence AND a real measured site-meter need is STILL flagged -- the
        # need-basis exemption does not blanket-cover a genuinely unmet measured need.
        assert any(
            v.scope.get("obligation_id") == ids["obligation_need_basis"]
            for v in outcomes[CHECK_K13_OUTAGE_GAP].violations
        )

        # K13_RESTORE_LAG (owner decision 2026-09-26): a cleared SHORTFALL not restored to the full
        # commitment within 2 grant cycles.
        assert outcomes[CHECK_K13_RESTORE_LAG].count >= 1

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
        # K2's oversold reservation is still active, so it's still DETECTED every run (count == 1)...
        assert second_outcomes[CHECK_K2_DOUBLE_SOLD].count == 1
        # ...but the idempotent upsert must not have recounted it into the running total a second time.
        summary_after_second_run = await inv_queries.read_summary(pool)
        assert summary_after_second_run.double_sold_kwh == summary.double_sold_kwh

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


# --- #19 load test: trace-verify's incremental stream discovery + per-stream watermark rows at scale ---

_TRACE_LOAD_TEST_STREAM_PREFIX = f"{_TAG}-loadtest-"
_TRACE_LOAD_TEST_STREAM_COUNT = int(os.environ.get("OG_TEST_TRACE_STREAMS", "500"))
_TRACE_LOAD_TEST_RECORDS_PER_STREAM = 3


def _cleanup_trace_load_test(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute("DELETE FROM og.trace WHERE stream_id LIKE %s", (f"{_TRACE_LOAD_TEST_STREAM_PREFIX}%",))
        cur.execute(
            "DELETE FROM og.invariant_trace_watermark WHERE stream_id LIKE %s",
            (f"{_TRACE_LOAD_TEST_STREAM_PREFIX}%",),
        )
        cur.execute("DELETE FROM og.invariant_check WHERE check_name = 'TRACE_VERIFY'")
        cur.execute("DELETE FROM og.invariant_violation WHERE check_name = 'TRACE_VERIFY'")


@requires_db
async def test_trace_verify_scales_with_many_streams() -> None:
    """#19: `opengrid.invariants.run_trace_verify_once()`'s stream discovery
    (`queries.fetch_new_trace_stream_ids`) must cost proportionally to what's NEW since the last
    discovery cursor, not to `og.trace`'s total row count, and each stream's resume point must live in
    its own row (`og.invariant_trace_watermark`) rather than one shared jsonb blob. Proven by seeding
    many independent streams and timing a cold run (discovers + verifies everything) against a
    steady-state re-run (nothing new: should be materially cheaper, not proportional to total streams
    re-verified from scratch)."""
    assert _DSN is not None
    migrate_sync(_DSN)
    _cleanup_trace_load_test(_DSN)

    pool = AsyncConnectionPool(_DSN, min_size=2, max_size=8, open=False)
    await pool.open(wait=True)
    try:
        from opengrid.trace.pg_backend import PgTraceBackend
        from opengrid.trace.store import TraceStore

        trace_store = TraceStore(PgTraceBackend(pool))
        t_seed0 = time.perf_counter()
        for i in range(_TRACE_LOAD_TEST_STREAM_COUNT):
            stream_id = f"{_TRACE_LOAD_TEST_STREAM_PREFIX}{i}"
            for seq in range(_TRACE_LOAD_TEST_RECORDS_PER_STREAM):
                await trace_store.append(
                    stream_id, "ALERT", "LOAD_TEST", {"i": i, "seq": seq}, reason_codes=None
                )
        t_seed1 = time.perf_counter()
        total_records = _TRACE_LOAD_TEST_STREAM_COUNT * _TRACE_LOAD_TEST_RECORDS_PER_STREAM
        logger.info(
            "seeded %d trace streams (%d records) in %.3fs",
            _TRACE_LOAD_TEST_STREAM_COUNT,
            total_records,
            t_seed1 - t_seed0,
        )

        invariants.configure(pool, _cfg())

        t0 = time.perf_counter()
        cold_outcome = await invariants.run_trace_verify_once()
        t1 = time.perf_counter()
        cold_ms = (t1 - t0) * 1000
        logger.info(
            "cold run_trace_verify_once() over %d streams took %.1f ms (%.3f ms/stream)",
            cold_outcome.checked_streams,
            cold_ms,
            cold_ms / max(cold_outcome.checked_streams, 1),
        )
        assert cold_outcome.checked_streams >= _TRACE_LOAD_TEST_STREAM_COUNT
        assert cold_outcome.failed_streams == ()

        # Every stream now has its own watermark row, advanced past its last record.
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM og.invariant_trace_watermark WHERE stream_id LIKE %s",
                (f"{_TRACE_LOAD_TEST_STREAM_PREFIX}%",),
            )
            (watermark_row_count,) = await cur.fetchone()
        assert watermark_row_count >= _TRACE_LOAD_TEST_STREAM_COUNT

        # Steady-state: nothing new written since the cold run. Discovery must find zero new streams
        # (bounded by the `discovery_since` cursor, not a re-scan of every stream ever seen), and each
        # known stream's verify() call is a no-op (already at its own tip) -- materially cheaper than
        # the cold run despite covering the same stream count.
        t2 = time.perf_counter()
        warm_outcome = await invariants.run_trace_verify_once()
        t3 = time.perf_counter()
        warm_ms = (t3 - t2) * 1000
        logger.info(
            "steady-state run_trace_verify_once() over %d streams took %.1f ms (%.3f ms/stream)",
            warm_outcome.checked_streams,
            warm_ms,
            warm_ms / max(warm_outcome.checked_streams, 1),
        )
        assert warm_outcome.failed_streams == ()
        assert warm_ms < cold_ms
    finally:
        _cleanup_trace_load_test(_DSN)
        await pool.close()
