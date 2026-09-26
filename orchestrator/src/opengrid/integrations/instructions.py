"""Utility-instruction change tracking shared by every SCADA protocol adapter (one owner, no copies).

DNP3 and ICCP deliver a bank's utility state as LEVELS (an ESTOP bit, a BLOCK bit, a LIMIT value) that
are re-read on every poll; IEEE 2030.5 delivers DERControl events. The orchestrator's model is an
EVENT (`ScadaUtilityInstruction`) whose end is expressed by `expires_at <= now` (the convention
`opengrid.fleet` and the guardian's L2 port already use, and the one `ogsim.scada` uses to lift an
instruction). `InstructionTracker` turns levels into exactly one instruction per change:

- precedence ESTOP > BLOCK > LIMIT (the most restrictive active level wins, K5);
- a lifted level emits the previous instruction kind with `expires_at == issued_at` (already expired);
- unchanged levels emit nothing, so a 2 s poll never floods the ingest path.

Instruction ids are UUIDv5 over (source, bank, change key), so a reconnect that re-reads the same
level produces the same id and consumers can deduplicate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from opengrid.core.models.mqtt import ScadaUtilityInstruction

__all__ = ["INSTRUCTION_NAMESPACE", "DesiredInstruction", "InstructionTracker", "instruction_uuid"]

#: Fixed namespace for deterministic instruction ids (never change: ids are persisted by consumers).
INSTRUCTION_NAMESPACE = uuid.UUID("6f0c8a52-6a55-5f3e-9d1c-0b2d7f3e1a10")

InstructionKind = Literal["LIMIT", "BLOCK", "ESTOP"]


def instruction_uuid(source: str, bank_id: str, key: str) -> uuid.UUID:
    """Deterministic instruction id for (`source`, `bank_id`, `key`)."""
    return uuid.uuid5(INSTRUCTION_NAMESPACE, f"{source}|{bank_id}|{key}")


@dataclass(frozen=True, slots=True)
class DesiredInstruction:
    """The utility's current requested state for one bank, as read from the protocol."""

    kind: InstructionKind
    limit_kw: float | None = None
    expires_at: datetime | None = None
    key: str | None = None  # protocol identity (e.g. a 2030.5 mRID); defaults to the level itself

    @staticmethod
    def from_levels(*, estop: bool, block: bool, limit_kw: float | None) -> DesiredInstruction | None:
        """Precedence ESTOP > BLOCK > LIMIT; `None` when nothing is active."""
        if estop:
            return DesiredInstruction(kind="ESTOP")
        if block:
            return DesiredInstruction(kind="BLOCK")
        if limit_kw is not None:
            return DesiredInstruction(kind="LIMIT", limit_kw=max(0.0, limit_kw))
        return None

    def change_key(self) -> str:
        if self.key is not None:
            return self.key
        return f"{self.kind}:{self.limit_kw}"


class InstructionTracker:
    """Emits a `ScadaUtilityInstruction` only when a bank's desired instruction changes."""

    def __init__(self, *, source: str, issued_by: str) -> None:
        self._source = source
        self._issued_by = issued_by
        self._current: dict[str, DesiredInstruction] = {}
        self._epoch: dict[str, int] = {}

    def active(self, bank_id: str) -> DesiredInstruction | None:
        return self._current.get(bank_id)

    def update(
        self, bank_id: str, desired: DesiredInstruction | None, *, now: datetime
    ) -> ScadaUtilityInstruction | None:
        previous = self._current.get(bank_id)
        if desired == previous:
            return None
        epoch = self._epoch.get(bank_id, 0) + 1
        self._epoch[bank_id] = epoch
        if desired is None:
            if previous is None:  # unreachable: equal values returned above
                return None
            del self._current[bank_id]
            return ScadaUtilityInstruction(
                instruction_id=instruction_uuid(
                    self._source, bank_id, f"lift:{epoch}:{previous.change_key()}"
                ),
                bank_id=bank_id,
                kind=previous.kind,
                limit_kw=previous.limit_kw,
                issued_at=now,
                expires_at=now,
                issued_by=self._issued_by,
            )
        self._current[bank_id] = desired
        return ScadaUtilityInstruction(
            instruction_id=instruction_uuid(self._source, bank_id, f"set:{epoch}:{desired.change_key()}"),
            bank_id=bank_id,
            kind=desired.kind,
            limit_kw=desired.limit_kw if desired.kind == "LIMIT" else None,
            issued_at=now,
            expires_at=desired.expires_at,
            issued_by=self._issued_by,
        )
