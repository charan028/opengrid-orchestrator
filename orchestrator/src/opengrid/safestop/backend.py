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


@dataclass(frozen=True, slots=True)
class StopEventRow:
    """The `og.stop_event` row a stop decision records (the `insert_stop_event` keyword set)."""

    stop_event_id: UUID
    scope_kind: Literal["BANK", "ZONE", "FLEET"]
    scope_ref: str
    action: Literal["ENGAGE", "RELEASE"]
    initiator_kind: InitiatorKind
    initiator_ref: str
    reason: str
    approver_ref: str | None
    signature: str


@dataclass(frozen=True, slots=True)
class RecordedL2Engage:
    """An UTILITY ENGAGE already recorded for an L2 instruction (for the redelivery repair)."""

    stop_id: UUID
    bank_id: str
    reason: str
    initiator_ref: str
    has_publication: bool  # an og.stop_outbox row exists for (stop_id, ENGAGE)
    released: bool  # a later RELEASE is recorded for the bank: never re-publish the ENGAGE


class StopOutboxBackend(Protocol):
    """K8 durable publish outbox: every accepted ENGAGE/RELEASE is queued and (re)published until the broker
    acknowledges it, in acceptance order within a scope and ENGAGE first across scopes; an entry that fails permanently `max_attempts` times is dead-lettered (skipped, alerted) so it
    never blocks later stops."""

    async def record_and_enqueue(
        self, row: StopEventRow, *, stop_id: UUID, topic_suffix: str, payload: dict[str, Any]
    ) -> None:
        """H5: write the `og.stop_event` row AND its outbox entry in ONE transaction -- a stop can never be
        recorded (which makes a redelivered L2 instruction ALREADY_ACTED) without being queued to publish."""
        ...

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

    async def pending_publications(self, *, limit: int, max_attempts: int) -> list[OutboxEntry]:
        """Unacknowledged, not dead-lettered entries (fewer than `max_attempts` permanent failures), in
        acceptance order (the drain orders them: `SafestopService.drain_order`): the oldest `limit` plus every entry
        of each scope with a queued ENGAGE, wherever it sits in the queue (an ENGAGE never waits behind a backlog)."""
        ...

    async def dead_lettered_publications(self, *, limit: int, max_attempts: int) -> list[OutboxEntry]:
        """Unacknowledged entries at or past the cap (dead-lettered), oldest first -- re-alerted on every drain
        (idempotently), so one dead-lettered by a crash between writes or by an upgrade is never silent."""
        ...

    async def mark_published(self, seq: int) -> None: ...

    async def record_publish_failure(self, seq: int, error: str, *, permanent: bool) -> int:
        """Record a failed publish. Only a `permanent` failure counts toward the dead-letter cap (a broker
        outage never dead-letters a stop). Returns the entry's permanent-failure count."""
        ...

    async def raise_dead_letter_alert(self, entry: OutboxEntry, error: str) -> bool:
        """ALR-STOP-PUBLISH-DEAD-LETTER (critical), once per entry while open. True when newly raised."""
        ...

    async def l2_engage_record(self, instruction_id: UUID, bank_id: str) -> RecordedL2Engage | None: ...


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
