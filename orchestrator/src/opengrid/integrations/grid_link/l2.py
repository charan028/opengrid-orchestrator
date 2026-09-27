"""L2 LIMIT/BLOCK levels received over the grid link, folded per bank (grid-link.md S5.2; K5/G-15).

The utility sets LEVELS per target (a bank or a zone): LIMIT on/off with a kW value, BLOCK on/off. The
orchestrator's model is one `ScadaUtilityInstruction` per bank change. This book:

- folds every target covering a bank into ONE desired instruction: BLOCK if any covering target blocks,
  else the LOWEST active limit (the most restrictive wins, K5);
- a LIMIT switched on before any value was written is a 0 kW limit (fail closed);
- turns changes into instructions through the shared `InstructionTracker` (one owner: deterministic
  ids, lift = `expires_at == issued_at`);
- never lifts anything on its own: when the link goes quiet the levels stay as last received.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime

from opengrid.core.models.mqtt import ScadaUtilityInstruction
from opengrid.integrations.grid_link.config import L2TargetSettings
from opengrid.integrations.grid_link.model import TargetStatus
from opengrid.integrations.grid_link.ports import BanksOfZone
from opengrid.integrations.instructions import DesiredInstruction, InstructionTracker

__all__ = ["L2Book", "TargetLevels", "fold_bank_levels"]


@dataclass(frozen=True, slots=True)
class TargetLevels:
    limit_active: bool = False
    limit_kw: float | None = None
    block_active: bool = False


def fold_bank_levels(covering: Iterable[TargetLevels]) -> DesiredInstruction | None:
    """The one desired instruction for a bank covered by `covering` targets (module docstring). Pure."""
    block = False
    limits: list[float] = []
    for levels in covering:
        block = block or levels.block_active
        if levels.limit_active:
            limits.append(levels.limit_kw if levels.limit_kw is not None else 0.0)
    return DesiredInstruction.from_levels(estop=False, block=block, limit_kw=min(limits) if limits else None)


class L2Book:
    """Per-utility L2 state: target levels -> per-bank instructions (change-only)."""

    def __init__(
        self,
        targets: list[L2TargetSettings],
        *,
        source: str,
        issued_by: str,
        banks_of_zone: BanksOfZone,
    ) -> None:
        self._targets = {t.name: t for t in targets}
        self._order = [t.name for t in targets]
        self._levels: dict[str, TargetLevels] = {name: TargetLevels() for name in self._order}
        self._banks_of_zone = banks_of_zone
        self._tracker = InstructionTracker(source=source, issued_by=issued_by)
        self._touched: set[str] = set()

    def knows(self, target: str) -> bool:
        return target in self._targets

    def set_limit_value(self, target: str, limit_kw: float) -> None:
        current = self._levels[target]
        self._levels[target] = TargetLevels(current.limit_active, max(0.0, limit_kw), current.block_active)

    def set_limit_active(self, target: str, active: bool) -> None:
        current = self._levels[target]
        self._levels[target] = TargetLevels(active, current.limit_kw, current.block_active)

    def set_block(self, target: str, active: bool) -> None:
        current = self._levels[target]
        self._levels[target] = TargetLevels(current.limit_active, current.limit_kw, active)

    def banks_of(self, target: str) -> list[str]:
        settings = self._targets[target]
        if settings.bank_id is not None:
            return [settings.bank_id]
        return self._banks_of_zone(settings.zone or "")

    def _coverage(self) -> Mapping[str, list[TargetLevels]]:
        coverage: dict[str, list[TargetLevels]] = {bank: [] for bank in self._touched}
        for name in self._order:
            for bank in self.banks_of(name):
                coverage.setdefault(bank, []).append(self._levels[name])
        return coverage

    def desired(self) -> dict[str, DesiredInstruction | None]:
        """Desired instruction per bank ever covered (None = nothing active)."""
        return {bank: fold_bank_levels(levels) for bank, levels in self._coverage().items()}

    def instructions(self, now: datetime) -> list[ScadaUtilityInstruction]:
        """Instructions for every bank whose desired state changed since the last call."""
        out: list[ScadaUtilityInstruction] = []
        for bank, desired in sorted(self.desired().items()):
            self._touched.add(bank)
            instruction = self._tracker.update(bank, desired, now=now)
            if instruction is not None:
                out.append(instruction)
        return out

    def ceiling_kw(self, bank_id: str) -> float | None:
        """The L2 discharge ceiling this link currently asserts on `bank_id` (0 for BLOCK), or None."""
        active = self._tracker.active(bank_id)
        if active is None:
            return None
        return 0.0 if active.kind in ("BLOCK", "ESTOP") else active.limit_kw

    def target_status(self) -> tuple[TargetStatus, ...]:
        return tuple(
            TargetStatus(
                target=name,
                limit_active=self._levels[name].limit_active,
                block_active=self._levels[name].block_active,
                limit_kw=self._levels[name].limit_kw if self._levels[name].limit_active else None,
            )
            for name in self._order
        )
