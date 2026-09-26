"""DNP3 point map: which outstation index carries which bank signal (protocol-adapters.md S3.2).

DNP3 analog inputs are transmitted as scaled integers (g30v1/g32v1, 32-bit), so every analog point
carries an engineering scale: `engineering = raw * scale + offset`. The utility supplies the real point
list at onboarding; `standard_layout` is the layout OpenGrid proposes to utilities (and the one the ogsim
DNP3 outstation serves), so the bilateral point list is short to agree:

    per bank b (0-based position in the configured bank list):
      AI  16*b + 0   APPARENT_POWER_KVA   0.1 kVA/count
               1   REAL_POWER_KW        0.1 kW
               2   VOLTAGE_PU           0.0001 pu
               3   CURRENT_A            0.1 A
               4-6 VOLTAGE_{A,B,C}_PU   0.0001 pu
               7-9 CURRENT_A_PHASE_{A,B,C} 0.1 A
               10  FREQUENCY_HZ         0.001 Hz
               11  THD_V_PCT            0.01 %
               12  THD_I_PCT            0.01 %
               13  UTILITY_LIMIT_KW     0.1 kW  (utility discharge ceiling; valid when BI LIMIT_ACTIVE)
      BI   8*b + 0   COMM_OK              RTU <-> bank meter comms healthy
               1   BREAKER_CLOSED
               2   UTILITY_BLOCK
               3   UTILITY_ESTOP
               4   UTILITY_LIMIT_ACTIVE
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opengrid.integrations.bank_points import MappedPoint

__all__ = [
    "ANALOG_STRIDE",
    "BINARY_STRIDE",
    "AnalogPoint",
    "BinaryPoint",
    "BinaryRole",
    "Dnp3PointMap",
    "standard_layout",
]

BankSignalName = Literal[
    "APPARENT_POWER_KVA",
    "REAL_POWER_KW",
    "VOLTAGE_PU",
    "CURRENT_A",
    "VOLTAGE_A_PU",
    "VOLTAGE_B_PU",
    "VOLTAGE_C_PU",
    "CURRENT_A_PHASE_A",
    "CURRENT_A_PHASE_B",
    "CURRENT_A_PHASE_C",
    "FREQUENCY_HZ",
    "THD_V_PCT",
    "THD_I_PCT",
]
AnalogRole = BankSignalName | Literal["UTILITY_LIMIT_KW"]
BinaryRole = Literal["COMM_OK", "BREAKER_CLOSED", "UTILITY_BLOCK", "UTILITY_ESTOP", "UTILITY_LIMIT_ACTIVE"]

ANALOG_STRIDE = 16
BINARY_STRIDE = 8

_STANDARD_ANALOGS: tuple[tuple[int, AnalogRole, float], ...] = (
    (0, "APPARENT_POWER_KVA", 0.1),
    (1, "REAL_POWER_KW", 0.1),
    (2, "VOLTAGE_PU", 0.0001),
    (3, "CURRENT_A", 0.1),
    (4, "VOLTAGE_A_PU", 0.0001),
    (5, "VOLTAGE_B_PU", 0.0001),
    (6, "VOLTAGE_C_PU", 0.0001),
    (7, "CURRENT_A_PHASE_A", 0.1),
    (8, "CURRENT_A_PHASE_B", 0.1),
    (9, "CURRENT_A_PHASE_C", 0.1),
    (10, "FREQUENCY_HZ", 0.001),
    (11, "THD_V_PCT", 0.01),
    (12, "THD_I_PCT", 0.01),
    (13, "UTILITY_LIMIT_KW", 0.1),
)
_STANDARD_BINARIES: tuple[tuple[int, BinaryRole], ...] = (
    (0, "COMM_OK"),
    (1, "BREAKER_CLOSED"),
    (2, "UTILITY_BLOCK"),
    (3, "UTILITY_ESTOP"),
    (4, "UTILITY_LIMIT_ACTIVE"),
)


class AnalogPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, le=65535)
    bank_id: str
    role: AnalogRole
    scale: float = 1.0
    offset: float = 0.0

    def mapped(self) -> MappedPoint:
        return MappedPoint(bank_id=self.bank_id, role=self.role, scale=self.scale, offset=self.offset)


class BinaryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=0, le=65535)
    bank_id: str
    role: BinaryRole
    invert: bool = False

    def mapped(self) -> MappedPoint:
        return MappedPoint(bank_id=self.bank_id, role=self.role, invert=self.invert)


class Dnp3PointMap(BaseModel):
    """The complete point list for ONE outstation (one DNP3 association)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    analogs: list[AnalogPoint] = []
    binaries: list[BinaryPoint] = []

    @model_validator(mode="after")
    def _unique_indices(self) -> Dnp3PointMap:
        for kind, points in (("analog", self.analogs), ("binary", self.binaries)):
            indices = [p.index for p in points]
            if len(indices) != len(set(indices)):
                raise ValueError(f"duplicate {kind} input index in DNP3 point map")
        return self

    def bank_ids(self) -> list[str]:
        seen: dict[str, None] = {}
        for bank_id in [*(p.bank_id for p in self.analogs), *(p.bank_id for p in self.binaries)]:
            seen.setdefault(bank_id, None)
        return list(seen)


def standard_layout(bank_ids: list[str]) -> Dnp3PointMap:
    """OpenGrid's proposed per-bank layout (module docstring) for `bank_ids`, in order."""
    analogs = [
        AnalogPoint(index=ANALOG_STRIDE * b + off, bank_id=bank_id, role=role, scale=scale)
        for b, bank_id in enumerate(bank_ids)
        for off, role, scale in _STANDARD_ANALOGS
    ]
    binaries = [
        BinaryPoint(index=BINARY_STRIDE * b + off, bank_id=bank_id, role=role)
        for b, bank_id in enumerate(bank_ids)
        for off, role in _STANDARD_BINARIES
    ]
    return Dnp3PointMap(analogs=analogs, binaries=binaries)
