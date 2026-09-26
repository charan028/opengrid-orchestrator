"""I/O contracts `SafestopService` needs, kept as `Protocol`s so unit tests can supply in-memory fakes
and the real Postgres/MQTT implementations (`pg_backend.py`, `mqtt_publish.py`) stay swappable.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol
from uuid import UUID

InitiatorKind = Literal["OPERATOR", "GUARDIAN", "SAFESTOP_AUTHORITY", "UTILITY"]


class StopEventBackend(Protocol):
    """Persistence for `og.stop_event` (02a S1.12). `safestop` is the only writer of stop rows: ENGAGE
    rows it signed itself, and RELEASE rows only for guardian-signed events it has verified and
    published (`SafestopService.relay_guardian_release`)."""

    async def insert_stop_event(
        self,
        *,
        stop_event_id: UUID,
        scope_kind: Literal["BANK", "ZONE", "FLEET"],
        scope_ref: str,
        action: Literal["ENGAGE", "RELEASE"],
        initiator_kind: InitiatorKind,
        initiator_ref: str,
        reason: str,
        approver_ref: str | None,
        signature: str,
    ) -> None: ...

    async def has_signature(self, signature: str) -> bool:
        """Whether a `stop_event` row with exactly this signature exists -- i.e. this signed event has
        already been published and recorded (relay idempotency)."""
        ...

    async def latest_action(self, scope_kind: str, scope_ref: str) -> str | None:
        """Most recent `action` for this scope, or None if never stopped. Used only for observability
        (e.g. `main.py` refuses a redundant ENGAGE) -- never for release, which `safestop` can't do."""
        ...


class StopPublisher(Protocol):
    """Publishes the retained `<root>/stop/<scope>/<id>` MQTT message (topics.md)."""

    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None: ...
