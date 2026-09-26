"""Point-oriented SCADA backends (DNP3, ICCP) share one mapping from "latest value of each mapped point"
to the orchestrator's bank signals and utility instructions (one owner; each protocol only decodes
its own wire values and quality flags into `update_analog` / `update_binary`).

Roles (the point list agreed with the utility):

- analog roles: every `ScadaBankSignal.signal` name, plus `UTILITY_LIMIT_KW` (the utility's discharge
  ceiling, valid while the `UTILITY_LIMIT_ACTIVE` status is on);
- status roles: `COMM_OK`, `BREAKER_CLOSED`, `UTILITY_BLOCK`, `UTILITY_ESTOP`, `UTILITY_LIMIT_ACTIVE`.

Readings with an unusable quality (`missing`, `comm_fail`, `stale`) are withheld unless
`deliver_bad_quality`: today's consumers (fleet twin, guardian G-03 via og.feed_obs) use `value` without
checking `quality`, so silence -- and the existing staleness handling -- is the safe report.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from opengrid.core.models.mqtt import ScadaBankSignal, ScadaUtilityInstruction
from opengrid.integrations.instructions import DesiredInstruction, InstructionTracker

__all__ = [
    "ANALOG_ROLES",
    "BINARY_ROLES",
    "SIGNAL_UNITS",
    "BankPointAssembler",
    "MappedPoint",
    "Quality",
]

Quality = Literal["good", "stale", "missing", "out_of_range", "comm_fail"]

#: Unit of each `ScadaBankSignal.signal` (matches interfaces/mqtt/scada_bank_signal.schema.json).
SIGNAL_UNITS: dict[str, Literal["kVA", "kW", "pu", "A", "Hz", "%"]] = {
    "APPARENT_POWER_KVA": "kVA",
    "REAL_POWER_KW": "kW",
    "VOLTAGE_PU": "pu",
    "CURRENT_A": "A",
    "VOLTAGE_A_PU": "pu",
    "VOLTAGE_B_PU": "pu",
    "VOLTAGE_C_PU": "pu",
    "CURRENT_A_PHASE_A": "A",
    "CURRENT_A_PHASE_B": "A",
    "CURRENT_A_PHASE_C": "A",
    "FREQUENCY_HZ": "Hz",
    "THD_V_PCT": "%",
    "THD_I_PCT": "%",
}
ANALOG_ROLES = frozenset({*SIGNAL_UNITS, "UTILITY_LIMIT_KW"})
BINARY_ROLES = frozenset(
    {"COMM_OK", "BREAKER_CLOSED", "UTILITY_BLOCK", "UTILITY_ESTOP", "UTILITY_LIMIT_ACTIVE"}
)
_WITHHELD: frozenset[Quality] = frozenset({"missing", "comm_fail", "stale"})


@dataclass(frozen=True, slots=True)
class MappedPoint:
    """What one protocol point means: `engineering = raw * scale + offset`; `invert` for statuses."""

    bank_id: str
    role: str
    scale: float = 1.0
    offset: float = 0.0
    invert: bool = False

    def __post_init__(self) -> None:
        if self.role not in ANALOG_ROLES and self.role not in BINARY_ROLES:
            raise ValueError(f"unknown SCADA point role {self.role!r}")

    @property
    def is_analog(self) -> bool:
        return self.role in ANALOG_ROLES


@dataclass
class _Latest:
    analogs: dict[tuple[str, str], tuple[float, Quality]] = field(default_factory=dict)
    statuses: dict[tuple[str, str], tuple[bool, bool]] = field(default_factory=dict)  # (value, online)


class BankPointAssembler:
    """Latest value per mapped point -> bank signals and (change-only) utility instructions."""

    def __init__(
        self,
        points: Mapping[Hashable, MappedPoint],
        *,
        source: str,
        issued_by: str,
        breaker_open_blocks: bool = True,
        deliver_bad_quality: bool = False,
    ) -> None:
        self._points = dict(points)
        self._latest = _Latest()
        self._tracker = InstructionTracker(source=source, issued_by=issued_by)
        self._breaker_open_blocks = breaker_open_blocks
        self._deliver_bad_quality = deliver_bad_quality
        seen: dict[str, None] = {}
        for p in self._points.values():
            seen.setdefault(p.bank_id, None)
        self.bank_ids = list(seen)

    def point(self, key: Hashable) -> MappedPoint | None:
        return self._points.get(key)

    def update_analog(self, key: Hashable, raw: float, quality: Quality) -> None:
        point = self._points.get(key)
        if point is not None and point.is_analog:
            self._latest.analogs[(point.bank_id, point.role)] = (point.scale * raw + point.offset, quality)

    def update_binary(self, key: Hashable, value: bool, *, online: bool) -> None:
        point = self._points.get(key)
        if point is not None and not point.is_analog:
            self._latest.statuses[(point.bank_id, point.role)] = (value != point.invert, online)

    def _status(self, bank_id: str, role: str) -> tuple[bool, bool] | None:
        return self._latest.statuses.get((bank_id, role))

    def _on(self, bank_id: str, role: str) -> bool:
        status = self._status(bank_id, role)
        return status is not None and status[0] and status[1]

    def signals(self, now: datetime) -> list[ScadaBankSignal]:
        signals: list[ScadaBankSignal] = []
        for (bank_id, role), (value, quality) in self._latest.analogs.items():
            if role not in SIGNAL_UNITS:
                continue
            comm = self._status(bank_id, "COMM_OK")
            if comm is not None and not (comm[0] and comm[1]):
                quality = "comm_fail"
            if quality in _WITHHELD and not self._deliver_bad_quality:
                continue
            signals.append(
                ScadaBankSignal.model_validate(
                    {
                        "bank_id": bank_id,
                        "signal": role,
                        "value": round(value, 6),
                        "unit": SIGNAL_UNITS[role],
                        "quality": quality,
                        "ts": now,
                    }
                )
            )
        return signals

    def desired(self, bank_id: str) -> DesiredInstruction | None:
        limit_kw: float | None = None
        if self._on(bank_id, "UTILITY_LIMIT_ACTIVE"):
            limit = self._latest.analogs.get((bank_id, "UTILITY_LIMIT_KW"))
            # a limit asserted with an unusable value is taken as the most restrictive one (fail closed)
            limit_kw = round(limit[0], 3) if limit is not None and limit[1] not in _WITHHELD else 0.0
        breaker = self._status(bank_id, "BREAKER_CLOSED")
        breaker_open = breaker is not None and breaker[1] and not breaker[0]
        return DesiredInstruction.from_levels(
            estop=self._on(bank_id, "UTILITY_ESTOP"),
            block=self._on(bank_id, "UTILITY_BLOCK") or (self._breaker_open_blocks and breaker_open),
            limit_kw=limit_kw,
        )

    def instructions(self, now: datetime) -> list[ScadaUtilityInstruction]:
        out: list[ScadaUtilityInstruction] = []
        for bank_id in self.bank_ids:
            instruction = self._tracker.update(bank_id, self.desired(bank_id), now=now)
            if instruction is not None:
                out.append(instruction)
        return out
