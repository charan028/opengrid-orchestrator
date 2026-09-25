"""ogsim.fleet.stop -- retained stop handling (interfaces/mqtt/stop.schema.json).

Scopes: `fleet`, `zone/<zone>`, `bank/<bank_id>`. A `StopRegistry` mirrors
the retained MQTT state: `ENGAGE` sets a scope active, `RELEASE` (or an
empty retained payload on the same topic) clears it. Because it is a plain
in-memory mapping queried fresh on every tick, a hub added *after* a stop
was already engaged sees it immediately -- there is no separate "did I
already process this event" flag to miss, which is what makes a late
join/reconnect behave the same as always having been subscribed.

Ramping ONLY affects the commanded (market) setpoint, never a hub's home
load: engaging a stop must not force discharge below the reserve floor,
it just removes market dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StopRegistry:
    fleet_stopped: bool = False
    zones_stopped: set[str] = field(default_factory=set)
    banks_stopped: set[str] = field(default_factory=set)

    def engage(self, scope: str, scope_id: str | None) -> None:
        if scope == "fleet":
            self.fleet_stopped = True
        elif scope == "zone" and scope_id:
            self.zones_stopped.add(scope_id)
        elif scope == "bank" and scope_id:
            self.banks_stopped.add(scope_id)

    def release(self, scope: str, scope_id: str | None) -> None:
        if scope == "fleet":
            self.fleet_stopped = False
        elif scope == "zone" and scope_id:
            self.zones_stopped.discard(scope_id)
        elif scope == "bank" and scope_id:
            self.banks_stopped.discard(scope_id)

    def apply_stop_event(self, action: str, scope: str, scope_id: str | None) -> None:
        if action == "ENGAGE":
            self.engage(scope, scope_id)
        elif action == "RELEASE":
            self.release(scope, scope_id)

    def is_stopped(self, zone: str, bank_id: str) -> bool:
        return self.fleet_stopped or zone in self.zones_stopped or bank_id in self.banks_stopped


def ramp_toward_zero(current_p_kw: float, dt_s: float, ramp_time_s: float, p_kw_limit: float) -> float:
    """Ramps a commanded setpoint toward 0 over `ramp_time_s`, never
    overshooting past 0. `p_kw_limit` bounds the per-tick step for a hub
    that was already at/near its max so ramp_time_s is respected even at
    a coarse tick rate."""
    if ramp_time_s <= 0 or current_p_kw == 0.0:
        return 0.0
    max_step = p_kw_limit * (dt_s / ramp_time_s)
    if current_p_kw > 0:
        return max(0.0, current_p_kw - max_step)
    return min(0.0, current_p_kw + max_step)
