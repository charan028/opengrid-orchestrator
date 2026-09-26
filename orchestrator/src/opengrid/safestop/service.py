"""`SafestopService` -- the object `opengrid.safestop.engage`/`release` delegate to once `main.py` (or a
test) wires it up via `configure_service()`. Owns the K8 safety property directly: `release()` always
raises, unconditionally, because the stop-only key cannot sign a RELEASE (02a S6.5, interfaces/crypto.md
S2.3). A RELEASE reaches the hubs only as an event the GUARDIAN signed after Tier-2 approval, which this
process verifies and relays (`relay_guardian_release`) but can never create or alter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError

from opengrid.core.crypto import verify_payload
from opengrid.core.models.mqtt import StopEvent
from opengrid.safestop.backend import (
    InitiatorKind,
    ReleaseHousekeepingBackend,
    StopEventBackend,
    StopPublisher,
)
from opengrid.safestop.events import Scope, build_engage_event, stop_topic_suffix, wire_stop_topic_suffix
from opengrid.safestop.keys import StopSigningKey

logger = logging.getLogger(__name__)

_SCOPE_TO_KIND: dict[Scope, str] = {"FLEET": "FLEET", "ZONE": "ZONE", "BANK": "BANK"}
_WIRE_TO_KIND: dict[str, Literal["FLEET", "ZONE", "BANK"]] = {
    "fleet": "FLEET",
    "zone": "ZONE",
    "bank": "BANK",
}


class ReleaseNotPermittedError(Exception):
    """K8: the stop-only key can never sign a RELEASE. Always raised by `SafestopService.release()`
    (and therefore by `opengrid.safestop.release()`) -- release is the guardian's job, after Tier-2
    (two-person) approval, plus a fresh operator action; it never happens through this process."""


class TraceAppender(Protocol):
    """Structural type for the one `TraceStore` method `SafestopService` calls -- lets tests pass a
    lightweight fake without constructing a full `TraceStore`."""

    async def append(
        self,
        stream_id: str,
        decision_type: str,
        event_class: str,
        payload: dict[str, object],
        reason_codes: list[str] | None = None,
    ) -> object: ...


@dataclass
class SafestopService:
    stop_key: StopSigningKey
    backend: StopEventBackend
    publisher: StopPublisher
    trace: TraceAppender | None = None
    #: The guardian's Ed25519 public key (32 raw bytes). None: no RELEASE is ever relayed (fail closed).
    guardian_public_key: bytes | None = None

    async def engage(
        self,
        scope: Scope,
        scope_ref: str,
        reason: str,
        initiator_ref: str,
        *,
        initiator_kind: InitiatorKind = "SAFESTOP_AUTHORITY",
    ) -> UUID:
        """Sign (stop-only key) and broadcast a retained ENGAGE stop for `scope`/`scope_ref` (02a S6.5).

        Order matters for K10 (no signed/broadcast action without a durable pre-image): trace first,
        then persist the `stop_event` row, then publish MQTT last -- a crash before the MQTT publish
        still leaves an auditable, unambiguous record that the ENGAGE was decided, and the retained
        publish is safely retried (StopEvent is idempotent by `stop_id`).

        `initiator_kind` is the `og.stop_event.initiator_kind` column: the API path keeps the default
        `SAFESTOP_AUTHORITY`; the host CLI records `OPERATOR` and the L2 intake `UTILITY`. Returns the
        new `stop_id` (the last segment of the retained topic).
        """
        stop_id = uuid4()
        event = build_engage_event(
            scope=scope,
            scope_ref=scope_ref,
            reason=reason,
            initiator_ref=initiator_ref,
            key_id=self.stop_key.key_id,
            seed=self.stop_key.seed,
            stop_id=stop_id,
        )

        if self.trace is not None:
            await self.trace.append(
                "safestop",
                "SAFE_STOP",
                "SAFE_STOP",
                event.model_dump(mode="json"),
                reason_codes=["SAFE_STOP_ENGAGE"],
            )

        await self.backend.insert_stop_event(
            stop_event_id=stop_id,
            scope_kind=_SCOPE_TO_KIND[scope],  # type: ignore[arg-type]
            scope_ref=scope_ref or "FLEET",
            action="ENGAGE",
            initiator_kind=initiator_kind,
            initiator_ref=initiator_ref,
            reason=reason,
            approver_ref=None,
            signature=event.signature,
        )

        topic_suffix = stop_topic_suffix(scope, scope_ref, stop_id)
        await self.publisher.publish_retained(topic_suffix, event.model_dump(mode="json"))
        logger.info(
            "safe stop engaged",
            extra={"scope": scope, "scope_ref": scope_ref, "stop_id": str(stop_id)},
        )
        return stop_id

    async def relay_guardian_release(self, event: dict[str, Any]) -> bool:
        """K8 / crypto.md S2.3: publish a RELEASE the GUARDIAN signed after Tier-2 approval. This process
        still never signs one; it only relays, and only what it verifies itself first:

        - it has the guardian public key configured (else nothing is ever relayed);
        - the event is a well-formed `StopEvent` with `action="RELEASE"` and a `guardian-*` key id;
        - it names two different people (`issued_by` requester, `approver_ref` approver);
        - the Ed25519 signature verifies against the guardian key over every field but key_id/signature.

        Then, in K10 order: trace, publish retained on the event's own topic (the ENGAGE's topic, which
        it replaces), and record the `og.stop_event` RELEASE row. Idempotent by signature: an event already
        recorded is not republished. Returns True when the event is (or already was) published."""
        refusal = self._release_refusal(event)
        if refusal is not None:
            logger.warning("refused to relay stop RELEASE", extra={"reason": refusal})
            return False
        parsed = StopEvent.model_validate(event)
        signature = parsed.signature
        if await self.backend.has_signature(signature):
            return True
        if self.trace is not None:
            await self.trace.append(
                "safestop", "SAFE_STOP", "SAFE_STOP", dict(event), reason_codes=["SAFE_STOP_RELEASE"]
            )
        await self.publisher.publish_retained(
            wire_stop_topic_suffix(parsed.scope, parsed.scope_id, parsed.stop_id), dict(event)
        )
        scope_kind = _WIRE_TO_KIND[parsed.scope]
        await self.backend.insert_stop_event(
            stop_event_id=uuid4(),
            scope_kind=scope_kind,
            scope_ref=parsed.scope_id or "FLEET",
            action="RELEASE",
            initiator_kind="GUARDIAN",
            initiator_ref=parsed.issued_by,
            reason=parsed.reason,
            approver_ref=parsed.approver_ref,
            signature=signature,
        )
        logger.info(
            "guardian-signed stop RELEASE published",
            extra={"scope": scope_kind, "scope_ref": parsed.scope_id, "stop_id": str(parsed.stop_id)},
        )
        return True

    async def clear_released_retained(
        self, housekeeping: ReleaseHousekeepingBackend, *, retain_s: float
    ) -> int:
        """K8 housekeeping: once a relayed RELEASE has stayed retained for `retain_s` (long enough for any
        hub that was offline to reconnect and receive it), delete the retained message on that stop's
        topic with an empty retained publish, and trace it. The topic then holds nothing -- neither the
        superseded ENGAGE (already replaced by the RELEASE) nor a RELEASE an attacker could replay from
        the broker. Hubs ignore the empty payload. Returns the number of topics cleared."""
        cleared = 0
        for event in await housekeeping.releases_due_for_clearing(retain_s=retain_s, limit=50):
            try:
                parsed = StopEvent.model_validate(event)
            except ValidationError:
                continue
            await self.publisher.clear_retained(
                wire_stop_topic_suffix(parsed.scope, parsed.scope_id, parsed.stop_id)
            )
            if self.trace is not None:
                await self.trace.append(
                    "safestop",
                    "SAFE_STOP",
                    "SAFE_STOP",
                    {
                        "housekeeping": "RETAINED_CLEARED",
                        "stop_id": str(parsed.stop_id),
                        "scope": parsed.scope,
                        "scope_id": parsed.scope_id,
                    },
                    reason_codes=["SAFE_STOP_HOUSEKEEPING"],
                )
            cleared += 1
        return cleared

    def _release_refusal(self, event: Any) -> str | None:
        if self.guardian_public_key is None:
            return "GUARDIAN_PUBLIC_KEY_NOT_CONFIGURED"
        if not isinstance(event, dict):
            return "NOT_AN_EVENT"
        try:
            parsed = StopEvent.model_validate(event)
        except ValidationError:
            return "MALFORMED_EVENT"
        if parsed.action != "RELEASE":
            return "NOT_A_RELEASE"
        if not parsed.key_id.startswith("guardian-"):
            return "NOT_A_GUARDIAN_KEY_ID"
        approver = (parsed.approver_ref or "").strip()
        if not approver or approver.casefold() == parsed.issued_by.strip().casefold():
            return "NOT_TIER2_APPROVED"
        signed = {k: v for k, v in event.items() if k not in ("key_id", "signature")}
        if not verify_payload(self.guardian_public_key, signed, parsed.signature):
            return "BAD_SIGNATURE"
        return None

    async def release(self, scope: Scope, scope_ref: str, approver_ref: str) -> None:
        """K8: always fails closed. See `ReleaseNotPermittedError`."""
        logger.warning(
            "release attempted through og-safestop -- refused (guardian Tier-2 path required)",
            extra={"scope": scope, "scope_ref": scope_ref},
        )
        raise ReleaseNotPermittedError(
            "safestop's stop-only key cannot sign RELEASE; release requires guardian Tier-2 "
            "(two-person) approval through the guardian's own signing path (02a S6.5)"
        )
