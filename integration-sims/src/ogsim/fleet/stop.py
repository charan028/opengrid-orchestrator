"""ogsim.fleet.stop -- retained stop handling (interfaces/mqtt/stop.schema.json).

Scopes: `fleet`, `zone/<zone>`, `bank/<bank_id>`. A `StopRegistry` mirrors
the retained MQTT state: a verified `ENGAGE` sets a scope active, a verified
`RELEASE` clears it. Nothing else changes stop state: an empty or non-object
retained payload is broker housekeeping (clearing the retained message) and is
ignored (K8; `ogsim.fleet.__main__.handle_stop_message`). The guardian publishes
its RELEASE on the ENGAGE's own topic, so the retained state of that topic
becomes the RELEASE and a late joiner never sees the stale ENGAGE. Because it is a plain
in-memory mapping queried fresh on every tick, a hub added *after* a stop
was already engaged sees it immediately -- there is no separate "did I
already process this event" flag to miss, which is what makes a late
join/reconnect behave the same as always having been subscribed.

Ramping ONLY affects the commanded (market) setpoint, never a hub's home
load: engaging a stop must not force discharge below the reserve floor,
it just removes market dispatch.

`verify_stop_event` implements crypto.md §2.3's signing rule: only the
safestop key may sign `action="ENGAGE"`; `action="RELEASE"` must be signed
by the guardian key (Tier-2 approved) and name a second approver distinct
from the requester (`approver_ref` != `issued_by`). A StopEvent that fails
this -- wrong key for the action, a bad/missing signature, an unknown action,
or a RELEASE without two people on it -- is rejected and must never reach
`StopRegistry.apply_verified_event`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ogsim.common.crypto import verify_signature
from ogsim.fleet.commands import parse_rfc3339

#: "BAD_SIGNATURE" | "UNKNOWN_ACTION" | "NOT_TIER2_APPROVED"; callers log rejects themselves.
StopRejectReason = str

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
    rejected, and so is an ENGAGE signed by the guardian key. A RELEASE must
    also carry a Tier-2 second approver distinct from its requester."""
    action = event.get("action")
    if action not in ("ENGAGE", "RELEASE"):
        return "UNKNOWN_ACTION"
    key = safestop_public_key if action == "ENGAGE" else guardian_public_key
    signing_fields = _stop_signing_fields(event)
    if not verify_signature(key, signing_fields, event.get("signature")):
        return "BAD_SIGNATURE"
    if action == "RELEASE":
        approver = str(event.get("approver_ref") or "").strip()
        requester = str(event.get("issued_by") or "").strip()
        if not approver or approver.casefold() == requester.casefold():
            return "NOT_TIER2_APPROVED"
    return None


ScopeKey = tuple[str, str | None]
_MANUAL_STOP_ID = "manual"


def _scope_key(scope: str, scope_id: str | None) -> ScopeKey | None:
    if scope == "fleet":
        return ("fleet", None)
    if scope in ("zone", "bank") and scope_id:
        return (scope, scope_id)
    return None


@dataclass
class StopRegistry:
    """K8 stop state, tracked PER STOP, not per scope. A scope is stopped while ANY of its ENGAGE
    `stop_id`s is outstanding. A verified RELEASE removes only its own `stop_id`; an unknown or
    already-released id changes nothing. So a replayed old RELEASE can never lift a newer stop, and the
    retained ENGAGE/RELEASE messages a reconnecting hub receives may arrive in any order:

    - RELEASE before its ENGAGE: the id is remembered as released, and the late ENGAGE is ignored;
    - `issued_at` backstop: a RELEASE issued before the newest ENGAGE seen for its scope is ignored.
    """

    #: scope -> {stop_id: ENGAGE issued_at (epoch seconds)}
    outstanding: dict[ScopeKey, dict[str, float]] = field(default_factory=dict)
    #: stop_ids already released: a later (replayed or reordered) ENGAGE with one of them is ignored.
    released: set[str] = field(default_factory=set)
    #: newest ENGAGE issued_at seen per scope (the RELEASE backstop).
    newest_engage_at: dict[ScopeKey, float] = field(default_factory=dict)

    def engage(
        self, scope: str, scope_id: str | None, stop_id: str = _MANUAL_STOP_ID, issued_at: float = 0.0
    ) -> bool:
        key = _scope_key(scope, scope_id)
        if key is None or stop_id in self.released:
            return False
        self.outstanding.setdefault(key, {})[stop_id] = issued_at
        self.newest_engage_at[key] = max(self.newest_engage_at.get(key, issued_at), issued_at)
        return True

    def release_stop(self, scope: str, scope_id: str | None, stop_id: str, issued_at: float) -> bool:
        """Release exactly `stop_id`. Returns True only if it was outstanding and is now removed."""
        key = _scope_key(scope, scope_id)
        if key is None or issued_at < self.newest_engage_at.get(key, float("-inf")):
            return False
        self.released.add(stop_id)
        ids = self.outstanding.get(key, {})
        if stop_id not in ids:
            return False
        del ids[stop_id]
        return True

    def apply_verified_event(self, event: dict[str, Any]) -> bool:
        """Apply a StopEvent that `verify_stop_event` has already accepted. Returns True if state changed."""
        scope, scope_id = str(event.get("scope", "")), event.get("scope_id")
        stop_id = str(event.get("stop_id", ""))
        if not stop_id:
            return False
        try:
            issued_at = parse_rfc3339(str(event["issued_at"])).timestamp()
        except (KeyError, ValueError):
            return False
        if event.get("action") == "ENGAGE":
            return self.engage(scope, scope_id, stop_id, issued_at)
        if event.get("action") == "RELEASE":
            return self.release_stop(scope, scope_id, stop_id, issued_at)
        return False

    def _stopped(self, key: ScopeKey) -> bool:
        return bool(self.outstanding.get(key))

    @property
    def fleet_stopped(self) -> bool:
        return self._stopped(("fleet", None))

    @property
    def zones_stopped(self) -> set[str]:
        return {ref for (scope, ref), ids in self.outstanding.items() if scope == "zone" and ids and ref}

    @property
    def banks_stopped(self) -> set[str]:
        return {ref for (scope, ref), ids in self.outstanding.items() if scope == "bank" and ids and ref}

    def is_stopped(self, zone: str, bank_id: str) -> bool:
        return self.fleet_stopped or self._stopped(("zone", zone)) or self._stopped(("bank", bank_id))


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
