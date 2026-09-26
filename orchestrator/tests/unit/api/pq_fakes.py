"""In-memory fakes for `opengrid.api.routers.pq`'s own unit tests: a `PqStore`, the `opengrid.assets`
ports `AssetHealthService.request_calibration` touches, and a recording MQTT publisher. No DB, no broker."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from opengrid.api.pq_store import HubLocation, ObligationEnvelope
from opengrid.assets.ports import DriftObservationWindow
from opengrid.assets.service import AssetHealthPorts, AssetHealthService
from opengrid.core.models.pq import PqWaveformSummaryRow
from opengrid.core.pq import OffsetVector


class FakePqStore:
    def __init__(self) -> None:
        self.hubs: dict[str, HubLocation] = {}
        self.summary_rows: list[PqWaveformSummaryRow] = []
        self.raw_index: list[dict[str, Any]] = []
        self.obligations: dict[UUID, ObligationEnvelope] = {}
        self.inverter_pq: dict[str, dict[str, Any]] = {}
        self.events: dict[str, list[dict[str, Any]]] = {}
        self.open_orders: dict[str, dict[str, Any]] = {}
        self.calibrations: dict[str, list[dict[str, Any]]] = {}
        self.orders: list[dict[str, Any]] = []

    def add_hub(self, hub_id: str, *, bank_id: str = "bank-01", zone: str = "north") -> None:
        self.hubs[hub_id] = HubLocation(hub_id=hub_id, zone=zone, bank_id=bank_id)

    async def hub_location(self, hub_id: str) -> HubLocation | None:
        return self.hubs.get(hub_id)

    async def summaries(self, hub_ids: Sequence[str], *, since: datetime) -> list[PqWaveformSummaryRow]:
        rows = [r for r in self.summary_rows if r.hub_id in hub_ids and r.ts >= since]
        return sorted(rows, key=lambda r: (r.hub_id, r.ts), reverse=True)

    async def raw_captures(self, hub_id: str, *, until: datetime, limit: int) -> list[dict[str, Any]]:
        rows = [r for r in self.raw_index if r["hub_id"] == hub_id and r["ts"] <= until]
        return sorted(rows, key=lambda r: r["ts"], reverse=True)[:limit]

    async def hub_ids_for_bank(self, bank_id: str) -> list[str]:
        return sorted(h.hub_id for h in self.hubs.values() if h.bank_id == bank_id)

    async def obligation_envelope(self, obligation_id: UUID) -> ObligationEnvelope | None:
        return self.obligations.get(obligation_id)

    async def hub_inverter_pq(self, hub_id: str) -> dict[str, Any] | None:
        return self.inverter_pq.get(hub_id)

    async def asset_events(self, hub_id: str, *, limit: int) -> list[dict[str, Any]]:
        return self.events.get(hub_id, [])[:limit]

    async def open_work_order(self, hub_id: str) -> dict[str, Any] | None:
        return self.open_orders.get(hub_id)

    async def calibration_history(self, hub_id: str, *, limit: int) -> list[dict[str, Any]]:
        return self.calibrations.get(hub_id, [])[:limit]

    async def work_orders(self, *, status: str | None, limit: int) -> list[dict[str, Any]]:
        return [o for o in self.orders if status is None or o["status"] == status][:limit]


class FakeAssetPorts:
    """Every `opengrid.assets.ports` method `AssetHealthService.request_calibration` calls."""

    def __init__(self) -> None:
        self.sensitive_grant = False
        self.last_attempt_epoch: float | None = None
        self.window: DriftObservationWindow | None = DriftObservationWindow(
            exceeded_per_summary=[True] * 10,
            correlates_with_fleet_event=False,
            latest_measured_offset=OffsetVector(freq_hz=0.05, voltage_pct=0.4, phase_deg=1.0),
        )
        self.recorded: list[dict[str, Any]] = []
        self.traced: list[tuple[str, dict[str, object]]] = []

    async def has_active_non_default_envelope_grant(self, hub_id: str) -> bool:
        return self.sensitive_grant

    async def last_attempt_epoch_s(self, hub_id: str) -> float | None:
        return self.last_attempt_epoch

    async def observation_window(self, hub_id: str) -> DriftObservationWindow | None:
        return self.window

    async def record_attempt(self, **kwargs: Any) -> None:
        self.recorded.append(kwargs)

    async def append(self, decision_type: str, payload: dict[str, object]) -> None:
        self.traced.append((decision_type, payload))

    def service(self) -> AssetHealthService:
        return AssetHealthService(
            ports=AssetHealthPorts(
                drift=self,
                asset_health=self,
                calibration_attempts=self,
                work_orders=self,
                asset_events=self,
                trace=self,
                sensitive_grants=self,
            )
        )


class RecordingPublisher:
    def __init__(self) -> None:
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, topic_suffix: str, payload: dict[str, Any]) -> None:
        self.published.append((topic_suffix, payload))
