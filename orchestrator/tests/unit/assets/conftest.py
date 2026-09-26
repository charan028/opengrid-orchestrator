"""In-memory fakes for every `opengrid.assets.ports` Protocol, mirroring
`tests/unit/guardian/conftest.py`'s style."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid4

import pytest

from opengrid.assets.ports import (
    DriftObservationWindow,
    HubAssetRecord,
    PendingCalibrationAttempt,
)
from opengrid.assets.service import AssetHealthPorts, AssetHealthService
from opengrid.core.models.pq import MaintenanceWorkOrder
from opengrid.core.pq import CalibrationOutcome, OffsetVector
from opengrid.guardian.pq_ports import AssetState

HUB_ID = "hub-0001"


class FakeDrift:
    def __init__(self) -> None:
        self.windows: dict[str, DriftObservationWindow] = {}

    async def observation_window(self, hub_id: str) -> DriftObservationWindow | None:
        return self.windows.get(hub_id)


class FakeAssetHealth:
    def __init__(self) -> None:
        self.records: dict[str, HubAssetRecord] = {}
        self.reset_at: dict[str, datetime] = {}

    async def list_hub_ids(self) -> list[str]:
        return sorted(self.records)

    async def get(self, hub_id: str) -> HubAssetRecord | None:
        return self.records.get(hub_id)

    async def set_state(
        self, hub_id: str, state: AssetState, *, since: datetime, consecutive_correctable_drifts: int
    ) -> None:
        existing = self.records[hub_id]
        self.records[hub_id] = HubAssetRecord(
            hub_id=hub_id,
            asset_state=state,
            asset_state_since=since,
            consecutive_correctable_drifts=consecutive_correctable_drifts,
            last_recalibration_at=existing.last_recalibration_at,
            ride_through_class=existing.ride_through_class,
        )

    async def record_recalibration(self, hub_id: str, *, at: datetime) -> None:
        existing = self.records[hub_id]
        self.records[hub_id] = HubAssetRecord(
            hub_id=hub_id,
            asset_state=existing.asset_state,
            asset_state_since=existing.asset_state_since,
            consecutive_correctable_drifts=existing.consecutive_correctable_drifts,
            last_recalibration_at=at,
            ride_through_class=existing.ride_through_class,
        )

    async def reset_characterization(self, hub_id: str, *, last_estimated_at: datetime) -> None:
        self.reset_at[hub_id] = last_estimated_at


class FakeCalibrationAttempts:
    def __init__(self) -> None:
        self.last_attempt: dict[str, float] = {}
        self.last_corrected: dict[str, datetime] = {}
        self.attempts: dict[UUID, dict[str, object]] = {}
        self.outcomes: dict[UUID, CalibrationOutcome] = {}

    async def last_attempt_epoch_s(self, hub_id: str) -> float | None:
        return self.last_attempt.get(hub_id)

    async def last_corrected_at(self, hub_id: str) -> datetime | None:
        return self.last_corrected.get(hub_id)

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
        self.attempts[calibration_id] = {
            "hub_id": hub_id,
            "requested_at": requested_at,
            "measured_offset": measured_offset,
        }
        self.last_attempt[hub_id] = requested_at.timestamp()

    async def get_pending(self, calibration_id: UUID) -> PendingCalibrationAttempt | None:
        attempt = self.attempts.get(calibration_id)
        if attempt is None:
            return None
        return PendingCalibrationAttempt(
            hub_id=str(attempt["hub_id"]),
            measured_offset=attempt["measured_offset"],  # type: ignore[arg-type]
        )

    async def record_outcome(
        self, calibration_id: UUID, outcome: CalibrationOutcome, *, verified_at: datetime
    ) -> None:
        self.outcomes[calibration_id] = outcome
        hub_id = self.attempts[calibration_id]["hub_id"]
        if outcome == CalibrationOutcome.CORRECTED:
            self.last_corrected[str(hub_id)] = verified_at


class FakeWorkOrders:
    def __init__(self) -> None:
        self.open_orders: dict[str, MaintenanceWorkOrder] = {}
        self.closed: list[UUID] = []
        self.closed_status: dict[UUID, str] = {}

    async def open(
        self, hub_id: str, *, severity: str, evidence: dict[str, object], opened_at: datetime
    ) -> UUID:
        work_order_id = uuid4()
        self.open_orders[hub_id] = MaintenanceWorkOrder(
            work_order_id=work_order_id,
            hub_id=hub_id,
            severity=severity,  # type: ignore[arg-type]
            evidence=evidence,
            status="OPEN",
            opened_at=opened_at,
        )
        return work_order_id

    async def close(
        self,
        work_order_id: UUID,
        *,
        closed_at: datetime,
        technician_notes: str | None,
        status: str = "CLOSED",
    ) -> None:
        self.closed.append(work_order_id)
        self.closed_status[work_order_id] = status
        for hub_id, order in list(self.open_orders.items()):
            if order.work_order_id == work_order_id:
                del self.open_orders[hub_id]

    async def open_for_hub(self, hub_id: str) -> MaintenanceWorkOrder | None:
        return self.open_orders.get(hub_id)


class FakeAssetEvents:
    def __init__(self) -> None:
        self.state_transitions: list[dict[str, object]] = []
        self.replacements: list[dict[str, object]] = []
        self.recommissioned: list[str] = []

    async def record_state_transition(
        self, hub_id: str, *, from_state: AssetState, to_state: AssetState, reason_code: str, at: datetime
    ) -> None:
        self.state_transitions.append(
            {"hub_id": hub_id, "from_state": from_state, "to_state": to_state, "reason_code": reason_code}
        )

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
        self.replacements.append({"hub_id": hub_id, "new_serial": new_serial})

    async def record_recommissioned(self, hub_id: str, *, at: datetime) -> None:
        self.recommissioned.append(hub_id)


class FakeTrace:
    def __init__(self) -> None:
        self.appended: list[tuple[str, dict[str, object]]] = []

    async def append(self, decision_type: str, payload: dict[str, object]) -> None:
        self.appended.append((decision_type, payload))


class FakeSensitiveGrants:
    def __init__(self) -> None:
        self.sensitive_hubs: set[str] = set()

    async def has_active_non_default_envelope_grant(self, hub_id: str) -> bool:
        return hub_id in self.sensitive_hubs


@dataclass
class Fakes:
    drift: FakeDrift
    asset_health: FakeAssetHealth
    calibration_attempts: FakeCalibrationAttempts
    work_orders: FakeWorkOrders
    asset_events: FakeAssetEvents
    trace: FakeTrace
    sensitive_grants: FakeSensitiveGrants

    def as_ports(self) -> AssetHealthPorts:
        return AssetHealthPorts(
            drift=self.drift,
            asset_health=self.asset_health,
            calibration_attempts=self.calibration_attempts,
            work_orders=self.work_orders,
            asset_events=self.asset_events,
            trace=self.trace,
            sensitive_grants=self.sensitive_grants,
        )


@pytest.fixture
def fakes() -> Fakes:
    return Fakes(
        FakeDrift(),
        FakeAssetHealth(),
        FakeCalibrationAttempts(),
        FakeWorkOrders(),
        FakeAssetEvents(),
        FakeTrace(),
        FakeSensitiveGrants(),
    )


@pytest.fixture
def service(fakes: Fakes) -> AssetHealthService:
    return AssetHealthService(ports=fakes.as_ports())


def make_asset_record(
    *,
    hub_id: str = HUB_ID,
    asset_state: AssetState = "OK",
    since: datetime,
    consecutive: int = 0,
    ride_through_class: str = "CATEGORY_III",
) -> HubAssetRecord:
    return HubAssetRecord(
        hub_id=hub_id,
        asset_state=asset_state,
        asset_state_since=since,
        consecutive_correctable_drifts=consecutive,
        last_recalibration_at=None,
        ride_through_class=ride_through_class,
    )
