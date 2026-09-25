"""`SafestopService` -- the object `opengrid.safestop.engage`/`release` delegate to once `main.py` (or a
test) wires it up via `configure_service()`. Owns the K8 safety property directly: `release()` always
raises, unconditionally, because the stop-only key cannot sign a RELEASE and there is no guardian
co-signature available to this process (02a S6.5, interfaces/crypto.md S2.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4

from opengrid.safestop.backend import StopEventBackend, StopPublisher
from opengrid.safestop.events import Scope, build_engage_event, stop_topic_suffix
from opengrid.safestop.keys import StopSigningKey

logger = logging.getLogger(__name__)

_SCOPE_TO_KIND: dict[Scope, str] = {"FLEET": "FLEET", "ZONE": "ZONE", "BANK": "BANK"}


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

    async def engage(self, scope: Scope, scope_ref: str, reason: str, initiator_ref: str) -> None:
        """Sign (stop-only key) and broadcast a retained ENGAGE stop for `scope`/`scope_ref` (02a S6.5).

        Order matters for K10 (no signed/broadcast action without a durable pre-image): trace first,
        then persist the `stop_event` row, then publish MQTT last -- a crash before the MQTT publish
        still leaves an auditable, unambiguous record that the ENGAGE was decided, and the retained
        publish is safely retried (StopEvent is idempotent by `stop_id`).
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
            initiator_kind="SAFESTOP_AUTHORITY",
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
