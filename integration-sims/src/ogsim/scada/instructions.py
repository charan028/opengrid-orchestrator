"""ogsim.scada.instructions -- rule-based auto utility instruction (02b §4.2).

A simple DMS-like rule: if a bank's kVA loading exceeds its rating for
`overload_consecutive_samples` consecutive readings, auto-issue a LIMIT
instruction (`scada_utility_instruction.schema.json`). Scenario-driven
`utility_instruction` anomalies issue LIMIT/BLOCK/ESTOP directly and
bypass this counter.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class OverloadRule:
    threshold_samples: int

    _consecutive: dict[str, int] = field(default_factory=dict)

    def observe(self, bank_id: str, kva_load: float, kva_rating: float) -> bool:
        """Records one sample; returns True the instant the rule should fire
        (i.e. on the sample that reaches `threshold_samples`), then resets
        so it does not re-fire every tick while still overloaded."""
        if kva_load > kva_rating:
            count = self._consecutive.get(bank_id, 0) + 1
            self._consecutive[bank_id] = count
            if count >= self.threshold_samples:
                self._consecutive[bank_id] = 0
                return True
            return False
        self._consecutive[bank_id] = 0
        return False


def limit_instruction(
    instruction_id: str, bank_id: str, limit_kw: float, issued_at: str
) -> dict[str, object]:
    return {
        "instruction_id": instruction_id,
        "bank_id": bank_id,
        "kind": "LIMIT",
        "limit_kw": limit_kw,
        "issued_at": issued_at,
        "expires_at": None,
        "issued_by": "SCADA_AUTO_RULE",
    }
