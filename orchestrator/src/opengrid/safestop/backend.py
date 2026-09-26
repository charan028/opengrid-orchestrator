"""I/O contracts `SafestopService` needs, kept as `Protocol`s so unit tests can supply in-memory fakes
and the real Postgres/MQTT implementations (`pg_backend.py`, `mqtt_publish.py`) stay swappable.
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True, slots=True)
class OutboxEntry:
    """One queued stop publication (`og.stop_outbox`, migration 0035)."""

    seq: int
    stop_id: UUID
    action: Literal["ENGAGE", "RELEASE"]
    topic_suffix: str
    payload: dict[str, Any]


class StopOutboxBackend(Protocol):
    """K8 durable publish outbox: every accepted ENGAGE/RELEASE is queued in acceptance order and
    (re)published until the broker acknowledges it."""

    async def enqueue_publication(
        self,
        *,
        stop_id: UUID,
        action: Literal["ENGAGE", "RELEASE"],
        topic_suffix: str,
        payload: dict[str, Any],
    ) -> None:
        """Queue once per (stop_id, action); a second enqueue of the same event is a no-op."""
        ...

    async def pending_publications(self, *, limit: int) -> list[OutboxEntry]:
        """Unacknowledged entries, oldest first (acceptance order)."""
        ...

    async def mark_published(self, seq: int) -> None: ...

    async def record_publish_failure(self, seq: int, error: str) -> None: ...


class StopPublisher(Protocol):
    """Publishes the retained `<root>/stop/<scope>/<id>` MQTT message (topics.md)."""

    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None: ...

    async def clear_retained(self, topic_suffix: str) -> None:
        """Publish an empty retained payload: broker housekeeping that deletes the retained message on
        the topic. Hubs ignore it (it never changes stop state, K8)."""
        ...


class ReleaseHousekeepingBackend(Protocol):
    async def releases_due_for_clearing(self, *, retain_s: float, limit: int) -> list[dict[str, Any]]:
        """Relayed guardian RELEASE events older than `retain_s` whose retained topic has not yet been
        cleared (from og-safestop's own trace stream)."""
        ...
