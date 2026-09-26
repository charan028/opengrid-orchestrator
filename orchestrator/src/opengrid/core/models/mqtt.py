"""Wire messages for the MQTT device boundary. Mirrors interfaces/mqtt/*.schema.json exactly --
if you change a field here, update the matching schema file (and vice versa); both sides validate
against the schemas independently (BUILD.md S1).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class _Wire(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Telemetry(_Wire):
    hub_id: str
    bank_id: str
    zone: str
    ts: datetime
    soc_kwh: float
    p_kw: float  # +charge / -discharge
    health: Literal["online", "stale", "fault"]
    seq: int
    epoch: int
    fault_code: str | None = None


class CommandItem(_Wire):
    hub_id: str
    p_kw_setpoint: float
    reason_code: str


class CommandPrecondition(_Wire):
    ledger_version: int


class CommandLease(_Wire):
    expires_at: datetime


class CommandBatch(_Wire):
    batch_id: UUID
    bank_id: str
    epoch: int
    seq: int
    issued_at: datetime  # valid_from
    expires_at: datetime
    items: list[CommandItem]
    precondition: CommandPrecondition | None = None
    lease: CommandLease | None = None
    key_id: str
    signature: str

    def signing_payload(self) -> dict[str, Any]:
        """Fields covered by the Ed25519 signature: exactly the 7 listed in interfaces/crypto.md
        S2.1, which is also exactly what the hub recomputes (ogsim.fleet.commands._SIGNED_FIELDS).
        `precondition`/`lease` are not signed; including them (even as null) breaks verification."""
        data: dict[str, Any] = self.model_dump(mode="json", include=set(COMMAND_BATCH_SIGNED_FIELDS))
        return data


COMMAND_BATCH_SIGNED_FIELDS = ("batch_id", "bank_id", "epoch", "seq", "issued_at", "expires_at", "items")


class Ack(_Wire):
    hub_id: str
    batch_id: UUID
    accepted: bool
    applied_p_kw: float | None = None
    reject_reason: Literal["BAD_SIGNATURE", "STALE_EPOCH", "STALE_SEQ", "EXPIRED"] | None = None
    ts: datetime


class StopEvent(_Wire):
    stop_id: UUID
    scope: Literal["fleet", "zone", "bank"]
    scope_id: str | None = None
    action: Literal["ENGAGE", "RELEASE"]
    reason: str
    issued_by: str
    issued_at: datetime
    approver_ref: str | None = None
    key_id: str
    signature: str

    def signing_payload(self) -> dict[str, Any]:
        data: dict[str, Any] = self.model_dump(mode="json", exclude={"key_id", "signature"})
        return data


class Lease(_Wire):
    hub_id: str
    epoch: int
    expires_at: datetime
    issued_at: datetime


class ScadaBankSignal(_Wire):
    bank_id: str
    signal: Literal["APPARENT_POWER_KVA", "REAL_POWER_KW", "VOLTAGE_PU", "CURRENT_A"]
    value: float
    unit: Literal["kVA", "kW", "pu", "A"]
    quality: Literal["good", "stale", "missing", "out_of_range", "comm_fail"]
    ts: datetime


class ScadaUtilityInstruction(_Wire):
    instruction_id: UUID
    bank_id: str
    kind: Literal["LIMIT", "BLOCK", "ESTOP"]
    limit_kw: float | None = None
    issued_at: datetime
    expires_at: datetime | None = None
    issued_by: str


class ScenarioTarget(_Wire):
    kind: Literal["sim", "asset", "zone", "bank", "hub"]
    ref: str


class ScenarioControl(_Wire):
    id: UUID
    target: ScenarioTarget
    type: str
    params: dict[str, Any] = {}
    start: datetime
    duration_s: int | None = None
