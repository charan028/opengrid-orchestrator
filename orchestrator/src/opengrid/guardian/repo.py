"""Postgres/process-backed implementations of `opengrid.guardian.ports` for the real `og-guardian`
process (`main.py`). Kept separate from `service.py`/`checks.py` so the decision logic has no
psycopg/subprocess import (BUILD.md S5a "pure logic separated from I/O"), mirroring `opengrid.trace.
pg_backend` and `opengrid.ledger.pg_backend`.

**Proposed-batch hand-off (02b S5.1 item 1: "engine -> guardian: proposed batch -- internal, not MQTT,
same process boundary is Postgres LISTEN/NOTIFY or a direct call").** MVP-S's schema (`og.command_batch`)
persists only batch-level metadata (`merkle_root`, `command_count`) -- the per-hub item content lives in
the batch's own K10 decision pre-image, which the engine must write to `og.trace` (`decision_type=
'RT_ALLOCATION'`, `event_class='RT_ALLOCATION'`) *before* invoking guardian, with a payload shaped like
`_trace_payload_to_proposal` below. `PgProposalPort` reads that same row back -- it is guardian's OWN
independent read of the pre-image, not a value the engine hands it a second time, so G-14 (pre-image
exists) and the proposal fetch are always consistent by construction.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import json
import logging
import math
import sys
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, ClassVar, Literal
from uuid import UUID, uuid4

from psycopg_pool import AsyncConnectionPool

from opengrid.core.physics import BankParams, HubParams
from opengrid.core.pq import OffsetVector
from opengrid.guardian.config import DEFAULT_CLOCK_CACHE_S, ClockSource
from opengrid.guardian.ports import (
    ActiveObligation,
    AlertPort,
    BankMembersPort,
    BankSnapshot,
    ClockPort,
    EngagedStop,
    GridTopologyPort,
    GuardianPorts,
    HubSnapshot,
    HubStatePort,
    L2InstructionPort,
    PqPorts,
    ProposedBatch,
    ProposedItem,
    ReleaseRequest,
    SafeStopScope,
    StopReleasePort,
    StopScopeKind,
    TerritoryPort,
)
from opengrid.guardian.pq_repo import (
    PgCalibrationHistoryPort,
    PgCalibrationLedgerPort,
    PgHubAssetStatePort,
    PgPqEnvelopeStatePort,
    PgPqMeasurementPort,
    PgSensitiveGrantPort,
    StaticFirmwareCalibrationBoundsPort,
)
from opengrid.ledger.pg_backend import PgLedgerBackend
from opengrid.safestop.pg_backend import REQUEST_CHANNEL
from opengrid.trace import TraceStore

logger = logging.getLogger(__name__)

_BANK_SNAPSHOT_SQL = "SELECT kva_rating, reserve_kva, feeder_id FROM og.bank WHERE bank_id = %(bank_id)s"

#: G-03's bank loading: the latest GOOD SCADA reading only. A reading the fleet ingest marked ESTIMATED or
#: STALE (`fleet.pg_backend`'s quality mapping: stale, missing, out of range, comm fail) is ignored, so a
#: bank whose recent readings are all bad carries the last good one's age -- stale, vetoed -- never a bad
#: value (often 0 kVA) taken at face value.
_BANK_LOAD_SQL = """
SELECT value, extract(epoch FROM now() - ts) FROM og.feed_obs
WHERE source = 'scada' AND product = %(bank_id)s AND series = 'APPARENT_POWER_KVA' AND quality = 'GOOD'
ORDER BY ts DESC LIMIT 1
"""

_ACTIVE_COMMITMENT_SQL = """
SELECT committed_kw FROM og.commitment
WHERE obligation_id = %(obligation_id)s AND supersedes IS NULL
ORDER BY interval_start DESC LIMIT 1
"""

# GUARD-01/K13: guardian's own, independent enumeration of every obligation with an ACTIVE commitment
# against this bank -- read-only from og.reservation/og.commitment, never from the proposed batch's own
# item list, so a batch cannot evade G-19 by omitting an obligation or relabelling it obligation_id=None.
#
# Scoped to reservations covering NOW on this bank, with this bank's reserved kW as the frozen amount: a
# batch is one bank for one cycle, so obligations reserved for later intervals (or on other banks) are
# not part of it. Unscoped, every future commitment on the bank counted as "omitted, 0 kW" and G-19
# vetoed every batch (live 2026-09-26).
_ACTIVE_OBLIGATIONS_FOR_BANK_SQL = """
SELECT r.obligation_id, SUM(r.amount) AS frozen_kw
FROM og.reservation r
JOIN og.commitment c ON c.obligation_id = r.obligation_id AND c.supersedes IS NULL
    AND c.interval_start = r.interval_start
WHERE r.bank_id = %(bank_id)s AND r.released_at IS NULL
  AND r.interval_start <= now() AND r.interval_end > now()
GROUP BY r.obligation_id
"""

_PRIOR_GRANT_SQL = """
SELECT granted_kw FROM og.grant WHERE obligation_id = %(obligation_id)s
ORDER BY created_at DESC LIMIT 1
"""

_STOP_STATE_SQL = """
SELECT action FROM og.stop_event WHERE scope_kind = %(scope_kind)s AND scope_ref = %(scope_ref)s
ORDER BY created_at DESC LIMIT 1
"""

_PROPOSAL_SQL = """
SELECT payload FROM og.trace
WHERE decision_type = 'RT_ALLOCATION' AND payload ->> 'command_batch_id' = %(command_batch_id)s
ORDER BY seq DESC LIMIT 1
"""


#: `utility_scale`: the hub's bank is an og.asset SUBSTATION (migration 0025; the D-29 20 MW set is a one-hub
#: bank) -- rated at its nameplate, never the home per-unit cap.
_ALL_HUB_PARAMS_SQL = """
SELECT h.hub_id, h.e_kwh, h.r_kwh, h.p_kw, h.eta_c, h.eta_d, h.units,
       EXISTS (SELECT 1 FROM og.asset a WHERE a.bank_id = h.bank_id AND a.asset_class = 'SUBSTATION')
           AS utility_scale
FROM og.hub h
"""


async def load_hub_params(pool: AsyncConnectionPool) -> dict[str, HubSnapshot]:
    """Seed `MqttHubStatePort` with every hub's static physical params (`og.hub`, config data -- not a
    live signal, so reading it from Postgres does not compromise the telemetry independence guardian
    otherwise keeps via MQTT). `soc_kwh`/`prev_p_kw` start at 0 and `health="stale"` until the first
    telemetry message for that hub arrives. `units` (migration 0032) feeds G-02's per-unit cap."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_ALL_HUB_PARAMS_SQL)
        rows = await cur.fetchall()
    return {
        hub_id: HubSnapshot(
            params=HubParams(
                e_kwh=e_kwh,
                r_kwh=r_kwh,
                p_kw=p_kw,
                eta_c=eta_c,
                eta_d=eta_d,
                units=units,
                utility_scale=bool(utility_scale),
            ),
            soc_kwh=0.0,
            prev_p_kw=0.0,
            health="stale",
        )
        for hub_id, e_kwh, r_kwh, p_kw, eta_c, eta_d, units, utility_scale in rows
    }


class PgBankStatePort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def snapshot(self, bank_id: str) -> BankSnapshot | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_BANK_SNAPSHOT_SQL, {"bank_id": bank_id})
            bank_row = await cur.fetchone()
            if bank_row is None:
                return None
            await cur.execute(_BANK_LOAD_SQL, {"bank_id": bank_id})
            load_row = await cur.fetchone()
        kva_rating, reserve_kva, feeder_id = bank_row
        # No reading at all is unknown loading (age inf -> G-03 vetoes), never an empty bank.
        bank_load_kva = float(load_row[0]) if load_row else 0.0
        load_age_s = float(load_row[1]) if load_row and load_row[1] is not None else math.inf
        return BankSnapshot(
            params=BankParams(kva_rating=kva_rating, reserve_kva=reserve_kva),
            bank_load_kva=bank_load_kva,
            feeder_id=feeder_id,
            feeder_ceiling_kw_per_min=None,
            bank_load_age_s=load_age_s,
        )


class PgLedgerVersionPort:
    """G-09's current ledger version, read from Postgres through the ledger's own backend query
    (`PgLedgerBackend.current_version`, single owner). The module-level `opengrid.ledger.ledger_version()`
    is the og-engine process's in-memory facade and is never configured in og-guardian (live
    2026-09-26: every evaluation raised `RuntimeError: opengrid.ledger.configure() must be called`)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._backend = PgLedgerBackend(pool)

    async def ledger_version(self) -> int:
        return await self._backend.current_version()


class PgCommitmentPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def active_kw(self, obligation_id: UUID, cycle_id: str) -> Decimal:
        _ = cycle_id  # MVP-S schema has no cycle-scoped commitment index; the latest active row applies
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ACTIVE_COMMITMENT_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
        return Decimal(str(row[0])) if row else Decimal(0)

    async def active_obligations_for_bank(self, bank_id: str, cycle_id: str) -> list[ActiveObligation]:
        _ = cycle_id  # MVP-S schema has no cycle-scoped index; "released_at IS NULL" is the active set
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ACTIVE_OBLIGATIONS_FOR_BANK_SQL, {"bank_id": bank_id})
            rows = await cur.fetchall()
        return [ActiveObligation(obligation_id=row[0], frozen_kw=Decimal(str(row[1]))) for row in rows]


class PgPriorGrantPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def prior_granted_kw(self, obligation_id: UUID) -> Decimal | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PRIOR_GRANT_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
        return Decimal(str(row[0])) if row else None


_LEASE_STATE_SELECT_SQL = "SELECT epoch, seq FROM og.lease_state WHERE bank_id = %(bank_id)s"

# Monotonic upsert (merge task, dispatch-live pass): never regresses a bank's recorded (epoch, seq) even
# under concurrent/out-of-order writers, so a delayed or replayed `record_accepted` call can never make
# G-13's freshness check MORE permissive than it already was.
_LEASE_STATE_UPSERT_SQL = """
INSERT INTO og.lease_state (bank_id, epoch, seq, updated_at)
VALUES (%(bank_id)s, %(epoch)s, %(seq)s, now())
ON CONFLICT (bank_id) DO UPDATE SET
    epoch = EXCLUDED.epoch, seq = EXCLUDED.seq, updated_at = now()
WHERE (og.lease_state.epoch, og.lease_state.seq) < (EXCLUDED.epoch, EXCLUDED.seq)
"""


class PgLeaseStatePort:
    """Durable per-bank (epoch, seq) high-water mark in `og.lease_state`
    (`migrations/0005_lease_state.sql`), so G-13 freshness survives a guardian restart instead of
    resetting every bank to (0, 0) (the prior `InMemoryLeaseStatePort`'s documented gap). A restart
    still reads back exactly what the last live guardian process last accepted, closing the "replay a
    batch right after a restart" window `InMemoryLeaseStatePort` left open (strictly more permissive of
    the first post-restart batch, not incorrect, but no longer necessary now the table exists)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def last_accepted(self, bank_id: str) -> tuple[int, int]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LEASE_STATE_SELECT_SQL, {"bank_id": bank_id})
            row = await cur.fetchone()
        return (int(row[0]), int(row[1])) if row else (0, 0)

    async def record_accepted(self, bank_id: str, epoch: int, seq: int) -> None:
        """Called only after a PASS verdict (`main.py`'s `tick()`), so a write failure here must never
        undo an already-finalized signing decision (K7) -- logged, not raised."""
        try:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_LEASE_STATE_UPSERT_SQL, {"bank_id": bank_id, "epoch": epoch, "seq": seq})
                await conn.commit()
        except Exception:
            logger.exception(
                "failed to persist lease_state; next restart may re-permit this (epoch, seq)",
                extra={"bank_id": bank_id, "epoch": epoch, "seq": seq},
            )


# The obligation's latest service profile (og.service_profile, highest version for its contract).
_SETPOINT_SOURCE_SQL = """
SELECT sp.setpoint_source
FROM og.obligation ob
JOIN LATERAL (
    SELECT setpoint_source FROM og.service_profile
    WHERE contract_id = ob.contract_id ORDER BY version DESC LIMIT 1
) sp ON true
WHERE ob.obligation_id = %(obligation_id)s
"""


class PgServiceProfilePort:
    """G-19 need basis: the guardian's own read of an obligation's service-profile setpoint source."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setpoint_source(self, obligation_id: UUID) -> str | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SETPOINT_SOURCE_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
        return str(row[0]) if row and row[0] is not None else None


_SERVICE_TYPE_SQL = "SELECT service_type FROM og.obligation WHERE obligation_id = %(obligation_id)s"

# The engine's coverage rule (migration 0020): an uncancelled deployment covering now, for this
# obligation or for every AS award (obligation_id NULL).
#: The engine's own coverage rule (engine.gateways, D-29): a deployment names its obligation, and a NULL
#: obligation_id covers every ERCOT_AS award -- never a REGULATED_CAPACITY utility toll, which is deployed
#: only by a row naming it. An unknown obligation matches nothing.
_AS_DEPLOYMENT_ACTIVE_SQL = """
SELECT EXISTS (
    SELECT 1 FROM og.as_deployment d JOIN og.obligation o ON o.obligation_id = %(obligation_id)s
    WHERE d.start_at <= now() AND d.end_at > now() AND d.cancelled_at IS NULL
      AND (d.obligation_id = o.obligation_id OR (d.obligation_id IS NULL AND o.service_type = 'ERCOT_AS'))
)
"""


class PgAsAwardPort:
    """G-19 R-GRANT-AS-HOLD: the guardian's own reads of the award's service type and its deployment."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def service_type(self, obligation_id: UUID) -> str | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_SERVICE_TYPE_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
        return str(row[0]) if row and row[0] is not None else None

    async def deployment_active(self, obligation_id: UUID) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_AS_DEPLOYMENT_ACTIVE_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
        return bool(row and row[0])


#: An open alert's condition key: the one this port stores in `detail`, else rebuilt from its scope (the
#: structured columns of migration 0031, or `detail`'s scope keys) -- so an alert raised without the stored
#: key (an older writer) is still found, deduplicated and cleared instead of staying open forever.
_ALERT_KEY_SQL = """coalesce(
    detail ->> 'condition_key',
    scope_kind || ':' || scope_ref,
    (detail ->> 'scope_kind') || ':' || (detail ->> 'scope_ref')
)"""
#: Live operator targets (the payload contract of `engine.manual`: `hub_ids`, `expires_at`), read by the
#: guardian itself. Over the same 24 h horizon the engine reads; a malformed `expires_at` fails the read,
#: which the caller treats as "no evidence" (VETO), never as a target.
_MANUAL_TARGET_HUBS_SQL = """
SELECT DISTINCT h.hub_id
FROM og.trace t
CROSS JOIN LATERAL jsonb_array_elements_text(t.payload -> 'hub_ids') AS h(hub_id)
WHERE t.event_class = 'MANUAL_TARGET' AND t.created_at > now() - interval '24 hours'
  AND (t.payload ->> 'expires_at')::timestamptz > now()
  AND h.hub_id = ANY(%(hub_ids)s)
"""


class ConfigMobileUnitPort:
    """G-35 (D-31) from SERVICES' home-station registry (`config/service_profiles/mobile_storage_home_stations
    .toml`, read by `selector.gate.load_mobile_units`, the one reader of that file). A mobile unit is a
    single-hub bank; its id is listed under `[[assignment]]`. No location or deployment-schedule source exists
    yet (the requested `og.mobile_deployment` table), so whether a unit is at its home station is UNKNOWN,
    which G-35 treats as away: a mobile unit is never charged until that source lands (fail closed)."""

    def __init__(self, mobile_ids: Iterable[str]) -> None:
        self._mobile = frozenset(mobile_ids)

    def is_mobile(self, hub_or_bank_id: str) -> bool:
        return hub_or_bank_id in self._mobile

    async def at_home_station(self, hub_id: str) -> bool | None:
        return None


class PgManualTargetPort:
    """G-19 R-OPERATOR-OVERRIDE: the guardian's own read of live MANUAL_TARGET trace events."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def manual_target_hubs(self, hub_ids: list[str]) -> set[str]:
        if not hub_ids:
            return set()
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_MANUAL_TARGET_HUBS_SQL, {"hub_ids": hub_ids})
            rows = await cur.fetchall()
        return {str(row[0]) for row in rows}


_OPEN_ALERTS_SQL = f"""
SELECT id FROM og.alert
WHERE rule = %(rule)s AND cleared_at IS NULL AND {_ALERT_KEY_SQL} = %(condition_key)s
"""  # noqa: S608 -- _ALERT_KEY_SQL is a fixed module-level literal
_OPEN_ALERT_KEYS_SQL = f"""
SELECT DISTINCT {_ALERT_KEY_SQL} FROM og.alert WHERE rule = %(rule)s AND cleared_at IS NULL
"""  # noqa: S608 -- _ALERT_KEY_SQL is a fixed module-level literal


class PgAlertPort:
    """`AlertPort` through `opengrid.health.queries` (the single og.alert writer). The condition key is
    stored in the alert detail so an open alert is raised once and cleared by its raiser."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def _open_ids(self, rule: str, condition_key: str) -> list[int]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_OPEN_ALERTS_SQL, {"rule": rule, "condition_key": condition_key})
            rows = await cur.fetchall()
        return [int(row[0]) for row in rows]

    async def raise_alert(
        self,
        rule: str,
        severity: Literal["warning", "critical"],
        summary: str,
        condition_key: str,
        detail: dict[str, object],
    ) -> None:
        from opengrid.health.model import AlertFinding
        from opengrid.health.queries import raise_alert

        if await self._open_ids(rule, condition_key):
            return
        finding = AlertFinding(
            rule=rule,
            severity=severity,
            summary=summary,
            condition_key=condition_key,
            detail={**detail, "condition_key": condition_key},
        )
        await raise_alert(self._pool, finding, opened_at=datetime.now(UTC))

    async def clear_alert(self, rule: str, condition_key: str) -> None:
        from opengrid.health.queries import clear_alert

        for alert_id in await self._open_ids(rule, condition_key):
            await clear_alert(self._pool, alert_id)

    async def open_condition_keys(self, rule: str) -> list[str]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_OPEN_ALERT_KEYS_SQL, {"rule": rule})
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows if row[0] is not None]


_UPSERT_POSTURE_SQL = """
INSERT INTO og.scope_posture (scope_kind, scope_ref, posture, veto_ratio, consecutive, stop_requested)
VALUES (%(scope_kind)s, %(scope_ref)s, %(posture)s, %(veto_ratio)s, %(consecutive)s, %(stop_requested)s)
ON CONFLICT (scope_kind, scope_ref) DO UPDATE SET
    since = CASE WHEN og.scope_posture.posture = EXCLUDED.posture THEN og.scope_posture.since ELSE now() END,
    posture = EXCLUDED.posture, veto_ratio = EXCLUDED.veto_ratio, consecutive = EXCLUDED.consecutive,
    stop_requested = EXCLUDED.stop_requested, updated_at = now()
"""

# ES06-S04: the safe stop is REQUESTED of a person -- an unconfirmed operator-action proposal for the
# normal two-step safe-stop flow. confirmed_at stays NULL; nothing here engages a stop.
_PROPOSE_SAFE_STOP_SQL = """
INSERT INTO og.operator_action (operator_action_id, operator_ref, action_kind, target_ref, tier, reason)
VALUES (%(operator_action_id)s, 'guardian:escalation', 'SAFE_STOP_ENGAGE', %(target_ref)s, 'ENGAGE', %(reason)s)
"""


class PgScopePosturePort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def set_posture(
        self,
        scope_kind: str,
        scope_ref: str,
        *,
        posture: str,
        veto_ratio: float,
        consecutive: int,
        stop_requested: bool,
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _UPSERT_POSTURE_SQL,
                {
                    "scope_kind": scope_kind,
                    "scope_ref": scope_ref,
                    "posture": posture,
                    "veto_ratio": veto_ratio,
                    "consecutive": consecutive,
                    "stop_requested": stop_requested,
                },
            )
            await conn.commit()

    async def propose_safe_stop(self, scope_kind: str, scope_ref: str, reason: str) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _PROPOSE_SAFE_STOP_SQL,
                {"operator_action_id": uuid4(), "target_ref": f"{scope_kind}:{scope_ref}", "reason": reason},
            )
            await conn.commit()


_ZONES_BY_BANK_SQL = "SELECT bank_id, zone FROM og.bank"


async def load_zones_by_bank(pool: AsyncConnectionPool) -> dict[str, str]:
    """`bank_id -> zone` (configuration). The guardian needs it for ZONE-scope safe stops (K8) and zone
    veto statistics (ES06-S04); without it a ZONE stop was never seen by the guardian."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_ZONES_BY_BANK_SQL)
        rows = await cur.fetchall()
    return {str(bank_id): str(zone) for bank_id, zone in rows}


class PgSafeStopPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def is_stopped(self, scope: SafeStopScope, scope_ref: str) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_STOP_STATE_SQL, {"scope_kind": scope, "scope_ref": scope_ref})
            row = await cur.fetchone()
        if row is None:
            return False
        return bool(row[0] == "ENGAGE")


def _trace_payload_to_proposal(payload: dict[str, Any]) -> ProposedBatch:
    items = [
        ProposedItem(
            hub_id=i["hub_id"],
            p_kw_setpoint=float(i["p_kw_setpoint"]),
            reason_code=i["reason_code"],
            obligation_id=UUID(i["obligation_id"]) if i.get("obligation_id") else None,
            obligation_granted_kw=(
                Decimal(str(i["obligation_granted_kw"]))
                if i.get("obligation_granted_kw") is not None
                else None
            ),
        )
        for i in payload["items"]
    ]
    return ProposedBatch(
        command_batch_id=UUID(payload["command_batch_id"]),
        bank_id=payload["bank_id"],
        cycle_id=payload["cycle_id"],
        epoch=int(payload["epoch"]),
        seq=int(payload["seq"]),
        issued_at=datetime.fromisoformat(payload["issued_at"]),
        expires_at=datetime.fromisoformat(payload["expires_at"]),
        ledger_version=int(payload["ledger_version"]),
        items=items,
        is_firm_event=bool(payload.get("is_firm_event", False)),
    )


class PgProposalPort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def fetch(self, command_batch_id: UUID) -> ProposedBatch | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PROPOSAL_SQL, {"command_batch_id": str(command_batch_id)})
            row = await cur.fetchone()
        if row is None:
            return None
        try:
            return _trace_payload_to_proposal(row[0])
        except (KeyError, ValueError, TypeError):
            logger.exception(
                "malformed RT_ALLOCATION trace payload", extra={"command_batch_id": str(command_batch_id)}
            )
            return None


class TraceStorePort:
    """Adapts `opengrid.trace.TraceStore` to the guardian's narrow `TracePort` (exists_preimage +
    append the verdict as its own trace event)."""

    def __init__(self, store: TraceStore, *, verdict_stream_id: str = "guardian") -> None:
        self._store = store
        self._stream_id = verdict_stream_id

    async def exists_preimage(self, decision_ref: UUID) -> bool:
        return await self._store.exists_preimage(decision_ref)

    async def append_verdict(self, batch_id: UUID, payload: dict[str, Any]) -> None:
        await self._store.append(
            self._stream_id,
            "GUARDIAN_VERDICT",
            "GUARDIAN_VERDICT",
            {**payload, "command_batch_id": str(batch_id)},
        )

    async def append_stop_release_verdict(self, operator_action_id: UUID, payload: dict[str, Any]) -> None:
        """K8 release verdicts share the guardian stream/event class; `kind='STOP_RELEASE'` +
        `operator_action_id` identify them for `PgStopReleasePort`'s claim and hand-off queries."""
        await self._store.append(
            self._stream_id,
            "GUARDIAN_VERDICT",
            "GUARDIAN_VERDICT",
            {**payload, "kind": "STOP_RELEASE", "operator_action_id": str(operator_action_id)},
        )

    async def append_calibration_verdict(self, calibration_id: UUID, payload: dict[str, Any]) -> None:
        """Same stream and event class as batch verdicts (`og.retention_policy` already covers it);
        `kind='CALIBRATION'` + `calibration_id` identify it for `PgCalibrationQueuePort`'s claim check
        and `PgCalibrationHistoryPort`'s rate limit."""
        await self._store.append(
            self._stream_id,
            "GUARDIAN_VERDICT",
            "GUARDIAN_VERDICT",
            {**payload, "kind": "CALIBRATION", "calibration_id": str(calibration_id)},
        )


# =====================================================================================================
# K12/G-20 clock-quality adapters. Every read failure, timeout, unparsable output or unsynchronised
# clock reports +inf (out of limit -> G-20 TIMEOUT, a hold). Nothing here ever reports a PASS by default.
# =====================================================================================================

#: chrony's `Leap status` values for a synchronised clock; anything else ("Not synchronised") is not.
_CHRONY_SYNCED_LEAP_STATUSES = frozenset({"Normal", "Insert second", "Delete second"})


def parse_chrony_tracking_offset_ms(output: str) -> float:
    """Signed offset (ms, + = system clock fast) from `chronyc tracking` output, or +inf unless the
    output shows a synchronised leap status AND a well-formed `System time` line."""
    leap_status: str | None = None
    offset_ms: float | None = None
    for line in output.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "Leap status":
            leap_status = value
        elif key == "System time":
            # "0.000123456 seconds fast of NTP time" / "... slow of NTP time"
            parts = value.split()
            if len(parts) < 3 or parts[1] != "seconds" or parts[2] not in ("fast", "slow"):
                return math.inf
            try:
                seconds = float(parts[0])
            except ValueError:
                return math.inf
            offset_ms = seconds * 1000.0 * (1.0 if parts[2] == "fast" else -1.0)
    if leap_status not in _CHRONY_SYNCED_LEAP_STATUSES or offset_ms is None or not math.isfinite(offset_ms):
        return math.inf
    return offset_ms


class _CachedOffset:
    """Serialises and briefly caches one clock-quality read, so a tick evaluating many batches costs one
    read. A failed read is cached for the same short window (it is `inf`: a hold either way)."""

    def __init__(self, ttl_s: float, monotonic_fn: Callable[[], float]) -> None:
        self._ttl_s = ttl_s
        self._monotonic = monotonic_fn
        self._lock = asyncio.Lock()
        self._value: float | None = None
        self._read_at = 0.0

    async def get(self, read: Callable[[], Awaitable[float]]) -> float:
        async with self._lock:
            now = self._monotonic()
            if self._value is not None and now - self._read_at < self._ttl_s:
                return self._value
            try:
                value = float(await read())
            except Exception:
                logger.exception("guardian clock-quality read failed (K12): reporting out of limit")
                value = math.inf
            if math.isnan(value):
                value = math.inf
            self._value, self._read_at = value, now
            return value


class ChronyClockPort:
    """K12/G-20 offset from `chronyc tracking` on the guardian host (for hosts running chronyd). A
    missing binary, non-zero exit, timeout, unparsable output or a leap status other than synchronised
    all report +inf -- fail closed, never 0.0 ms."""

    def __init__(
        self,
        *,
        timeout_s: float = 1.0,
        cache_s: float = DEFAULT_CLOCK_CACHE_S,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._timeout_s = timeout_s
        self._cache = _CachedOffset(cache_s, monotonic_fn)

    async def offset_from_ntp_ms(self) -> float:
        return await self._cache.get(self._read)

    async def _read(self) -> float:
        try:
            proc = await asyncio.create_subprocess_exec(
                "chronyc",
                "tracking",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except OSError:
            logger.error("chronyc unavailable; G-20 reports the clock out of limit (K12)")
            return math.inf
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self._timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            logger.error("chronyc timed out; G-20 reports the clock out of limit (K12)")
            return math.inf
        if proc.returncode != 0:
            return math.inf
        return parse_chrony_tracking_offset_ms(stdout.decode("ascii", errors="replace"))


#: Linux `adjtimex(2)` constants (include/uapi/linux/timex.h).
_TIME_ERROR = 5
_STA_UNSYNC = 0x0040
_STA_NANO = 0x2000
#: The kernel grows `maxerror` by 500 ppm between daemon updates and flags the clock unsynchronised only
#: at 16 s. A bound this large means the NTP daemon has stopped disciplining the clock (with ntpd's
#: 1024 s poll the live host peaks near 0.5 s), so it is treated as unsynchronised long before that.
KERNEL_MAXERROR_UNSYNC_MS_DEFAULT = 2_000.0


@dataclass(frozen=True, slots=True)
class KernelTimex:
    """The read-only subset of `struct timex` G-20 needs (`adjtimex` with modes=0, no privilege)."""

    state: int  # adjtimex() return value; TIME_ERROR (5) = clock not synchronised
    status: int
    offset: int  # ns when STA_NANO is set, else us
    esterror_us: int
    maxerror_us: int


def kernel_clock_offset_ms(
    timex: KernelTimex, *, maxerror_unsync_ms: float = KERNEL_MAXERROR_UNSYNC_MS_DEFAULT
) -> float:
    """Signed clock-quality bound in ms: the kernel PLL's current offset plus its estimated error, or
    +inf when the kernel reports the clock unsynchronised or its max-error bound shows the discipline
    daemon has stopped updating it. Daemon-agnostic: ntpd, chronyd and timesyncd all feed it."""
    if timex.state == _TIME_ERROR or timex.status & _STA_UNSYNC:
        return math.inf
    if timex.maxerror_us / 1000.0 > maxerror_unsync_ms:
        return math.inf
    offset_ms = timex.offset / (1_000_000.0 if timex.status & _STA_NANO else 1_000.0)
    return math.copysign(abs(offset_ms) + max(timex.esterror_us, 0) / 1000.0, offset_ms)


class _Timeval(ctypes.Structure):
    _fields_: ClassVar[list[tuple[str, Any]]] = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]


class _Timex(ctypes.Structure):
    """glibc `struct timex` (sys/timex.h), including its 11 trailing reserved ints."""

    _fields_: ClassVar[list[tuple[str, Any]]] = [
        ("modes", ctypes.c_uint),
        ("offset", ctypes.c_long),
        ("freq", ctypes.c_long),
        ("maxerror", ctypes.c_long),
        ("esterror", ctypes.c_long),
        ("status", ctypes.c_int),
        ("constant", ctypes.c_long),
        ("precision", ctypes.c_long),
        ("tolerance", ctypes.c_long),
        ("time", _Timeval),
        ("tick", ctypes.c_long),
        ("ppsfreq", ctypes.c_long),
        ("jitter", ctypes.c_long),
        ("shift", ctypes.c_int),
        ("stabil", ctypes.c_long),
        ("jitcnt", ctypes.c_long),
        ("calcnt", ctypes.c_long),
        ("errcnt", ctypes.c_long),
        ("stbcnt", ctypes.c_long),
        ("tai", ctypes.c_int),
        ("_reserved", ctypes.c_int * 11),
    ]


def read_kernel_timex() -> KernelTimex:
    """Read-only `adjtimex(2)` (modes=0). Raises `OSError` where it is unavailable (non-Linux)."""
    libc_name = ctypes.util.find_library("c")
    if libc_name is None or not sys.platform.startswith("linux"):
        raise OSError("adjtimex is only available on Linux")
    libc = ctypes.CDLL(libc_name, use_errno=True)
    timex = _Timex()
    timex.modes = 0
    state = int(libc.adjtimex(ctypes.byref(timex)))
    if state < 0:
        raise OSError(ctypes.get_errno(), "adjtimex failed")
    return KernelTimex(
        state=state,
        status=int(timex.status),
        offset=int(timex.offset),
        esterror_us=int(timex.esterror),
        maxerror_us=int(timex.maxerror),
    )


class KernelClockPort:
    """K12/G-20 from the kernel's NTP discipline state (`adjtimex`), whichever daemon disciplines it --
    the live host runs ntpd, which `ChronyClockPort` cannot read. Any read failure reports +inf."""

    def __init__(
        self,
        *,
        read_timex: Callable[[], KernelTimex] = read_kernel_timex,
        maxerror_unsync_ms: float = KERNEL_MAXERROR_UNSYNC_MS_DEFAULT,
        cache_s: float = DEFAULT_CLOCK_CACHE_S,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read_timex = read_timex
        self._maxerror_unsync_ms = maxerror_unsync_ms
        self._cache = _CachedOffset(cache_s, monotonic_fn)

    async def offset_from_ntp_ms(self) -> float:
        return await self._cache.get(self._read)

    async def _read(self) -> float:
        return kernel_clock_offset_ms(self._read_timex(), maxerror_unsync_ms=self._maxerror_unsync_ms)


def build_clock_port(source: ClockSource, *, cache_s: float = DEFAULT_CLOCK_CACHE_S) -> ClockPort:
    """`guardian.clock_source` -> the K12 adapter (`GuardianConfig` has already rejected any other value)."""
    if source == "chrony":
        return ChronyClockPort(cache_s=cache_s)
    return KernelClockPort(cache_s=cache_s)


# =====================================================================================================
# S6.7 calibration hand-off: the ladder (`opengrid.assets`) records a PENDING og.calibration_attempt row;
# the guardian polls it, evaluates G-20/G-25 and signs. Its og.calibration_command row (the atomic claim,
# signed or refused; pq_repo.PgCalibrationLedgerPort) excludes the attempt from this queue, so each attempt is
# decided at most once and never re-signed, even by a concurrent evaluator.
# =====================================================================================================

_PENDING_CALIBRATIONS_SQL = """
SELECT ca.calibration_id, ca.hub_id, ca.reference_phase_deg, ca.reference_freq_hz, ca.reference_amplitude_v,
       ca.correction_freq_hz, ca.correction_voltage_pct, ca.correction_phase_deg, ca.requested_at
FROM og.calibration_attempt ca
WHERE ca.outcome = 'PENDING' AND ca.command_batch_id IS NULL
  AND ca.requested_at > now() - make_interval(secs => %(max_age_s)s)
  AND NOT EXISTS (SELECT 1 FROM og.calibration_command c WHERE c.calibration_id = ca.calibration_id)
ORDER BY ca.requested_at
LIMIT %(limit)s
"""


@dataclass(frozen=True, slots=True)
class PendingCalibration:
    """One PENDING, not-yet-evaluated `og.calibration_attempt` row (the ladder's candidate)."""

    calibration_id: UUID
    hub_id: str
    reference_phase_deg: float
    reference_freq_hz: float
    reference_amplitude_v: float
    correction: OffsetVector
    requested_at: datetime


class PgCalibrationQueuePort:
    def __init__(self, pool: AsyncConnectionPool, *, limit: int = 20) -> None:
        self._pool = pool
        self._limit = limit

    async def pending(self, *, max_age_s: float) -> list[PendingCalibration]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PENDING_CALIBRATIONS_SQL, {"max_age_s": max_age_s, "limit": self._limit})
            rows = await cur.fetchall()
        return [
            PendingCalibration(
                calibration_id=row[0],
                hub_id=row[1],
                reference_phase_deg=float(row[2]),
                reference_freq_hz=float(row[3]),
                reference_amplitude_v=float(row[4]),
                # A NULL correction axis is "no correction on that axis", never an unbounded one.
                correction=OffsetVector(
                    freq_hz=float(row[5] or 0.0),
                    voltage_pct=float(row[6] or 0.0),
                    phase_deg=float(row[7] or 0.0),
                ),
                requested_at=row[8],
            )
            for row in rows
        ]


# =====================================================================================================
# K8 stop RELEASE hand-off (see `opengrid.guardian.stop_release` for the end-to-end path).
# =====================================================================================================

_PENDING_RELEASE_REQUESTS_SQL = """
SELECT oa.operator_action_id, oa.operator_ref, oa.approver_ref, oa.target_ref, oa.reason, oa.created_at,
       oa.confirmed_at, oa.trace_id
FROM og.operator_action oa
WHERE oa.action_kind = 'SAFE_STOP_RELEASE' AND oa.tier = 'TIER2' AND oa.confirmed_at IS NOT NULL
  AND oa.confirmed_at > now() - make_interval(secs => %(max_age_s)s)
  AND NOT EXISTS (
      SELECT 1 FROM og.trace t
      WHERE t.event_class = 'GUARDIAN_VERDICT'
        AND t.created_at > now() - make_interval(secs => %(max_age_s)s) - interval '1 minute'
        AND t.payload ->> 'kind' = 'STOP_RELEASE'
        AND t.payload ->> 'operator_action_id' = oa.operator_action_id::text
  )
ORDER BY oa.confirmed_at
LIMIT 20
"""

_OUTSTANDING_ENGAGES_SQL = """
SELECT e.stop_event_id, e.initiator_kind, e.created_at
FROM og.stop_event e
WHERE e.scope_kind = %(scope_kind)s AND e.scope_ref = %(scope_ref)s AND e.action = 'ENGAGE'
  AND e.created_at > COALESCE(
      (SELECT max(r.created_at) FROM og.stop_event r
       WHERE r.scope_kind = %(scope_kind)s AND r.scope_ref = %(scope_ref)s AND r.action = 'RELEASE'),
      '-infinity'::timestamptz)
ORDER BY e.created_at
"""

_BANKS_SQL = {
    "BANK": "SELECT bank_id FROM og.bank WHERE bank_id = %(scope_ref)s",
    "ZONE": "SELECT bank_id FROM og.bank WHERE zone = %(scope_ref)s ORDER BY bank_id",
    "FLEET": "SELECT bank_id FROM og.bank ORDER BY bank_id",
}

# Signed RELEASE events og-safestop has not yet published: publication is recorded as an og.stop_event
# RELEASE row carrying the event's own signature.
_UNPUBLISHED_RELEASES_SQL = """
SELECT ev
FROM og.trace t
CROSS JOIN LATERAL jsonb_array_elements(t.payload -> 'events') AS ev
WHERE t.event_class = 'GUARDIAN_VERDICT'
  AND t.created_at > now() - make_interval(secs => %(max_age_s)s)
  AND t.payload ->> 'kind' = 'STOP_RELEASE' AND t.payload ->> 'outcome' = 'SIGNED'
  AND NOT EXISTS (SELECT 1 FROM og.stop_event s WHERE s.signature = ev ->> 'signature')
ORDER BY t.created_at
"""

_SCOPE_KINDS: dict[str, StopScopeKind] = {"FLEET": "FLEET", "ZONE": "ZONE", "BANK": "BANK"}


def parse_release_target(target_ref: str | None) -> tuple[StopScopeKind, str] | None:
    """og-api's `target_ref` `<SCOPE_KIND>:<scope_ref>` -> (scope kind, scope ref as og.stop_event stores
    it: "FLEET" for the fleet). Anything else is `None` (the request is ignored, never guessed at)."""
    kind, sep, ref = (target_ref or "").partition(":")
    scope_kind = _SCOPE_KINDS.get(kind.strip().upper())
    if not sep or scope_kind is None:
        return None
    if scope_kind == "FLEET":
        return scope_kind, "FLEET"
    ref = ref.strip()
    return (scope_kind, ref) if ref else None


class PgStopReleasePort:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def pending_requests(self, *, max_age_s: float) -> list[ReleaseRequest]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_PENDING_RELEASE_REQUESTS_SQL, {"max_age_s": max_age_s})
            rows = await cur.fetchall()
        requests: list[ReleaseRequest] = []
        for (
            action_id,
            operator_ref,
            approver_ref,
            target_ref,
            reason,
            created_at,
            confirmed_at,
            trace_id,
        ) in rows:
            target = parse_release_target(target_ref)
            if target is None:
                logger.warning(
                    "ignoring release request with an unparsable target", extra={"target": target_ref}
                )
                continue
            requests.append(
                ReleaseRequest(
                    operator_action_id=action_id,
                    requested_by=str(operator_ref),
                    approved_by=approver_ref,
                    scope_kind=target[0],
                    scope_ref=target[1],
                    reason=str(reason or "operator release"),
                    requested_at=created_at,
                    approved_at=confirmed_at,
                    trace_id=trace_id,
                )
            )
        return requests

    async def outstanding_engages(self, scope_kind: StopScopeKind, scope_ref: str) -> list[EngagedStop]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_OUTSTANDING_ENGAGES_SQL, {"scope_kind": scope_kind, "scope_ref": scope_ref})
            rows = await cur.fetchall()
        return [EngagedStop(stop_id=row[0], initiator_kind=str(row[1]), engaged_at=row[2]) for row in rows]

    async def banks_in_scope(self, scope_kind: StopScopeKind, scope_ref: str) -> list[str]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_BANKS_SQL[scope_kind], {"scope_ref": scope_ref})
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    async def unpublished_release_events(self, *, max_age_s: float) -> list[dict[str, Any]]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_UNPUBLISHED_RELEASES_SQL, {"max_age_s": max_age_s})
            rows = await cur.fetchall()
        return [dict(row[0]) for row in rows]

    async def hand_to_safestop(self, event: dict[str, Any]) -> None:
        """NOTIFY og-safestop's request channel with one signed RELEASE to publish. og-safestop verifies
        the guardian signature itself before publishing anything."""
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT pg_notify(%(channel)s, %(payload)s)",
                {
                    "channel": REQUEST_CHANNEL,
                    "payload": json.dumps({"action": "PUBLISH_RELEASE", "event": event}),
                },
            )
            await conn.commit()


_HUB_BANKS_SQL = "SELECT hub_id, bank_id FROM og.hub"


async def load_bank_membership(pool: AsyncConnectionPool) -> dict[str, str]:
    """`hub_id -> bank_id` from `og.hub` (configuration, like `load_hub_params`), so the guardian's own
    telemetry cache can answer "every hub on this bank" (`MqttHubStatePort.member_snapshots`)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_HUB_BANKS_SQL)
        rows = await cur.fetchall()
    return {hub_id: bank_id for hub_id, bank_id in rows}


def build_pg_ports(
    pool: AsyncConnectionPool,
    trace_store: TraceStore,
    hubs: HubStatePort,
    *,
    clock: ClockPort,
    l2_instructions: L2InstructionPort,
    bank_members: BankMembersPort | None = None,
    stop_release: StopReleasePort | None = None,
    alerts: AlertPort | None = None,
    zones_by_bank: dict[str, str] | None = None,
    topology: GridTopologyPort | None = None,
    territory: TerritoryPort | None = None,
) -> tuple[GuardianPorts, PgLeaseStatePort]:
    """Convenience wiring for `main.py`: constructs every Postgres-backed port plus the durable lease
    tracker (returned separately so `main.py` can `await record_accepted(...)` after a PASS verdict).

    GUARD-02/04: `hubs` is a required argument, not a Postgres default -- guardian's hub-state read must
    always be its OWN telemetry (`opengrid.guardian.mqtt_io.MqttHubStatePort`), never `og.hub_state`
    (the row the engine/fleet processes maintain). There is deliberately no `PgHubStatePort` in this
    module for a caller to reach for by mistake. `clock` (K12) and `l2_instructions` (K5, the guardian's
    own MQTT subscription to utility instructions) are required for the same reason: no silent default.
    """
    leases = PgLeaseStatePort(pool)
    ports = GuardianPorts(
        clock=clock,
        proposals=PgProposalPort(pool),
        trace=TraceStorePort(trace_store),
        hubs=hubs,
        banks=PgBankStatePort(pool),
        ledger=PgLedgerVersionPort(pool),
        commitments=PgCommitmentPort(pool),
        prior_grants=PgPriorGrantPort(pool),
        leases=leases,
        l2_instructions=l2_instructions,
        safe_stop=PgSafeStopPort(pool),
        zones_by_bank=dict(zones_by_bank or {}),
        bank_members=bank_members,
        stop_release=stop_release,
        alerts=alerts,
        service_profiles=PgServiceProfilePort(pool),
        as_awards=PgAsAwardPort(pool),
        topology=topology,
        territory=territory,
        manual_targets=PgManualTargetPort(pool),
        pq=PqPorts(
            envelopes=PgPqEnvelopeStatePort(pool),
            measurements=PgPqMeasurementPort(pool),
            hub_assets=PgHubAssetStatePort(pool),
            calibration_history=PgCalibrationHistoryPort(pool),
            firmware_bounds=StaticFirmwareCalibrationBoundsPort(),
            sensitive_grants=PgSensitiveGrantPort(pool),
            calibration_ledger=PgCalibrationLedgerPort(pool),
        ),
    )
    return ports, leases
