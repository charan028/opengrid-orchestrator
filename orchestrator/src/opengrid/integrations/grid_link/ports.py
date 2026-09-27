"""Seams between the grid-link service and the rest of the orchestrator (grid-link.md S6).

- `TollCallPort`: the ONE core toll-call function (`opengrid.calls`, shared with the operator route and
  the utility customer API). The grid link never implements call logic itself.
- `TelemetryPort`: per-bank SoC / available / delivered kW from the fleet twin.
- `TraceAppend`: `TraceStore.append`, for the raw inbound protocol commands (origin GRID_LINK).
- `BanksOfZone`: zone -> banks, for zone-scoped L2 targets.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

from opengrid.integrations.grid_link.model import BankStatus, CallOutcome

__all__ = ["BanksOfZone", "TelemetryPort", "TollCallPort", "TraceAppend"]


class TollCallPort(Protocol):
    """The core toll-call function as the grid link sees it. Implementations never raise for a refusal:
    a refused call is a `CallOutcome` with `phase = REJECTED` and its reason code."""

    async def issue(
        self, utility_id: str, ems_call_id: int, setpoint_kw: float, duration_min: int
    ) -> CallOutcome:
        """Discharge `setpoint_kw` (> 0) for `duration_min` from now, on the utility's tolling obligation."""
        ...

    async def cancel(self, utility_id: str, ems_call_id: int) -> CallOutcome:
        """End the call the EMS knows as `ems_call_id` now."""
        ...

    async def status(self, utility_id: str, ems_call_id: int) -> CallOutcome:
        """Current state of the call the EMS knows as `ems_call_id`."""
        ...


class TelemetryPort(Protocol):
    async def bank_status(self, bank_ids: Sequence[str]) -> list[BankStatus]:
        """Telemetry for each known bank of `bank_ids` (unknown banks are omitted)."""
        ...


TraceAppend = Callable[..., Awaitable[Any]]
BanksOfZone = Callable[[str], list[str]]
