"""ICCP / TASE.2 (IEC 60870-6-503/802) bilateral table and data-value mapping (protocol-adapters.md S5).

A TASE.2 link is governed by a BILATERAL TABLE agreed between the two control centres: the association
parameters (AP titles, domains, TASE.2 version) and, per data value, its name, scope (VCC or ICC), type
and access. This module holds that agreement as configuration and maps each agreed data value onto a
bank point role (`opengrid.integrations.bank_points`).

TASE.2 types used here (IEC 60870-6-802 S8.1):

- `Data_Real`, `Data_RealQ`, `Data_RealQTimeTag`: IEEE float32 engineering values (no scaling on the
  wire; `scale`/`offset` exist for unit conversion, e.g. MVA -> kVA = 1000);
- `Data_State`, `Data_StateQ`, `Data_StateQTimeTag`: a 2-bit state, 0 = BETWEEN, 1 = OFF/TRIPPED,
  2 = ON/CLOSED, 3 = INVALID (BETWEEN and INVALID are reported as "not online").

Quality (the `Q` flags): Validity VALID -> good; HELD -> stale; SUSPECT -> stale; NOTVALID ->
comm_fail. CurrentSource (TELEMETERED/CALCULATED/ENTERED/ESTIMATED) is kept for audit only.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opengrid.integrations.bank_points import ANALOG_ROLES, BINARY_ROLES, MappedPoint, Quality

__all__ = [
    "BilateralTable",
    "IccpDataValue",
    "iccp_quality",
    "iccp_state_is_on",
    "standard_bilateral_points",
]

Tase2Type = Literal[
    "Data_Real",
    "Data_RealQ",
    "Data_RealQTimeTag",
    "Data_State",
    "Data_StateQ",
    "Data_StateQTimeTag",
]
_REAL_TYPES = frozenset({"Data_Real", "Data_RealQ", "Data_RealQTimeTag"})

STATE_BETWEEN = 0
STATE_OFF = 1
STATE_ON = 2
STATE_INVALID = 3


class IccpDataValue(BaseModel):
    """One agreed data value of the bilateral table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=32)  # MMS identifier limit
    scope: Literal["VCC", "ICC"] = "ICC"
    type: Tase2Type = "Data_RealQ"
    bank_id: str
    role: str
    scale: float = 1.0
    offset: float = 0.0

    @model_validator(mode="after")
    def _role_matches_type(self) -> IccpDataValue:
        is_real = self.type in _REAL_TYPES
        if is_real and self.role not in ANALOG_ROLES:
            raise ValueError(f"{self.name}: a real-valued data value needs an analog role, got {self.role}")
        if not is_real and self.role not in BINARY_ROLES:
            raise ValueError(f"{self.name}: a state data value needs a status role, got {self.role}")
        return self

    @property
    def is_real(self) -> bool:
        return self.type in _REAL_TYPES

    def mapped(self) -> MappedPoint:
        return MappedPoint(bank_id=self.bank_id, role=self.role, scale=self.scale, offset=self.offset)


class BilateralTable(BaseModel):
    """The bilateral agreement for ONE TASE.2 association."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    bilateral_table_id: str
    tase2_version: Literal["1996-08", "2000-08"] = "2000-08"
    local_domain: str  # our ICC domain name, as registered with the utility
    remote_domain: str  # the utility's domain
    calling_ap_title: str = "1.3.9999.1"  # our AP title (object identifier), set at onboarding
    called_ap_title: str = "1.3.9999.2"
    dataset_name: str = "DS_OPENGRID_BANKS"
    transfer_interval_s: float = Field(default=2.0, gt=0)
    report_by_exception: bool = False
    data_values: list[IccpDataValue] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_names(self) -> BilateralTable:
        names = [v.name for v in self.data_values]
        if len(names) != len(set(names)):
            raise ValueError("duplicate data value name in bilateral table")
        return self


_VALIDITY_QUALITY: dict[str, Quality] = {"VALID": "good", "HELD": "stale", "SUSPECT": "stale"}


def iccp_quality(validity: str) -> Quality:
    return _VALIDITY_QUALITY.get(validity.upper(), "comm_fail")


def iccp_state_is_on(state: int) -> tuple[bool, bool]:
    """(value, online) for a TASE.2 2-bit state."""
    if state == STATE_ON:
        return True, True
    if state == STATE_OFF:
        return False, True
    return False, False


_STANDARD_REALS: tuple[tuple[str, str], ...] = (
    ("KVA", "APPARENT_POWER_KVA"),
    ("KW", "REAL_POWER_KW"),
    ("VPU", "VOLTAGE_PU"),
    ("AMP", "CURRENT_A"),
    ("VA_PU", "VOLTAGE_A_PU"),
    ("VB_PU", "VOLTAGE_B_PU"),
    ("VC_PU", "VOLTAGE_C_PU"),
    ("IA", "CURRENT_A_PHASE_A"),
    ("IB", "CURRENT_A_PHASE_B"),
    ("IC", "CURRENT_A_PHASE_C"),
    ("HZ", "FREQUENCY_HZ"),
    ("THDV", "THD_V_PCT"),
    ("THDI", "THD_I_PCT"),
    ("LIMKW", "UTILITY_LIMIT_KW"),
)
_STANDARD_STATES: tuple[tuple[str, str], ...] = (
    ("COMM", "COMM_OK"),
    ("BRKR", "BREAKER_CLOSED"),
    ("BLOCK", "UTILITY_BLOCK"),
    ("ESTOP", "UTILITY_ESTOP"),
    ("LIMACT", "UTILITY_LIMIT_ACTIVE"),
)


def standard_bilateral_points(bank_ids: list[str]) -> list[IccpDataValue]:
    """OpenGrid's proposed data-value naming: `<BANK>_<SUFFIX>`, e.g. `BANK_000_KVA`."""
    values: list[IccpDataValue] = []
    for bank_id in bank_ids:
        prefix = bank_id.upper().replace("-", "_")
        values.extend(
            IccpDataValue(name=f"{prefix}_{suffix}", type="Data_RealQ", bank_id=bank_id, role=role)
            for suffix, role in _STANDARD_REALS
        )
        values.extend(
            IccpDataValue(name=f"{prefix}_{suffix}", type="Data_StateQ", bank_id=bank_id, role=role)
            for suffix, role in _STANDARD_STATES
        )
    return values
