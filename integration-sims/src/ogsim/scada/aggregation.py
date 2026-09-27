"""ogsim.scada.aggregation -- per-bank kVA aggregation (02b §4.2/§5.1).

Bank load = sum of that bank's hub telemetry `p_kw` (+charging adds feeder
demand, -discharging offsets it) plus the bank's background load. kVA is
approximated from real kW at a fixed assumed power factor (documented
constant, not a modeled quantity in this MVP).
"""

from __future__ import annotations

from dataclasses import dataclass, field

ASSUMED_POWER_FACTOR = 0.98


@dataclass
class BankTelemetryBuffer:
    """Latest known `p_kw` per hub for one bank, updated as telemetry arrives."""

    hub_p_kw: dict[str, float] = field(default_factory=dict)

    def update(self, hub_id: str, p_kw: float) -> None:
        self.hub_p_kw[hub_id] = p_kw

    def drop_hub(self, hub_id: str) -> None:
        self.hub_p_kw.pop(hub_id, None)

    def net_battery_kw(self) -> float:
        return sum(self.hub_p_kw.values())


def bank_load_kw(battery_net_kw: float, background_kw: float) -> float:
    """Net real power the bank presents to the feeder (kW)."""
    return background_kw + battery_net_kw


def kw_to_kva(real_power_kw: float, power_factor: float = ASSUMED_POWER_FACTOR) -> float:
    """Approximates apparent power (kVA) from real power (kW) at a fixed pf."""
    if power_factor <= 0:
        raise ValueError("power_factor must be > 0")
    return abs(real_power_kw) / power_factor


def kva_to_kw(apparent_power_kva: float, power_factor: float = ASSUMED_POWER_FACTOR) -> float:
    """Inverse of `kw_to_kva`: the (importing, positive) real power whose apparent power at a fixed pf
    is `apparent_power_kva`."""
    if power_factor <= 0:
        raise ValueError("power_factor must be > 0")
    return abs(apparent_power_kva) * power_factor
