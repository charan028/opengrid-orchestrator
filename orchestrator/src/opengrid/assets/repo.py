"""Postgres-backed implementations of `opengrid.assets.ports` (migration `0011_asset_health.sql`,
owned by Agent A -- this module only reads/writes those tables). Kept separate from `service.py` so the
orchestration logic has no psycopg import (BUILD.md S5a), mirroring `opengrid.guardian.repo`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID

from psycopg.types.json import Json
from psycopg_pool import AsyncConnectionPool

from opengrid import pq_ingest
from opengrid.assets.calibration_ack import IssuedCalibration
from opengrid.assets.ports import DriftObservationWindow, HubAssetRecord, PendingCalibrationAttempt
from opengrid.core.models.pq import MaintenanceWorkOrder
from opengrid.core.pq import CalibrationOutcome, OffsetVector, exceeds_watch_threshold
from opengrid.core.pq.constants import (
    DRIFT_FLOOR_FREQ_HZ,
    DRIFT_FLOOR_PHASE_DEG,
    DRIFT_FLOOR_THD_PCT,
    DRIFT_FLOOR_VOLTAGE_PCT,
    DRIFT_OBSERVATION_WINDOW_S_DEFAULT,
    NOMINAL_FREQ_HZ,
)
from opengrid.core.services import DATA_CENTER_SERVICE_TYPE, PIPELINE_AC_SERVICE_TYPE
from opengrid.guardian.pq_ports import AssetState
from opengrid.pq_ingest import aggregation as pq_ingest_aggregation
from opengrid.trace import TraceStore

logger = logging.getLogger(__name__)

_HUB_ASSET_LIST_IDS_SQL = "SELECT hub_id FROM og.hub_inverter_pq ORDER BY hub_id"

_HUB_ASSET_SELECT_SQL = """
SELECT asset_state, asset_state_since, consecutive_correctable_drifts, last_recalibration_at,
       ride_through_class
FROM og.hub_inverter_pq WHERE hub_id = %(hub_id)s
"""

_HUB_ASSET_SET_STATE_SQL = """
UPDATE og.hub_inverter_pq
SET asset_state = %(asset_state)s, asset_state_since = %(since)s,
    consecutive_correctable_drifts = %(consecutive)s
WHERE hub_id = %(hub_id)s
"""

_HUB_ASSET_RECALIBRATION_SQL = """
UPDATE og.hub_inverter_pq SET last_recalibration_at = %(at)s WHERE hub_id = %(hub_id)s
"""

_HUB_ASSET_RESET_CHARACTERIZATION_SQL = """
UPDATE og.hub_inverter_pq SET last_estimated_at = %(at)s WHERE hub_id = %(hub_id)s
"""


class PgAssetHealthRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def list_hub_ids(self) -> list[str]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HUB_ASSET_LIST_IDS_SQL)
            rows = await cur.fetchall()
        return [row[0] for row in rows]

    async def get(self, hub_id: str) -> HubAssetRecord | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HUB_ASSET_SELECT_SQL, {"hub_id": hub_id})
            row = await cur.fetchone()
        if row is None:
            return None
        asset_state, since, consecutive, last_recalibration_at, ride_through_class = row
        return HubAssetRecord(
            hub_id=hub_id,
            asset_state=asset_state,
            asset_state_since=since,
            consecutive_correctable_drifts=consecutive,
            last_recalibration_at=last_recalibration_at,
            ride_through_class=ride_through_class,
        )

    async def set_state(
        self, hub_id: str, state: AssetState, *, since: datetime, consecutive_correctable_drifts: int
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _HUB_ASSET_SET_STATE_SQL,
                {
                    "hub_id": hub_id,
                    "asset_state": state,
                    "since": since,
                    "consecutive": consecutive_correctable_drifts,
                },
            )
            await conn.commit()

    async def record_recalibration(self, hub_id: str, *, at: datetime) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HUB_ASSET_RECALIBRATION_SQL, {"hub_id": hub_id, "at": at})
            await conn.commit()

    async def reset_characterization(self, hub_id: str, *, last_estimated_at: datetime) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _HUB_ASSET_RESET_CHARACTERIZATION_SQL, {"hub_id": hub_id, "at": last_estimated_at}
            )
            await conn.commit()


_LAST_ATTEMPT_SQL = """
SELECT extract(epoch FROM requested_at) FROM og.calibration_attempt
WHERE hub_id = %(hub_id)s ORDER BY requested_at DESC LIMIT 1
"""

_LAST_CORRECTED_SQL = """
SELECT verified_at FROM og.calibration_attempt
WHERE hub_id = %(hub_id)s AND outcome = 'CORRECTED' ORDER BY verified_at DESC LIMIT 1
"""

_INSERT_ATTEMPT_SQL = """
INSERT INTO og.calibration_attempt (
    calibration_id, hub_id, requested_at, reference_phase_deg, reference_freq_hz, reference_amplitude_v,
    measured_offset_freq_hz, measured_offset_voltage_pct, measured_offset_phase_deg,
    correction_freq_hz, correction_voltage_pct, correction_phase_deg, command_batch_id
) VALUES (
    %(calibration_id)s, %(hub_id)s, %(requested_at)s, %(reference_phase_deg)s, %(reference_freq_hz)s,
    %(reference_amplitude_v)s, %(measured_offset_freq_hz)s, %(measured_offset_voltage_pct)s,
    %(measured_offset_phase_deg)s, %(correction_freq_hz)s, %(correction_voltage_pct)s,
    %(correction_phase_deg)s, %(command_batch_id)s
)
"""

_UPDATE_OUTCOME_SQL = """
UPDATE og.calibration_attempt SET outcome = %(outcome)s, verified_at = %(verified_at)s
WHERE calibration_id = %(calibration_id)s
"""

_GET_PENDING_SQL = """
SELECT hub_id, measured_offset_freq_hz, measured_offset_voltage_pct, measured_offset_phase_deg
FROM og.calibration_attempt WHERE calibration_id = %(calibration_id)s AND outcome = 'PENDING'
"""

# `og.calibration_command` (migration 0016, written by og-guardian): the command actually issued for an
# attempt, and its single-use ack marker.
_ISSUED_COMMAND_SQL = """
SELECT hub_id, epoch, seq, ack_consumed_at IS NOT NULL
FROM og.calibration_command WHERE calibration_id = %(calibration_id)s AND status = 'SIGNED'
"""

_CONSUME_ACK_SQL = """
UPDATE og.calibration_command SET ack_consumed_at = now()
WHERE calibration_id = %(calibration_id)s AND status = 'SIGNED' AND ack_consumed_at IS NULL
RETURNING calibration_id
"""

_CLOSE_PROTOCOL_ERROR_SQL = """
UPDATE og.calibration_attempt SET outcome = 'FAILED_NO_ACK', verified_at = %(at)s
WHERE calibration_id = %(calibration_id)s AND outcome = 'PENDING'
"""

_OPEN_ALERT_SQL = "SELECT 1 FROM og.alert WHERE rule = %(rule)s AND cleared_at IS NULL LIMIT 1"


class PgCalibrationAckGuard:
    """`opengrid.assets.calibration_ack.CalibrationAckGuard` over `og.calibration_command`. `hub_public_keys`
    (hub_id -> 32-byte Ed25519 key) verifies hub-signed acks; with no key for a hub, a SIGNED ack from it
    is refused (unsigned acks are still accepted, bound by hub, id and (epoch, seq))."""

    def __init__(self, pool: AsyncConnectionPool, *, hub_public_keys: dict[str, bytes] | None = None) -> None:
        self._pool = pool
        self._hub_keys = dict(hub_public_keys or {})

    async def issued(self, calibration_id: UUID) -> IssuedCalibration | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ISSUED_COMMAND_SQL, {"calibration_id": calibration_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return IssuedCalibration(
            hub_id=str(row[0]), epoch=int(row[1]), seq=int(row[2]), ack_consumed=bool(row[3])
        )

    async def consume(self, calibration_id: UUID) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_CONSUME_ACK_SQL, {"calibration_id": calibration_id})
            row = await cur.fetchone()
            await conn.commit()
        return row is not None

    async def close_protocol_error(self, calibration_id: UUID, *, at: datetime) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_CLOSE_PROTOCOL_ERROR_SQL, {"calibration_id": calibration_id, "at": at})
            await conn.commit()

    async def raise_alert(self, rule: str, summary: str, detail: dict[str, object]) -> None:
        from opengrid.health.model import AlertFinding
        from opengrid.health.queries import raise_alert

        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_OPEN_ALERT_SQL, {"rule": rule})
            if await cur.fetchone() is not None:
                return
        finding = AlertFinding(
            rule=rule, severity="warning", summary=summary, condition_key=rule, detail=dict(detail)
        )
        await raise_alert(self._pool, finding, opened_at=datetime.now(UTC))

    def hub_public_key(self, hub_id: str) -> bytes | None:
        return self._hub_keys.get(hub_id)


class PgCalibrationAttemptRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def last_attempt_epoch_s(self, hub_id: str) -> float | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LAST_ATTEMPT_SQL, {"hub_id": hub_id})
            row = await cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None

    async def last_corrected_at(self, hub_id: str) -> datetime | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LAST_CORRECTED_SQL, {"hub_id": hub_id})
            row = await cur.fetchone()
        return row[0] if row else None

    async def record_attempt(
        self,
        *,
        calibration_id: UUID,
        hub_id: str,
        requested_at: datetime,
        reference_phase_deg: float,
        reference_freq_hz: float,
        reference_amplitude_v: float,
        measured_offset: OffsetVector,
        correction: OffsetVector,
        command_batch_id: UUID | None,
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_ATTEMPT_SQL,
                {
                    "calibration_id": calibration_id,
                    "hub_id": hub_id,
                    "requested_at": requested_at,
                    "reference_phase_deg": Decimal(str(reference_phase_deg)),
                    "reference_freq_hz": Decimal(str(reference_freq_hz)),
                    "reference_amplitude_v": Decimal(str(reference_amplitude_v)),
                    "measured_offset_freq_hz": Decimal(str(measured_offset.freq_hz)),
                    "measured_offset_voltage_pct": Decimal(str(measured_offset.voltage_pct)),
                    "measured_offset_phase_deg": Decimal(str(measured_offset.phase_deg)),
                    "correction_freq_hz": Decimal(str(correction.freq_hz)),
                    "correction_voltage_pct": Decimal(str(correction.voltage_pct)),
                    "correction_phase_deg": Decimal(str(correction.phase_deg)),
                    "command_batch_id": command_batch_id,
                },
            )
            await conn.commit()

    async def get_pending(self, calibration_id: UUID) -> PendingCalibrationAttempt | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_GET_PENDING_SQL, {"calibration_id": calibration_id})
            row = await cur.fetchone()
        if row is None:
            return None
        hub_id, freq_hz, voltage_pct, phase_deg = row
        return PendingCalibrationAttempt(
            hub_id=hub_id,
            measured_offset=OffsetVector(
                freq_hz=float(freq_hz or 0),
                voltage_pct=float(voltage_pct or 0),
                phase_deg=float(phase_deg or 0),
            ),
        )

    async def record_outcome(
        self, calibration_id: UUID, outcome: CalibrationOutcome, *, verified_at: datetime
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _UPDATE_OUTCOME_SQL,
                {"calibration_id": calibration_id, "outcome": outcome.value, "verified_at": verified_at},
            )
            await conn.commit()


_OPEN_WORK_ORDER_SQL = """
INSERT INTO og.maintenance_work_order (work_order_id, hub_id, severity, evidence, opened_at)
VALUES (gen_random_uuid(), %(hub_id)s, %(severity)s, %(evidence)s, %(opened_at)s)
RETURNING work_order_id
"""

_CLOSE_WORK_ORDER_SQL = """
UPDATE og.maintenance_work_order
SET status = %(status)s, closed_at = %(closed_at)s, technician_notes = %(technician_notes)s
WHERE work_order_id = %(work_order_id)s
"""

_OPEN_FOR_HUB_SQL = """
SELECT work_order_id, hub_id, severity, evidence, status, opened_at, closed_at, technician_notes
FROM og.maintenance_work_order
WHERE hub_id = %(hub_id)s AND status IN ('OPEN', 'IN_PROGRESS')
ORDER BY opened_at DESC LIMIT 1
"""


class PgWorkOrderRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def open(
        self, hub_id: str, *, severity: str, evidence: dict[str, object], opened_at: datetime
    ) -> UUID:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _OPEN_WORK_ORDER_SQL,
                {"hub_id": hub_id, "severity": severity, "evidence": Json(evidence), "opened_at": opened_at},
            )
            row = await cur.fetchone()
            await conn.commit()
        assert row is not None  # noqa: S101 -- RETURNING always yields exactly one row on INSERT
        return UUID(str(row[0]))

    async def close(
        self,
        work_order_id: UUID,
        *,
        closed_at: datetime,
        technician_notes: str | None,
        status: str = "CLOSED",
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _CLOSE_WORK_ORDER_SQL,
                {
                    "work_order_id": work_order_id,
                    "closed_at": closed_at,
                    "technician_notes": technician_notes,
                    "status": status,
                },
            )
            await conn.commit()

    async def open_for_hub(self, hub_id: str) -> MaintenanceWorkOrder | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_OPEN_FOR_HUB_SQL, {"hub_id": hub_id})
            row = await cur.fetchone()
        if row is None:
            return None
        work_order_id, hub, severity, evidence, status, opened_at, closed_at, notes = row
        return MaintenanceWorkOrder(
            work_order_id=work_order_id,
            hub_id=hub,
            severity=severity,
            evidence=evidence,
            status=status,
            opened_at=opened_at,
            closed_at=closed_at,
            technician_notes=notes,
        )


_INSERT_STATE_TRANSITION_SQL = """
INSERT INTO og.asset_event (asset_event_id, hub_id, event_type, from_state, to_state, reason_code, occurred_at)
VALUES (gen_random_uuid(), %(hub_id)s, 'STATE_TRANSITION', %(from_state)s, %(to_state)s, %(reason_code)s, %(at)s)
"""

_INSERT_INVERTER_REPLACED_SQL = """
INSERT INTO og.asset_event (
    asset_event_id, hub_id, work_order_id, event_type,
    old_inverter_serial, new_inverter_serial, old_firmware, new_firmware, occurred_at
) VALUES (
    gen_random_uuid(), %(hub_id)s, %(work_order_id)s, 'INVERTER_REPLACED',
    %(old_serial)s, %(new_serial)s, %(old_firmware)s, %(new_firmware)s, %(at)s
)
"""

_INSERT_RECOMMISSIONED_SQL = """
INSERT INTO og.asset_event (asset_event_id, hub_id, event_type, occurred_at)
VALUES (gen_random_uuid(), %(hub_id)s, 'RECOMMISSIONED', %(at)s)
"""


class PgAssetEventRepo:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def record_state_transition(
        self, hub_id: str, *, from_state: AssetState, to_state: AssetState, reason_code: str, at: datetime
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_STATE_TRANSITION_SQL,
                {
                    "hub_id": hub_id,
                    "from_state": from_state,
                    "to_state": to_state,
                    "reason_code": reason_code,
                    "at": at,
                },
            )
            await conn.commit()

    async def record_inverter_replaced(
        self,
        hub_id: str,
        *,
        work_order_id: UUID | None,
        old_serial: str | None,
        new_serial: str,
        old_firmware: str | None,
        new_firmware: str,
        at: datetime,
    ) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _INSERT_INVERTER_REPLACED_SQL,
                {
                    "hub_id": hub_id,
                    "work_order_id": work_order_id,
                    "old_serial": old_serial,
                    "new_serial": new_serial,
                    "old_firmware": old_firmware,
                    "new_firmware": new_firmware,
                    "at": at,
                },
            )
            await conn.commit()

    async def record_recommissioned(self, hub_id: str, *, at: datetime) -> None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_INSERT_RECOMMISSIONED_SQL, {"hub_id": hub_id, "at": at})
            await conn.commit()


_CHARACTERIZATION_SQL = """
SELECT freq_offset_hz, freq_offset_std_hz, voltage_offset_pct, voltage_offset_std_pct,
       thd_current_pct, phase_angle_error_deg
FROM og.hub_inverter_pq WHERE hub_id = %(hub_id)s
"""


#: Reuses Agent G's own nominal-voltage constant (`opengrid.pq_ingest.aggregation`, the same one
#: `bank_measurement` expresses voltage deviation against) rather than a second copy (BUILD.md S1).
_NOMINAL_VOLTAGE_V = pq_ingest_aggregation.NOMINAL_VOLTAGE_V


class PgDriftObservationRepo:
    """S5.5.1's rolling observation window, from measured PQ summaries compared against the hub's own
    characterized values (`og.hub_inverter_pq`). Reads summaries through `opengrid.pq_ingest.
    latest_summaries` (Agent G's own read-through, S6.5 step 4's "kept here rather than duplicated in
    each caller") rather than querying `og.pq_waveform_summary` directly -- this package owns no SQL
    against that table (BUILD.md S1: one owner per function).

    NOTE (reported, not silently assumed): S5.5.1's "correlates with a fleet-wide/bank-wide event
    already explained by a trace entry" transient classification needs the bank/feeder-wide event log
    Agent G's waveform-ingestion service and the existing `03 S8.6` R26 frequency-freeze alerting own --
    this adapter conservatively reports `correlates_with_fleet_event=False` (never hides a real drift
    behind an unconfirmed correlation) until that event feed exists; wire it in once Agent G's ingestion
    service surfaces one."""

    def __init__(
        self, pool: AsyncConnectionPool, *, window_s: float = DRIFT_OBSERVATION_WINDOW_S_DEFAULT
    ) -> None:
        self._pool = pool
        self._window_s = window_s

    async def observation_window(self, hub_id: str) -> DriftObservationWindow | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_CHARACTERIZATION_SQL, {"hub_id": hub_id})
            char_row = await cur.fetchone()
        if char_row is None:
            return None
        freq_offset_std_hz, voltage_offset_std_pct, thd_current_pct = (
            float(char_row[1]),
            float(char_row[3]),
            float(char_row[4]),
        )
        rows = await pq_ingest.latest_summaries([hub_id], since=_window_start(self._window_s))
        if not rows:
            return None
        # LIVE BUG FIX (R2, 2026-09-26): `pq_ingest.latest_summaries`/`PgPqIngestBackend.latest_
        # summaries` return rows NEWEST FIRST (`ORDER BY hub_id, ts DESC`, pg_backend.py's own
        # `_LATEST_SUMMARIES_SQL`). This method used to read `rows[-1]` for "the latest measured
        # offset" -- the OLDEST row in the window, from before any real drift -- feeding a stale,
        # near-zero pre-calibration offset into `request_calibration`'s correction math (hub-01996's
        # 0.017 Hz vs. its real 0.2 Hz). Sorting explicitly by `ts` here, once, makes every downstream
        # use of `rows` (this loop's chronological order and `exceeded_per_summary`'s tail, plus
        # `rows[-1]` below) correct by construction -- never rely on a backend's row order again.
        rows = sorted(rows, key=lambda r: r.ts)

        exceeded: list[bool] = []
        for row in rows:
            freq_dev = abs(float(row.freq_hz) - NOMINAL_FREQ_HZ) if row.freq_hz is not None else 0.0
            voltage_dev_pct = _max_pct_deviation(
                row.v_rms_a, row.v_rms_b, row.v_rms_c, nominal=_NOMINAL_VOLTAGE_V
            )
            thd_dev_pct = _max_abs_deviation(
                row.thd_i_pct_a, row.thd_i_pct_b, row.thd_i_pct_c, baseline=thd_current_pct
            )
            phase_dev = _max_abs(row.phase_angle_deg_a, row.phase_angle_deg_b, row.phase_angle_deg_c)
            exceeded.append(
                exceeds_watch_threshold(freq_dev, freq_offset_std_hz, DRIFT_FLOOR_FREQ_HZ)
                or exceeds_watch_threshold(voltage_dev_pct, voltage_offset_std_pct, DRIFT_FLOOR_VOLTAGE_PCT)
                or exceeds_watch_threshold(thd_dev_pct, DRIFT_FLOOR_THD_PCT / 1.5, DRIFT_FLOOR_THD_PCT)
                or exceeds_watch_threshold(phase_dev, 1.0, DRIFT_FLOOR_PHASE_DEG)
            )
        newest = rows[-1]  # `rows` was explicitly sorted ascending by `ts` above -- last is newest.
        latest_offset = OffsetVector(
            freq_hz=(float(newest.freq_hz) - NOMINAL_FREQ_HZ) if newest.freq_hz is not None else 0.0,
            voltage_pct=_max_pct_deviation(
                newest.v_rms_a, newest.v_rms_b, newest.v_rms_c, nominal=_NOMINAL_VOLTAGE_V
            ),
            phase_deg=_max_abs(newest.phase_angle_deg_a, newest.phase_angle_deg_b, newest.phase_angle_deg_c),
        )
        return DriftObservationWindow(
            exceeded_per_summary=exceeded,
            correlates_with_fleet_event=False,
            latest_measured_offset=latest_offset,
        )


def _window_start(window_s: float) -> datetime:
    return datetime.now(UTC) - timedelta(seconds=window_s)


def _max_abs(*values: Decimal | None) -> float:
    present = [abs(float(v)) for v in values if v is not None]
    return max(present) if present else 0.0


def _max_abs_deviation(*values: Decimal | None, baseline: float) -> float:
    present = [abs(float(v) - baseline) for v in values if v is not None]
    return max(present) if present else 0.0


def _max_pct_deviation(*values: Decimal | None, nominal: float) -> float:
    present = [abs(float(v) - nominal) / nominal * 100.0 for v in values if v is not None]
    return max(present) if present else 0.0


class TraceStoreAssetTracePort:
    """Adapts `opengrid.trace.TraceStore` to `AssetTracePort` (mirrors `guardian.repo.TraceStorePort`)."""

    def __init__(self, store: TraceStore, *, stream_id: str = "assets") -> None:
        self._store = store
        self._stream_id = stream_id

    async def append(self, decision_type: str, payload: dict[str, object]) -> None:
        await self._store.append(self._stream_id, decision_type, decision_type, payload)


#: S5.4/S5.5.4: contract service types this MVP-S+ increment treats as carrying a non-default
#: (PQ-sensitive) PowerQualityEnvelope (S4.b/S4.a) -- `ERCOT_ENERGY`/`HOME` use the fleet-default envelope
#: (S4.c) and are never "sensitive" for G-25/S5.5.4's purposes. NOTE for Agent A/D (reported, not
#: guessed at silently): MVP-S's schema has no hub-level grant table (`og.grant`/`og.reservation` are
#: bank-scoped only, 0001_init.sql) -- which hub actually serves an obligation lives in the `RT_
#: ALLOCATION` trace pre-image's per-item `hub_id`/`obligation_id` pairs (the same source guardian's own
#: `PgProposalPort` reads, `guardian/repo.py`). This adapter reads that same trace payload rather than
#: introducing a second hub-to-obligation index; if a dedicated hub-level grant table is added later,
#: point this query at it instead.
PQ_SENSITIVE_SERVICE_TYPES = frozenset({DATA_CENTER_SERVICE_TYPE, PIPELINE_AC_SERVICE_TYPE})

_ACTIVE_SENSITIVE_GRANT_SQL = """
SELECT 1
FROM og.trace t
JOIN LATERAL jsonb_array_elements(t.payload -> 'items') AS item ON true
JOIN og.obligation ob ON ob.obligation_id = (item ->> 'obligation_id')::uuid
WHERE t.decision_type = 'RT_ALLOCATION'
  AND item ->> 'hub_id' = %(hub_id)s
  AND ob.service_type = ANY(%(service_types)s)
  AND ob.state IN ('COMMITTED', 'DELIVERING')
  AND t.created_at >= now() - interval '1 hour'
LIMIT 1
"""


class PgSensitiveGrantPort:
    """S5.4 step 2 / S5.5.4's "never on a live PQ-sensitive delivery" ledger read. See
    `PQ_SENSITIVE_SERVICE_TYPES`'s docstring for the query's known limitation (no hub-level grant table
    in MVP-S's schema yet) -- flagged in this package's build report, not silently assumed correct."""

    def __init__(
        self, pool: AsyncConnectionPool, *, service_types: frozenset[str] = PQ_SENSITIVE_SERVICE_TYPES
    ) -> None:
        self._pool = pool
        self._service_types = list(service_types)

    async def has_active_non_default_envelope_grant(self, hub_id: str) -> bool:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                _ACTIVE_SENSITIVE_GRANT_SQL, {"hub_id": hub_id, "service_types": self._service_types}
            )
            row = await cur.fetchone()
        return row is not None
