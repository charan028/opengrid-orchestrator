"""I/O seams for `opengrid.assets.service.AssetHealthService` (07-delivery/06-service-profiles-and-
power-quality.md S5.5, S6.5, S6.7). `opengrid.assets.repo` implements these against Postgres
(`og.hub_inverter_pq`'s asset-health columns, `og.calibration_attempt`, `og.maintenance_work_order`,
`og.asset_event` -- migration `0011_asset_health.sql`); tests use in-memory fakes -- the same "pure logic
separated from I/O" split as `opengrid.guardian.ports`/`service.py` (BUILD.md S5a).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from opengrid.core.models.pq import MaintenanceWorkOrder
from opengrid.core.pq import CalibrationOutcome, OffsetVector
from opengrid.guardian.pq_ports import AssetState


@dataclass(frozen=True, slots=True)
class PendingCalibrationAttempt:
    """The subset of a recorded `og.calibration_attempt` row `handle_calibration_ack` needs: which hub
    it was for, and the pre-calibration measured offset the candidate's correction was computed from."""

    hub_id: str
    measured_offset: OffsetVector


@dataclass(frozen=True, slots=True)
class DriftObservationWindow:
    """S5.5.1's rolling observation-window evidence for one tracked dimension (frequency, voltage, THD
    or phase-angle offset) on one hub: whether each summary in the window exceeded the WATCH threshold,
    plus whether the deviation correlates with an already-explained bank/feeder-wide event (a transient a
    fleet-wide grid excursion causes, which must never be mistaken for hardware drift, S5.5.1)."""

    exceeded_per_summary: Sequence[bool]
    correlates_with_fleet_event: bool
    latest_measured_offset: OffsetVector


class DriftObservationPort(Protocol):
    async def observation_window(self, hub_id: str) -> DriftObservationWindow | None:
        """The rolling 15-minute (default) observation window for `hub_id`, built from measured
        waveform summaries (S6.5) -- `None` if there is not yet enough telemetry to evaluate (never
        treated as "no drift", per K1's stale-data pattern; the caller simply defers judgement)."""
        ...


@dataclass(frozen=True, slots=True)
class HubAssetRecord:
    """The asset-health columns of one `og.hub_inverter_pq` row (0011_asset_health.sql S1)."""

    hub_id: str
    asset_state: AssetState
    asset_state_since: datetime
    consecutive_correctable_drifts: int
    last_recalibration_at: datetime | None
    ride_through_class: str


class AssetHealthRepoPort(Protocol):
    async def list_hub_ids(self) -> list[str]:
        """Every hub with a characterization row (`og.hub_inverter_pq`) -- the runner's own candidate
        set for a drift-evaluation sweep (`opengrid.assets.runner.run_once`)."""
        ...

    async def get(self, hub_id: str) -> HubAssetRecord | None: ...

    async def set_state(
        self, hub_id: str, state: AssetState, *, since: datetime, consecutive_correctable_drifts: int
    ) -> None: ...

    async def record_recalibration(self, hub_id: str, *, at: datetime) -> None: ...

    async def reset_characterization(self, hub_id: str, *, last_estimated_at: datetime) -> None:
        """S5.5.6 recommissioning: reset `last_estimated_at` once a replaced unit's post-installation
        verification capture passes, so the row is again read as freshly characterized, not stale."""
        ...


class CalibrationAttemptRepoPort(Protocol):
    async def last_attempt_epoch_s(self, hub_id: str) -> float | None: ...

    async def last_corrected_at(self, hub_id: str) -> datetime | None:
        """S5.5.4 recurrence check: the most recent attempt against `hub_id` whose `outcome = 'CORRECTED'`,
        or `None` -- used to detect "drift recurs within N days of a CORRECTED outcome"."""
        ...

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
    ) -> None: ...

    async def get_pending(self, calibration_id: UUID) -> PendingCalibrationAttempt | None:
        """The `hub_id` and pre-calibration `measured_offset` recorded by `record_attempt` for
        `calibration_id` -- read back when a `CalibrationAck` arrives (`opengrid.assets.calibration_ack.
        handle_calibration_ack`) so the outcome classifier has the SAME pre-offset the candidate was
        built from, never a value the ack itself claims."""
        ...

    async def record_outcome(
        self, calibration_id: UUID, outcome: CalibrationOutcome, *, verified_at: datetime
    ) -> None: ...


class WorkOrderRepoPort(Protocol):
    async def open(
        self, hub_id: str, *, severity: str, evidence: dict[str, object], opened_at: datetime
    ) -> UUID: ...

    async def close(
        self, work_order_id: UUID, *, closed_at: datetime, technician_notes: str | None
    ) -> None: ...

    async def open_for_hub(self, hub_id: str) -> MaintenanceWorkOrder | None:
        """The hub's currently `OPEN`/`IN_PROGRESS` work order, if any -- so replacement/recommissioning
        can close the SAME order that justified the escalation, never leaving an orphaned open order."""
        ...


class AssetEventRepoPort(Protocol):
    async def record_state_transition(
        self, hub_id: str, *, from_state: AssetState, to_state: AssetState, reason_code: str, at: datetime
    ) -> None: ...

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
    ) -> None: ...

    async def record_recommissioned(self, hub_id: str, *, at: datetime) -> None: ...


class AssetTracePort(Protocol):
    async def append(self, decision_type: str, payload: dict[str, object]) -> None:
        """K10/K11: every state transition and calibration attempt is a traced decision -- `decision_
        type` is `'ASSET_STATE_TRANSITION'` or `'CALIBRATION_ATTEMPT'` (0011_asset_health.sql S4).
        Distinct from the guardian's own K10 pre-image check on the `CalibrationCommand` itself (that
        trace write happens before the command reaches the guardian, same as `RT_ALLOCATION` for a
        `CommandBatch` -- see this package's README for the exact call site)."""
        ...


class SensitiveGrantPort(Protocol):
    async def has_active_non_default_envelope_grant(self, hub_id: str) -> bool:
        """The ladder's OWN primary check (S5.4 step 2 / S5.5.4: "never on a live PQ-sensitive
        delivery"), mirroring `opengrid.guardian.pq_ports.SensitiveGrantPort`'s independent re-check.
        Two separate reads of the same ledger fact, primary + independent, per K2/K14's shape -- not a
        duplicated implementation of ledger access, since each side owns its own adapter."""
        ...
