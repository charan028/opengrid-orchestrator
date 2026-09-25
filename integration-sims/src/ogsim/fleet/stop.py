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

`verify_stop_event` implements crypto.md §2.3's signing rule: only the
safestop key may sign `action="ENGAGE"`; `action="RELEASE"` must be signed
by the guardian key (Tier-2 approved). A StopEvent that fails this --
wrong key for the action, or a bad/missing signature -- is rejected and
must never reach `StopRegistry.apply_stop_event`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ogsim.common.crypto import verify_signature

StopRejectReason = str  # currently just "BAD_SIGNATURE"; callers log rejects themselves

# stop.schema.json fields that are signed over (JCS bytes), i.e. everything
# except `key_id`/`signature` (crypto.md §2.3).
_STOP_SIGNED_FIELDS = (
    "stop_id",
    "scope",
    "scope_id",
    "action",
    "reason",
    "issued_by",
    "issued_at",
    "approver_ref",
)


def _stop_signing_fields(event: dict[str, Any]) -> dict[str, Any]:
    return {k: event[k] for k in _STOP_SIGNED_FIELDS if k in event}


def verify_stop_event(
    event: dict[str, Any],
    safestop_public_key: Ed25519PublicKey,
    guardian_public_key: Ed25519PublicKey,
) -> StopRejectReason | None:
    """Verifies `event` per crypto.md §2.3. Returns `None` if the signature
    is valid for the key required by `event["action"]`, else a reject
    reason. `action="ENGAGE"` must verify against `safestop_public_key`;
    any other action (i.e. "RELEASE") must verify against
    `guardian_public_key` -- a RELEASE signed by the safestop key is
    rejected, and so is an ENGAGE signed by the guardian key."""
    key = safestop_public_key if event.get("action") == "ENGAGE" else guardian_public_key
    signing_fields = _stop_signing_fields(event)
    if not verify_signature(key, signing_fields, event.get("signature")):
        return "BAD_SIGNATURE"
    return None


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
