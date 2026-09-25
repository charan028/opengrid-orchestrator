"""ogsim.fleet.lease -- per-hub lease tracking and local autonomy (02b §4.3/§4.4).

A hub holds a lease (`lease.schema.json`, retained on `<root>/lease/<hub_id>`).
On expiry it holds its last setpoint for `lease_hold_after_expiry_s` (a
short grace period so a momentary renewal gap doesn't visibly disrupt
dispatch), then falls to local autonomy: self-consumption/home-load
following only, no market setpoint, until a fresh valid batch arrives.
"""

from __future__ import annotations

from dataclasses import dataclass

from ogsim.fleet.commands import parse_rfc3339


@dataclass(frozen=True)
class LeaseState:
    expires_at: float
    holding: bool
    local_autonomy: bool


def lease_expiry_from_message(expires_at: str) -> float:
    """Converts a `lease.schema.json`/inline-batch `expires_at` RFC3339
    string to a Unix-epoch float."""
    return parse_rfc3339(expires_at).timestamp()


class HoldTracker:
    """Per-hub lease state machine: leased -> (on expiry) holding for
    `hold_after_expiry_s` -> local autonomy, until a fresh lease renews it.
    Stateful so "hold briefly, then autonomy" doesn't depend on tick rate.
    """

    def __init__(self, hold_after_expiry_s: float) -> None:
        self._hold_after_expiry_s = hold_after_expiry_s
        self._hold_started_at: dict[str, float] = {}

    def state_for(self, hub_id: str, expires_at: float, now: float) -> LeaseState:
        if expires_at == 0.0:
            return LeaseState(expires_at, holding=False, local_autonomy=True)
        if now < expires_at:
            self._hold_started_at.pop(hub_id, None)
            return LeaseState(expires_at, holding=False, local_autonomy=False)
        started = self._hold_started_at.setdefault(hub_id, expires_at)
        if now < started + self._hold_after_expiry_s:
            return LeaseState(expires_at, holding=True, local_autonomy=False)
        return LeaseState(expires_at, holding=False, local_autonomy=True)

    def renew(self, hub_id: str) -> None:
        self._hold_started_at.pop(hub_id, None)
