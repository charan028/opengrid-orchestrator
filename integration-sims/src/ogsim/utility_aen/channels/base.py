"""ogsim.utility_aen.channels.base -- the Channel interface: how the simulated utility EMS delivers a toll
call to the orchestrator. The simulator core (schedule, scenarios, runtime) only ever talks to a
`Channel`; each transport is one implementation:

- `customer_api` (this lane): HTTPS through Apache to `/og/api/customer/v1/utility/`;
- `grid_link` (GRID-LINK lane): the utility's grid link (DNP3 over mutual TLS).

A channel never raises for an orchestrator refusal: it returns a `CallResult` with `accepted=False`, the
`state` REFUSED and the orchestrator's `reason_code`. It raises `ChannelError` only when the transport
itself failed (unreachable, timeout, auth), so the runtime can log and retry that differently.

Sign convention everywhere: kW is signed, + charge / - discharge. A toll call is discharge, so `kw` < 0;
a scenario deliberately sends a positive kW to prove the orchestrator refuses a charge call.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

#: `channels.<name>` sub-table of the sim config, passed to that channel's factory as-is.
ChannelSettings = dict[str, Any]

#: States a channel reports. The orchestrator's own states, plus UNKNOWN when a channel cannot tell.
#: r3.4.1 reports ACTIVE (delivery unmeasured); RAMPING/DELIVERING come back in r3.4.2 with measured delivery.
CALL_STATES = frozenset({"ACCEPTED", "ACTIVE", "RAMPING", "DELIVERING", "COMPLETED", "REFUSED", "UNKNOWN"})


class ChannelError(RuntimeError):
    """The transport failed (not an orchestrator refusal). Never carries a credential."""


@dataclass(frozen=True)
class CallSpec:
    """One toll call as the utility EMS issues it. `call_ref` is the EMS's own id, used as the
    idempotency key, so resending the same spec never creates a second call."""

    call_ref: str
    kw: float
    start: datetime
    duration_min: int
    reason: str = "utility toll call"


@dataclass(frozen=True)
class CallResult:
    """What the orchestrator answered. `remote_id` is the orchestrator's own call id when it has one."""

    call_ref: str
    accepted: bool
    state: str
    reason_code: str | None = None
    detail: str | None = None
    #: MEASURED delivery (DELIVERY-VERIFY): signed kW (< 0 = discharge) and discharged kWh; None while the
    #: orchestrator has not measured the call yet (its `delivery_measured` false). Never planned/granted kW.
    delivered_kw: float | None = None
    delivered_kwh: float | None = None
    remote_id: str | None = None
    #: The orchestrator's delivery verdict (IN_PROGRESS, PASS, PARTIAL, FAIL or UNMEASURED), if reported.
    delivery_state: str | None = None
    #: PLANNED/granted kW and kWh (deprecated granted_* keys from r3.4.1-r3.4.2 orchestrators). Shown next to
    #: the measured values, never copied into delivered_*: a grant is not a delivery.
    granted_kw: float | None = None
    granted_kwh: float | None = None


class Channel(Protocol):
    name: str

    async def issue_call(self, spec: CallSpec) -> CallResult:
        """Send the call (idempotent on `spec.call_ref`)."""
        ...

    async def cancel(self, call_ref: str, *, end_at: datetime | None = None) -> CallResult:
        """Cancel now (no `end_at`) or shorten to `end_at`."""
        ...

    async def status(self, call_ref: str) -> CallResult:
        """Read back the call's state and delivery."""
        ...


ChannelFactory = Callable[[ChannelSettings], Channel]

__all__ = [
    "CALL_STATES",
    "CallResult",
    "CallSpec",
    "Channel",
    "ChannelError",
    "ChannelFactory",
    "ChannelSettings",
]
