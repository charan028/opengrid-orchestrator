"""DNP3 point layout "opengrid-gridlink-v1" (interfaces/grid_link/opengrid-gridlink-v1.json; grid-link.md
S4). Pure mapping between point indices and grid-link roles; no I/O.

The layout is the per-utility ALLOW-LIST: a control on any index not produced by `control_role` is refused
with NOT_SUPPORTED and never reaches the service.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from opengrid.integrations.grid_link.model import LinkStatus

__all__ = [
    "AI_BANK_BASE",
    "AI_BANK_STRIDE",
    "AI_FIXED",
    "AI_TARGET_BASE",
    "AO_FIXED",
    "AO_TARGET_BASE",
    "BI_FIXED",
    "BI_TARGET_BASE",
    "BO_FIXED",
    "BO_TARGET_BASE",
    "NO_L2_CEILING",
    "ControlRole",
    "GridLinkPointMap",
]

RoleName = Literal[
    "CALL_SETPOINT_KW",
    "CALL_DURATION_MIN",
    "CALL_ID",
    "L2_LIMIT_KW",
    "CALL_EXECUTE",
    "CALL_CANCEL",
    "HEARTBEAT",
    "L2_LIMIT_ACTIVE",
    "L2_BLOCK",
]

AO_FIXED: dict[RoleName, int] = {"CALL_SETPOINT_KW": 0, "CALL_DURATION_MIN": 1, "CALL_ID": 2}
AO_TARGET_BASE = 16  # + target: L2_LIMIT_KW
BO_FIXED: dict[RoleName, int] = {"CALL_EXECUTE": 0, "CALL_CANCEL": 1, "HEARTBEAT": 2}
BO_TARGET_BASE = 16  # + 2*target: L2_LIMIT_ACTIVE, + 2*target + 1: L2_BLOCK
AI_FIXED: dict[str, int] = {
    "AVAILABLE_KW": 0,
    "DELIVERED_KW": 1,
    "CALL_STATE": 2,
    "CALL_ID": 3,
    "CALL_REASON": 4,
    "CALL_DELIVERED_KW": 5,
    "SOC_PCT": 6,
    "HEARTBEAT_COUNT": 7,
}
AI_TARGET_BASE = 16  # + target: L2_LIMIT_KW_ECHO
AI_BANK_BASE = 100
AI_BANK_STRIDE = 4  # SOC_PCT, AVAILABLE_KW, DELIVERED_KW, L2_CEILING_KW
BI_FIXED: dict[str, int] = {
    "LINK_HEALTHY": 0,
    "CALL_ACTIVE": 1,
    "ALARM_HEARTBEAT_LOST": 2,
    "ALARM_CALL_REJECTED": 3,
    "ALARM_TELEMETRY_STALE": 4,
    "ALARM_L2_ACTIVE": 5,
    "TOLL_CALLS_ENABLED": 6,
}
BI_TARGET_BASE = 16  # + 2*target: LIMIT_ACTIVE echo, + 2*target + 1: BLOCK echo
#: Reported for an absent L2 limit or an unknown SoC (a sentinel the EMS can test for).
NO_L2_CEILING = -1.0


@dataclass(frozen=True, slots=True)
class ControlRole:
    """What an inbound control point means. `target` is set for the per-target L2 roles."""

    name: RoleName
    target: str | None = None


class GridLinkPointMap:
    """The point list for ONE utility: its L2 targets and banks, in configured order."""

    def __init__(self, targets: list[str], banks: list[str]) -> None:
        self.targets = list(targets)
        self.banks = list(banks)

    # -- inbound -------------------------------------------------------------------------------------

    def analog_output_role(self, index: int) -> ControlRole | None:
        for name, fixed in AO_FIXED.items():
            if index == fixed:
                return ControlRole(name)
        position = index - AO_TARGET_BASE
        if 0 <= position < len(self.targets):
            return ControlRole("L2_LIMIT_KW", self.targets[position])
        return None

    def binary_output_role(self, index: int) -> ControlRole | None:
        for name, fixed in BO_FIXED.items():
            if index == fixed:
                return ControlRole(name)
        offset = index - BO_TARGET_BASE
        if 0 <= offset < 2 * len(self.targets):
            target = self.targets[offset // 2]
            return ControlRole("L2_LIMIT_ACTIVE" if offset % 2 == 0 else "L2_BLOCK", target)
        return None

    # -- outbound ------------------------------------------------------------------------------------

    def analog_inputs(self, status: LinkStatus) -> dict[int, float]:
        """AI index -> engineering value for `status`. CALL_DELIVERED_KW is the call's MEASURED delivery (D-38,
        via the core call status); while it is unmeasured or stale it is absent, so it is served with the
        COMM_LOST flag, never invented."""
        values: dict[int, float] = {
            AI_FIXED["AVAILABLE_KW"]: status.available_kw,
            AI_FIXED["DELIVERED_KW"]: status.delivered_kw,
            AI_FIXED["CALL_STATE"]: float(status.call_phase),
            AI_FIXED["CALL_ID"]: float(status.ems_call_id),
            AI_FIXED["CALL_REASON"]: float(status.call_reason),
            AI_FIXED["SOC_PCT"]: status.soc_pct if status.soc_pct is not None else NO_L2_CEILING,
            AI_FIXED["HEARTBEAT_COUNT"]: float(status.heartbeat_count),
        }
        if status.call_delivered_kw is not None:
            values[AI_FIXED["CALL_DELIVERED_KW"]] = status.call_delivered_kw
        by_target = {t.target: t for t in status.targets}
        for position, name in enumerate(self.targets):
            echo = by_target.get(name)
            limit = echo.limit_kw if echo is not None and echo.limit_active else None
            values[AI_TARGET_BASE + position] = limit if limit is not None else NO_L2_CEILING
        by_bank = {b.bank_id: b for b in status.banks}
        for position, bank_id in enumerate(self.banks):
            base = AI_BANK_BASE + AI_BANK_STRIDE * position
            bank = by_bank.get(bank_id)
            if bank is None:
                continue  # no telemetry: the points keep their RESTART/offline flags
            values[base] = bank.soc_pct if bank.soc_pct is not None else NO_L2_CEILING
            values[base + 1] = bank.available_kw
            values[base + 2] = bank.delivered_kw
            values[base + 3] = bank.l2_ceiling_kw if bank.l2_ceiling_kw is not None else NO_L2_CEILING
        return values

    def analog_input_indices(self) -> list[int]:
        """Every AI index this map serves, in order."""
        fixed = sorted(AI_FIXED.values())
        targets = [AI_TARGET_BASE + i for i in range(len(self.targets))]
        banks = [
            AI_BANK_BASE + AI_BANK_STRIDE * b + k
            for b in range(len(self.banks))
            for k in range(AI_BANK_STRIDE)
        ]
        return fixed + targets + banks

    def binary_inputs(self, status: LinkStatus) -> dict[int, bool]:
        """BI index -> state for `status`."""
        values: dict[int, bool] = {
            BI_FIXED["LINK_HEALTHY"]: status.link_healthy,
            BI_FIXED["CALL_ACTIVE"]: status.call_active,
            BI_FIXED["ALARM_HEARTBEAT_LOST"]: not status.link_healthy,
            BI_FIXED["ALARM_CALL_REJECTED"]: status.call_rejected,
            BI_FIXED["ALARM_TELEMETRY_STALE"]: status.telemetry_stale,
            BI_FIXED["ALARM_L2_ACTIVE"]: status.l2_active,
            BI_FIXED["TOLL_CALLS_ENABLED"]: status.toll_calls_enabled,
        }
        by_target = {t.target: t for t in status.targets}
        for position, name in enumerate(self.targets):
            echo = by_target.get(name)
            values[BI_TARGET_BASE + 2 * position] = echo is not None and echo.limit_active
            values[BI_TARGET_BASE + 2 * position + 1] = echo is not None and echo.block_active
        return values

    def binary_input_indices(self) -> list[int]:
        targets = [BI_TARGET_BASE + i for i in range(2 * len(self.targets))]
        return sorted(BI_FIXED.values()) + targets
