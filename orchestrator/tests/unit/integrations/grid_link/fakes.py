"""Fakes shared by the grid-link tests: an in-memory core toll-call port, fleet telemetry, a trace recorder and a
controllable monotonic clock. No database, no broker; sockets only on localhost ephemeral ports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from opengrid.integrations.grid_link.config import L2TargetSettings, UtilityLinkSettings
from opengrid.integrations.grid_link.model import BankStatus, CallOutcome, CallPhase
from opengrid.integrations.grid_link.service import GridLinkService
from opengrid.integrations.sinks import CollectingSink

BANKS = ["bank-040", "bank-041", "bank-042"]
ZONE_BANKS = {"LZ_AEN": ["bank-040", "bank-041"]}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@dataclass
class FakeCalls:
    """Stands in for `opengrid.calls` behind `TollCallPort`: accepts up to `committed_kw`, idempotent by id."""

    committed_kw: float = 5000.0
    calls: dict[int, CallOutcome] = field(default_factory=dict)
    issued: list[tuple[str, int, float, int]] = field(default_factory=list)
    cancelled: list[tuple[str, int]] = field(default_factory=list)

    async def issue(
        self, utility_id: str, ems_call_id: int, setpoint_kw: float, duration_min: int
    ) -> CallOutcome:
        self.issued.append((utility_id, ems_call_id, setpoint_kw, duration_min))
        if ems_call_id in self.calls:
            return self.calls[ems_call_id]
        if setpoint_kw > self.committed_kw:
            outcome = CallOutcome(CallPhase.REJECTED, "R-CALL-OVER-COMMITTED")
        else:
            outcome = CallOutcome(CallPhase.ACCEPTED)
        self.calls[ems_call_id] = outcome
        return outcome

    async def cancel(self, utility_id: str, ems_call_id: int) -> CallOutcome:
        self.cancelled.append((utility_id, ems_call_id))
        if ems_call_id not in self.calls:
            return CallOutcome(CallPhase.REJECTED, "R-CALL-NOT-FOUND")
        self.calls[ems_call_id] = CallOutcome(CallPhase.ENDED)
        return self.calls[ems_call_id]

    async def status(self, utility_id: str, ems_call_id: int) -> CallOutcome:
        return self.calls.get(ems_call_id, CallOutcome(CallPhase.REJECTED, "R-CALL-NOT-FOUND"))


class FakeTelemetry:
    def __init__(self) -> None:
        self.banks = {
            b: BankStatus(
                b, soc_pct=50.0, available_kw=1000.0, delivered_kw=0.0, soc_kwh=500.0, capacity_kwh=1000.0
            )
            for b in BANKS
        }

    async def bank_status(self, bank_ids: Sequence[str]) -> list[BankStatus]:
        return [self.banks[b] for b in bank_ids if b in self.banks]


class TraceRecorder:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, dict[str, Any]]] = []

    async def __call__(
        self, stream_id: str, decision_type: str, event_class: str, payload: dict[str, Any]
    ) -> None:
        self.rows.append((stream_id, decision_type, event_class, payload))

    def events(self, event_class: str) -> list[dict[str, Any]]:
        return [row[3] for row in self.rows if row[2] == event_class]


def make_settings(**overrides: Any) -> UtilityLinkSettings:
    base: dict[str, Any] = {
        "utility_id": "AUSTIN_ENERGY",
        "enabled": True,
        "listen_host": "127.0.0.1",
        "listen_port": 0,
        "allowed_peers": ["127.0.0.1/32"],
        "banks": BANKS,
        "l2_targets": [
            L2TargetSettings(name="LZ_AEN", zone="LZ_AEN"),
            L2TargetSettings(name="B041", bank_id="bank-041"),
        ],
        "heartbeat_timeout_s": 30.0,
        "max_setpoint_kw": 26000.0,
    }
    base.update(overrides)
    return UtilityLinkSettings(**base)


@dataclass
class Harness:
    service: GridLinkService
    calls: FakeCalls
    telemetry: FakeTelemetry
    trace: TraceRecorder
    sink: CollectingSink
    clock: FakeClock


def make_harness(settings: UtilityLinkSettings | None = None, sink: Any = None) -> Harness:
    calls, telemetry, trace, clock = FakeCalls(), FakeTelemetry(), TraceRecorder(), FakeClock()
    collecting = sink if sink is not None else CollectingSink()
    service = GridLinkService(
        settings or make_settings(),
        calls=calls,
        telemetry=telemetry,
        trace=trace,
        sink=collecting,
        banks_of_zone=lambda zone: ZONE_BANKS.get(zone, []),
        monotonic=clock,
    )
    return Harness(service, calls, telemetry, trace, collecting, clock)
