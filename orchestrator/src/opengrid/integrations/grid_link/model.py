"""Protocol-neutral grid-link commands and status (grid-link.md S3, decision log D-34).

A utility control system (EMS) sends COMMANDS to OpenGrid and reads STATUS back. These types are what
the grid-link service works on; a protocol transport (DNP3 today, an ICCP/TASE.2 bilateral table
tomorrow) only decodes its wire objects into them and encodes `LinkStatus` onto its points.

Sign convention on the link: kW are MAGNITUDES of discharge (the utility asks for "discharge X kW");
the orchestrator's signed convention (+charge / -discharge) is applied only at the core toll-call
boundary (`opengrid.integrations.grid_link.calls_port`).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum

__all__ = [
    "CALL_REASON_CODES",
    "BankStatus",
    "CallOutcome",
    "CallPhase",
    "CancelCall",
    "ControlVerdict",
    "GridCommand",
    "Heartbeat",
    "L2Block",
    "L2Limit",
    "L2LimitValue",
    "LinkStatus",
    "TargetStatus",
    "TollCall",
    "reason_number",
]


class CallPhase(IntEnum):
    """Call state as reported on the link (`CALL_STATE` point)."""

    IDLE = 0
    ACCEPTED = 1
    ACTIVE = 2
    ENDED = 3
    REJECTED = 4


class ControlVerdict(StrEnum):
    """Synchronous answer to one inbound control, before it is queued (protocol-neutral; DNP3 maps it
    onto a control status code)."""

    ACCEPTED = "ACCEPTED"
    NOT_SUPPORTED = "NOT_SUPPORTED"  # no such point in this utility's point list (allow-list)
    NOT_AUTHORIZED = "NOT_AUTHORIZED"  # the point exists but this utility may not use it
    OUT_OF_RANGE = "OUT_OF_RANGE"
    FORMAT_ERROR = "FORMAT_ERROR"  # e.g. an execute without a complete staged call
    INHIBITED = "INHIBITED"  # heartbeat lost: no new calls until the link is healthy again
    TIMEOUT = "TIMEOUT"  # staged values older than the select timeout


#: Numeric `CALL_REASON` codes (interfaces/grid_link/opengrid-gridlink-v1.json `call_reason_codes`).
CALL_REASON_CODES: dict[str, int] = {
    "R-CALL-NOT-FOUND": 1,
    "R-CALL-NOT-DEPLOYABLE": 2,
    "R-CALL-STATE": 3,
    "R-CALL-NO-PRODUCT-DURATION": 4,
    "R-CALL-DURATION-CAP": 5,
    "R-CALL-OVERLAP": 6,
    "R-CALL-CHARGE-REFUSED": 7,
    "R-CALL-OVER-COMMITTED": 8,
    "R-CALL-OUTSIDE-WINDOW": 9,
    "R-CALL-IDEMPOTENCY-CONFLICT": 10,
    "R-CALL-RATE-LIMIT": 11,
    "R-CALL-FLEET-WIDE": 12,
    "R-GL-LINK-DOWN": 50,
    "R-GL-CORE-TIMEOUT": 51,
    "R-GL-INTERNAL": 52,
}
_UNKNOWN_REASON = 99


def reason_number(reason_code: str | None) -> int:
    """The `CALL_REASON` point value for a reason code (0 for none, 99 for an unlisted code)."""
    if reason_code is None:
        return 0
    return CALL_REASON_CODES.get(reason_code, _UNKNOWN_REASON)


# -- inbound commands ------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TollCall:
    """D-29 TOLLING discharge call: discharge `setpoint_kw` (> 0) for `duration_min` minutes, starting
    now. `ems_call_id` is the utility's own id (idempotency key)."""

    ems_call_id: int
    setpoint_kw: float
    duration_min: int


@dataclass(frozen=True, slots=True)
class CancelCall:
    """End a call now. `ems_call_id=None` cancels the utility's current call."""

    ems_call_id: int | None


@dataclass(frozen=True, slots=True)
class L2LimitValue:
    """New L2 discharge ceiling value (kW) for a target; takes effect while its LIMIT is active."""

    target: str
    limit_kw: float


@dataclass(frozen=True, slots=True)
class L2Limit:
    """L2 LIMIT level on/off for a target (bank or zone)."""

    target: str
    active: bool


@dataclass(frozen=True, slots=True)
class L2Block:
    """L2 BLOCK level on/off for a target (bank or zone)."""

    target: str
    active: bool


@dataclass(frozen=True, slots=True)
class Heartbeat:
    """The EMS is alive. Fail safe when these stop (grid-link.md S5)."""


GridCommand = TollCall | CancelCall | L2LimitValue | L2Limit | L2Block | Heartbeat


# -- outbound status -------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """What the core toll-call function said about a call, in link terms."""

    phase: CallPhase
    reason_code: str | None = None
    # MEASURED discharge magnitude (>= 0) from the core's delivery verification (D-38); None while unmeasured
    # or stale, and then served with the COMM_LOST flag, never invented
    delivered_kw: float | None = None


@dataclass(frozen=True, slots=True)
class BankStatus:
    """One bank's telemetry for the link. `soc_pct` is None when no online hub reports SoC; `soc_kwh` and
    `capacity_kwh` (online hubs only) weight the utility-wide aggregate."""

    bank_id: str
    soc_pct: float | None
    available_kw: float
    delivered_kw: float
    soc_kwh: float = 0.0
    capacity_kwh: float = 0.0
    l2_ceiling_kw: float | None = None


@dataclass(frozen=True, slots=True)
class TargetStatus:
    """Echo of one L2 target's levels as OpenGrid holds them."""

    target: str
    limit_active: bool
    block_active: bool
    limit_kw: float | None


@dataclass(frozen=True, slots=True)
class LinkStatus:
    """Everything the link reports back to the utility, at one instant."""

    available_kw: float
    delivered_kw: float
    call_phase: CallPhase
    ems_call_id: int
    call_reason: int
    call_delivered_kw: float | None
    soc_pct: float | None
    heartbeat_count: int
    link_healthy: bool
    telemetry_stale: bool
    toll_calls_enabled: bool
    banks: tuple[BankStatus, ...] = ()
    targets: tuple[TargetStatus, ...] = ()

    @property
    def call_active(self) -> bool:
        return self.call_phase in (CallPhase.ACCEPTED, CallPhase.ACTIVE)

    @property
    def l2_active(self) -> bool:
        return any(t.limit_active or t.block_active for t in self.targets)

    @property
    def call_rejected(self) -> bool:
        return self.call_phase == CallPhase.REJECTED
