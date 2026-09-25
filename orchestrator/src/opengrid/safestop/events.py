"""Pure logic for building a signed `StopEvent` (02a S6.5). No I/O -- `service.py` calls this and then
hands the result to the Postgres backend and the MQTT publisher.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from opengrid.core.crypto import sign_payload
from opengrid.core.models.mqtt import StopEvent

Scope = Literal["FLEET", "ZONE", "BANK"]

# 02a S6.5: linear ramp-to-zero window per scope. Not carried on the wire message (the hub/sim applies
# its own scope's window on receipt); kept here as the single source of truth for tests and docs.
RAMP_WINDOW_S: dict[Scope, int] = {"BANK": 30, "ZONE": 60, "FLEET": 120}

_WIRE_SCOPE: dict[Scope, Literal["fleet", "zone", "bank"]] = {
    "FLEET": "fleet",
    "ZONE": "zone",
    "BANK": "bank",
}


class InvalidScopeReferenceError(ValueError):
    """Raised when `scope_ref` doesn't match what `scope` requires (e.g. a non-empty ref for FLEET)."""


def ramp_window_s(scope: Scope) -> int:
    """Ramp-to-zero window in seconds for `scope` (02a S6.5)."""
    return RAMP_WINDOW_S[scope]


def to_wire_scope(scope: Scope, scope_ref: str) -> tuple[Literal["fleet", "zone", "bank"], str | None]:
    """Map the internal `Scope`/`scope_ref` pair to the wire `(scope, scope_id)` pair
    (`interfaces/mqtt/stop.schema.json`: `scope_id` is null for fleet scope, required otherwise)."""
    wire_scope = _WIRE_SCOPE[scope]
    if scope == "FLEET":
        return wire_scope, None
    if not scope_ref:
        raise InvalidScopeReferenceError(f"scope_ref is required for scope={scope}")
    return wire_scope, scope_ref


def stop_topic_suffix(scope: Scope, scope_ref: str, stop_id: UUID) -> str:
    """`<root>/stop/<scope>/<id>` suffix (topics.md); `<scope>` includes the zone/bank ref inline."""
    wire_scope, scope_id = to_wire_scope(scope, scope_ref)
    scope_part = wire_scope if scope_id is None else f"{wire_scope}/{scope_id}"
    return f"stop/{scope_part}/{stop_id}"


def build_engage_event(
    *,
    scope: Scope,
    scope_ref: str,
    reason: str,
    initiator_ref: str,
    key_id: str,
    seed: bytes,
    stop_id: UUID | None = None,
    issued_at: datetime | None = None,
) -> StopEvent:
    """Build and sign an `action="ENGAGE"` StopEvent with the stop-only key. Pure aside from the
    Ed25519 sign call, which takes no clock/network/DB dependency."""
    wire_scope, scope_id = to_wire_scope(scope, scope_ref)
    # interfaces/crypto.md S2.3: signed over the StopEvent fields excluding signature/key_id.
    signing_fields: dict[str, Any] = {
        "stop_id": str(stop_id or uuid4()),
        "scope": wire_scope,
        "scope_id": scope_id,
        "action": "ENGAGE",
        "reason": reason,
        "issued_by": initiator_ref,
        "issued_at": (issued_at or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "approver_ref": None,
    }
    signature = sign_payload(seed, signing_fields)
    return StopEvent(**signing_fields, key_id=key_id, signature=signature)
