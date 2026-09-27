"""Data types of the dispatch-call core (D-29, D-33): who calls (`CallOrigin`), what is called
(`CallKind`), the request, the persisted ledger row (`CallRecord`), the read-back status and the typed
refusal. Sign convention everywhere: kW is signed, + charge / - discharge; a call is discharge-only.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

#: The longest product any call can be capped by (ERCOT Non-Spin, 240 min); the obligation's own product
#: rule is the real cap (TOLLING 90 min, ECRS 60 min).
MAX_CALL_MINUTES = 240
MAX_TEXT_LEN = 200
#: `CallStatus.delivered_kw/_kwh` are MEASURED from telemetry by `opengrid.delivery` (D-38), never grants.
#: Before the first evaluated bucket (or with no record yet) delivery is UNMEASURED and the state is ACTIVE.
DELIVERY_STATE_UNMEASURED = "UNMEASURED"
#: The deprecated `granted_*` status fields: the allocator's grants (planned), kept for compatibility.
GRANTED_DESCRIPTION = "planned; removed in r3.5; use delivered_*"


class CallOrigin(StrEnum):
    """Where a call came from. Recorded in the trace, the call ledger and `og.as_deployment.source`."""

    OPERATOR = "OPERATOR"
    UTILITY = "UTILITY"
    GRID_LINK = "GRID_LINK"
    MARKET_SIM = "MARKET_SIM"
    SCENARIO = "SCENARIO"
    ERCOT_POLL = "ERCOT_POLL"


class CallKind(StrEnum):
    """`AS`: an ERCOT_AS award (NPRR1282 capacity hold). `UTILITY_CALL`: a utility's discharge call on a
    tolling obligation (D-29: REGULATED_CAPACITY, contract variant TOLLING)."""

    AS = "AS"
    UTILITY_CALL = "UTILITY_CALL"


class CallOutcome(StrEnum):
    ACCEPTED = "ACCEPTED"
    REFUSED = "REFUSED"


class CallState(StrEnum):
    """Read-back state of a call. ACCEPTED: before its start. RAMPING / DELIVERING: its window is running
    and MEASURED delivery (`opengrid.delivery`, D-38) is below / at or above `ramping_fraction` of the
    call's target. ACTIVE: running, but no measurement yet (no evaluated bucket, or stale telemetry).
    COMPLETED: ended or cancelled. REFUSED: never deployed (see the record's reason code)."""

    ACCEPTED = "ACCEPTED"
    ACTIVE = "ACTIVE"
    RAMPING = "RAMPING"
    DELIVERING = "DELIVERING"
    COMPLETED = "COMPLETED"
    REFUSED = "REFUSED"


#: `og.as_deployment.source` per origin (migration 0047). ERCOT_POLL is the ercot_mms deployment poller.
SOURCE_FOR_ORIGIN: dict[CallOrigin, str] = {
    CallOrigin.OPERATOR: "OPERATOR",
    CallOrigin.UTILITY: "UTILITY",
    CallOrigin.GRID_LINK: "GRID_LINK",
    CallOrigin.MARKET_SIM: "MARKET_SIM",
    CallOrigin.SCENARIO: "SCENARIO",
    CallOrigin.ERCOT_POLL: "ERCOT",
}

#: What each origin may call: a utility (API or grid link) calls only tolling obligations; ERCOT and the
#: market sim deploy only AS awards; an operator or a scenario may do either.
ALLOWED_KINDS: dict[CallOrigin, frozenset[CallKind]] = {
    CallOrigin.OPERATOR: frozenset(CallKind),
    CallOrigin.SCENARIO: frozenset(CallKind),
    CallOrigin.UTILITY: frozenset({CallKind.UTILITY_CALL}),
    CallOrigin.GRID_LINK: frozenset({CallKind.UTILITY_CALL}),
    CallOrigin.MARKET_SIM: frozenset({CallKind.AS}),
    CallOrigin.ERCOT_POLL: frozenset({CallKind.AS}),
}


class CallRequest(BaseModel):
    """One call. Exactly one of `duration_minutes` / `end_at` bounds it. `requested_kw` is signed and
    must be < 0 (discharge); None means the obligation's full committed kW. `start_at` None means now.
    `utility_id` scopes the call to that utility's own obligations (anything else is NOT FOUND); with
    `obligation_id` None it resolves the utility's tolling obligation whose window covers the start.
    `idempotency_key` is unique per `principal`: a replay returns the original call."""

    model_config = ConfigDict(frozen=True)

    origin: CallOrigin
    principal: str = Field(min_length=1, max_length=MAX_TEXT_LEN)
    reason: str = Field(min_length=1, max_length=MAX_TEXT_LEN)
    duration_minutes: int | None = Field(default=None, ge=1, le=MAX_CALL_MINUTES)
    end_at: datetime | None = None
    obligation_id: UUID | None = None
    utility_id: str | None = Field(default=None, min_length=1, max_length=64)
    requested_kw: float | None = None
    start_at: datetime | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=MAX_TEXT_LEN)
    #: An operator's `scope = ALL`: refused (it needs a two-person approval no path provides).
    fleet_wide: bool = False

    @model_validator(mode="after")
    def _one_bound(self) -> CallRequest:
        if (self.duration_minutes is None) == (self.end_at is None):
            raise ValueError("give exactly one of duration_minutes or end_at")
        for value in (self.start_at, self.end_at):
            if value is not None and value.tzinfo is None:
                raise ValueError("start_at/end_at must be timezone-aware")
        return self

    def fingerprint(self) -> str:
        """Hash of what the call asks for (not who or why): an idempotent replay must match it."""
        material = {
            "obligation_id": str(self.obligation_id) if self.obligation_id else None,
            "utility_id": self.utility_id,
            "requested_kw": self.requested_kw,
            "start_at": self.start_at.isoformat() if self.start_at else None,
            "end_at": self.end_at.isoformat() if self.end_at else None,
            "duration_minutes": self.duration_minutes,
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


class CallRecord(BaseModel):
    """A row of the call ledger (`og.dispatch_call`), accepted or refused."""

    model_config = ConfigDict(frozen=True)

    call_id: UUID
    outcome: CallOutcome
    origin: CallOrigin
    principal: str
    reason: str
    start_at: datetime
    end_at: datetime
    duration_minutes: int
    reason_code: str | None = None
    detail: str | None = None
    deployment_id: UUID | None = None
    obligation_id: UUID | None = None
    utility_id: str | None = None
    kind: CallKind | None = None
    requested_kw: float | None = None
    committed_kw: float | None = None
    idempotency_key: str | None = None
    trace_id: UUID | None = None
    created_at: datetime | None = None
    cancelled_at: datetime | None = None
    #: True when this record answers an idempotent replay (nothing new was deployed).
    replayed: bool = False

    @property
    def target_kw(self) -> float | None:
        """The discharge the call asks for (signed, < 0): the requested kW, else the committed kW."""
        if self.requested_kw is not None:
            return self.requested_kw
        return -self.committed_kw if self.committed_kw is not None else None

    def public(self) -> dict[str, Any]:
        """JSON form for API responses (the request fingerprint is never exposed)."""
        return {**self.model_dump(mode="json"), "target_kw": self.target_kw}


class CallStatus(BaseModel):
    """`call_status`: the record, its state and its MEASURED delivery (`opengrid.delivery`, D-38).
    `delivered_kw` is signed (< 0 = discharge; None before the start or while unmeasured), the latest
    evaluated bucket's; `delivered_kwh` is the discharged energy magnitude so far. `delivery` is None
    until the delivery job has evaluated the call."""

    model_config = ConfigDict(frozen=True)

    call: CallRecord
    state: CallState
    delivered_kw: float | None
    delivered_kwh: float | None
    as_of: datetime
    delivery: MeasuredDelivery | None = None
    #: Deprecated (planned/granted, NOT measured; removed in r3.5): the allocator's grants over the call.
    granted_kw: float | None = None
    granted_kwh: float | None = None

    def public(self) -> dict[str, Any]:
        measured = self.delivery is not None and self.delivery.is_measured
        return {
            **self.call.public(),
            "state": self.state.value,
            "delivered_kw": self.delivered_kw,
            "delivered_kwh": self.delivered_kwh,
            "delivery_measured": measured,
            "delivery_state": self.delivery.result
            if measured and self.delivery
            else DELIVERY_STATE_UNMEASURED,
            "delivery_reasons": list(self.delivery.reasons) if self.delivery else [],
            "meter_status": self.delivery.meter_status if self.delivery else None,
            "delivery_as_of": (
                self.delivery.evaluated_to.isoformat()
                if self.delivery and self.delivery.evaluated_to
                else None
            ),
            "granted_kw": self.granted_kw,
            "granted_kwh": self.granted_kwh,
            "granted_description": GRANTED_DESCRIPTION,
            "as_of": self.as_of.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class AwardView:
    """What the checks read about the called obligation. `duration_minutes` is its OWN product rule's
    full-deployment duration (None = unknown); windows and `utility_id` may be None (unknown / FREE)."""

    obligation_id: UUID
    service_type: str
    variant: str | None
    state: str
    duration_minutes: int | None
    committed_kw: float | None
    window_start: datetime | None
    window_end: datetime | None
    utility_id: str | None


@dataclass(frozen=True, slots=True)
class Granted:
    """Deprecated (removed in r3.5): grants to a called obligation over the call so far, planned and NOT
    metered: the latest cycle's granted discharge (kW magnitude, None when no cycle yet) and kWh."""

    last_kw: float | None
    kwh: float


@dataclass(frozen=True, slots=True)
class MeasuredDelivery:
    """A call's measured delivery, from its `og.delivery_record` (`opengrid.delivery.store.fetch_record`):
    the latest evaluated bucket's delivered kW (signed, < 0 = discharge; None = stale), discharged kWh so
    far, the verification result (IN_PROGRESS/PASS/PARTIAL/FAIL) with reasons, and the meter check."""

    delivered_kw: float | None
    discharged_kwh: float
    result: str
    reasons: tuple[str, ...]
    meter_status: str
    evaluated_to: datetime | None

    @property
    def is_measured(self) -> bool:
        """A measured value exists: a delivered kW in the latest bucket, or a final verdict. A running call
        whose first buckets had no telemetry yet is NOT measured (delivery_state UNMEASURED)."""
        return self.delivered_kw is not None or self.result != "IN_PROGRESS"


CallStatus.model_rebuild()


class CallRefused(Exception):  # noqa: N818 -- a domain outcome, named for what it is
    """A call (or a cancel) was refused. `reason_code` is one of `opengrid.calls.reasons`; `http_status`
    is the status an HTTP surface should answer; `call` is the persisted REFUSED record (None when
    nothing was recorded, e.g. an unknown call on cancel)."""

    def __init__(
        self, reason_code: str, detail: str, http_status: int, call: CallRecord | None = None
    ) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail
        self.http_status = http_status
        self.call = call
