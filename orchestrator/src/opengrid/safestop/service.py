"""`SafestopService` -- the object `opengrid.safestop.engage`/`release` delegate to once `main.py` (or a
test) wires it up via `configure_service()`. Owns the K8 safety property directly: `release()` always
raises, unconditionally, because the stop-only key cannot sign a RELEASE (02a S6.5, interfaces/crypto.md
S2.3). A RELEASE reaches the hubs only as an event the GUARDIAN signed after Tier-2 approval, which this
process verifies and relays (`relay_guardian_release`) but can never create or alter.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import ValidationError

from opengrid.core.crypto import verify_payload
from opengrid.core.models.mqtt import StopEvent
from opengrid.safestop.backend import (
    InitiatorKind,
    OutboxEntry,
    ReleaseHousekeepingBackend,
    StopEventBackend,
    StopEventRow,
    StopOutboxBackend,
    StopPublisher,
)
from opengrid.safestop.events import Scope, build_engage_event, stop_topic_suffix, wire_stop_topic_suffix
from opengrid.safestop.keys import StopSigningKey
from opengrid.safestop.mqtt_publish import StopPublishError

logger = logging.getLogger(__name__)

#: Permanent publish failures before a stop outbox entry is dead-lettered.
DEFAULT_OUTBOX_MAX_ATTEMPTS = 5


def _row_kwargs(row: StopEventRow) -> dict[str, Any]:
    return {
        "stop_event_id": row.stop_event_id,
        "scope_kind": row.scope_kind,
        "scope_ref": row.scope_ref,
        "action": row.action,
        "initiator_kind": row.initiator_kind,
        "initiator_ref": row.initiator_ref,
        "reason": row.reason,
        "approver_ref": row.approver_ref,
        "signature": row.signature,
    }


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


def outbox_scope(entry: OutboxEntry) -> str:
    """The stop scope an outbox entry publishes to: its retained topic without the stop id
    (`stop/<scope>/<id>/<stop_id>` -> `stop/<scope>/<id>`)."""
    return entry.topic_suffix.rsplit("/", 1)[0]


def drain_order(entries: list[OutboxEntry]) -> list[OutboxEntry]:
    """The publish order of queued stop events (given in acceptance order): each scope's entries stay in
    acceptance order; across scopes, the next ENGAGE at the head of any scope goes before any RELEASE at a
    head (oldest first within each kind). So an ENGAGE never waits behind another scope's RELEASE, and never
    overtakes an older RELEASE of its own scope (which the hubs would then drop, leaving the bank stopped)."""
    queues: dict[str, list[OutboxEntry]] = {}
    for entry in sorted(entries, key=lambda e: e.seq):
        queues.setdefault(outbox_scope(entry), []).append(entry)
    ordered: list[OutboxEntry] = []
    while queues:
        heads = [queue[0] for queue in queues.values()]
        engages = [e for e in heads if e.action == "ENGAGE"]
        chosen = min(engages or heads, key=lambda e: e.seq)
        scope = outbox_scope(chosen)
        queues[scope].pop(0)
        if not queues[scope]:
            del queues[scope]
        ordered.append(chosen)
    return ordered


@dataclass
class SafestopService:
    stop_key: StopSigningKey
    backend: StopEventBackend
    publisher: StopPublisher
    trace: TraceAppender | None = None
    #: The guardian's Ed25519 public key (32 raw bytes). None: no RELEASE is ever relayed (fail closed).
    guardian_public_key: bytes | None = None
    #: K8 durable publish outbox (og.stop_outbox, migration 0035). Set in production (main.py): every
    #: accepted ENGAGE/RELEASE is queued and (re)published until the broker acknowledges it, in acceptance
    #: order, including after a broker reconnect. None: publish directly (a failed publish raises).
    outbox: StopOutboxBackend | None = None
    #: Permanent publish failures before an outbox entry is dead-lettered (skipped and alerted) -- a poison
    #: entry must never block later stops. Broker outages never count toward it.
    outbox_max_attempts: int = DEFAULT_OUTBOX_MAX_ATTEMPTS
    _drain_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

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

        row = StopEventRow(
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
        await self._record_and_publish(row, stop_id, topic_suffix, event.model_dump(mode="json"))
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
        topic_suffix = wire_stop_topic_suffix(parsed.scope, parsed.scope_id, parsed.stop_id)
        if await self.backend.has_signature(signature):
            if self.outbox is not None:
                # H5 repair: recorded but (from before the atomic write) never queued -- queue it now. A no-op
                # when it is already queued (once per stop_id and action).
                await self.outbox.enqueue_publication(
                    stop_id=parsed.stop_id, action="RELEASE", topic_suffix=topic_suffix, payload=dict(event)
                )
                await self._drain_after_accept()
            return True
        scope_kind = _WIRE_TO_KIND[parsed.scope]
        scope_ref = parsed.scope_id or "FLEET"
        engage_at = await self.backend.latest_engage_at(scope_kind, scope_ref)
        if engage_at is not None and parsed.issued_at < engage_at:
            await self._refuse_superseded_release(parsed, scope_kind, scope_ref, engage_at)
            return False
        if self.trace is not None:
            await self.trace.append(
                "safestop", "SAFE_STOP", "SAFE_STOP", dict(event), reason_codes=["SAFE_STOP_RELEASE"]
            )
        row = StopEventRow(
            stop_event_id=uuid4(),
            scope_kind=scope_kind,
            scope_ref=scope_ref,
            action="RELEASE",
            initiator_kind="GUARDIAN",
            initiator_ref=parsed.issued_by,
            reason=parsed.reason,
            approver_ref=parsed.approver_ref,
            signature=signature,
        )
        await self._record_and_publish(row, parsed.stop_id, topic_suffix, dict(event))
        logger.info(
            "guardian-signed stop RELEASE published",
            extra={"scope": scope_kind, "scope_ref": parsed.scope_id, "stop_id": str(parsed.stop_id)},
        )
        return True

    async def _refuse_superseded_release(
        self, parsed: StopEvent, scope_kind: str, scope_ref: str, engage_at: datetime
    ) -> None:
        """r3.4.2 review L-1 (lead-approved): a guardian RELEASE signed BEFORE the newest ENGAGE on its scope is
        never relayed. The hubs would drop it as older than that ENGAGE, so relaying it would record the scope as
        released in og.stop_event while the hubs stay stopped. It is traced as superseded (the guardian stops
        re-handing it: `guardian.repo._UNPUBLISHED_RELEASES_SQL`), and ALR-STOP-RELEASE-SUPERSEDED asks the
        operators to re-issue the two-person release, which the guardian then signs after the ENGAGE."""
        logger.warning(
            "refused a stop RELEASE superseded by a newer ENGAGE on its scope",
            extra={"scope": scope_kind, "scope_ref": scope_ref, "stop_id": str(parsed.stop_id)},
        )
        if self.trace is not None:
            await self.trace.append(
                "safestop",
                "SAFE_STOP",
                "SAFE_STOP",
                {
                    "release": "SUPERSEDED",
                    "superseded_signature": parsed.signature,
                    "stop_id": str(parsed.stop_id),
                    "scope_kind": scope_kind,
                    "scope_ref": scope_ref,
                    "release_issued_at": parsed.issued_at.isoformat(),
                    "newer_engage_at": engage_at.isoformat(),
                },
                reason_codes=["SAFE_STOP_RELEASE_SUPERSEDED"],
            )
        await self.backend.raise_superseded_release_alert(
            scope_kind=scope_kind,
            scope_ref=scope_ref,
            stop_id=parsed.stop_id,
            signature=parsed.signature,
            engage_at=engage_at,
        )

    async def _record_and_publish(
        self, row: StopEventRow, stop_id: UUID, topic_suffix: str, payload: dict[str, Any]
    ) -> None:
        """Without an outbox: record, then publish now (a failure raises; the RELEASE keeps its old
        publish-then-record order so the guardian re-hands an unpublished one). With one (production): the
        `og.stop_event` row and its outbox entry in ONE transaction (H5 -- a stop is never recorded without
        being queued), then drain. A publish that cannot happen now (broker down) is NOT an error for the
        caller: the stop is accepted, recorded and published in order as soon as the broker is back."""
        if self.outbox is None:
            if row.action == "RELEASE":
                await self.publisher.publish_retained(topic_suffix, payload)
                await self.backend.insert_stop_event(**_row_kwargs(row))
            else:
                await self.backend.insert_stop_event(**_row_kwargs(row))
                await self.publisher.publish_retained(topic_suffix, payload)
            return
        await self.outbox.record_and_enqueue(row, stop_id=stop_id, topic_suffix=topic_suffix, payload=payload)
        await self._drain_after_accept()

    async def ensure_l2_engage_published(self, instruction_id: UUID, bank_id: str) -> bool:
        """H5 repair on a redelivered utility L2 instruction the intake reports ALREADY_ACTED: if its ENGAGE
        is recorded but was never queued to publish (a failure between the old separate writes), sign it again
        under the SAME stop_id (hubs dedupe by stop_id) and queue it -- unless that bank was released since
        (never re-stop a released bank on an old instruction). Returns True when it queued a publication."""
        if self.outbox is None:
            return False
        record = await self.outbox.l2_engage_record(instruction_id, bank_id)
        if record is None or record.has_publication or record.released:
            return False
        event = build_engage_event(
            scope="BANK",
            scope_ref=bank_id,
            reason=record.reason,
            initiator_ref=record.initiator_ref,
            key_id=self.stop_key.key_id,
            seed=self.stop_key.seed,
            stop_id=record.stop_id,
        )
        if self.trace is not None:
            await self.trace.append(
                "safestop",
                "SAFE_STOP",
                "SAFE_STOP",
                event.model_dump(mode="json"),
                reason_codes=["SAFE_STOP_ENGAGE", "SAFE_STOP_REPUBLISH"],
            )
        await self.outbox.enqueue_publication(
            stop_id=record.stop_id,
            action="ENGAGE",
            topic_suffix=stop_topic_suffix("BANK", bank_id, record.stop_id),
            payload=event.model_dump(mode="json"),
        )
        logger.warning(
            "recorded utility ENGAGE had no publication: re-signed and queued",
            extra={"stop_id": str(record.stop_id), "bank_id": bank_id},
        )
        await self._drain_after_accept()
        return True

    async def drain_outbox(self, *, batch: int = 100) -> int:
        """K8: publish every queued stop event the broker has not acknowledged, in `drain_order` -- in
        acceptance order WITHIN a scope (a hub drops a RELEASE older than the newest ENGAGE on its scope, so a
        newer ENGAGE must never overtake an older RELEASE of the same scope), with ENGAGE priority only ACROSS
        scopes (a stop is never delayed behind another scope's release). Each is marked once its QoS 1 publish
        returned (the broker's PUBACK). A broker/connection failure stops the drain (the rest go on the next
        drain: after a reconnect, or the next tick) and never counts against an entry. A PERMANENT failure (a
        payload that can never be published) counts, and holds the rest of ITS scope back until the entry is
        published or dead-lettered; at `outbox_max_attempts` it is dead-lettered (skipped from then on) and
        ALR-STOP-PUBLISH-DEAD-LETTER is raised -- re-raised idempotently on every drain for every dead-lettered
        entry, so one dead-lettered by a crash between writes, or found at the cap after an upgrade, is never
        silent. Other scopes carry on, so a poison entry never blocks later stops. Serialised. Returns how
        many were published."""
        if self.outbox is None:
            return 0
        published = 0
        async with self._drain_lock:
            pending = await self.outbox.pending_publications(
                limit=batch, max_attempts=self.outbox_max_attempts
            )
            held: set[str] = set()  # scopes whose earlier entry failed permanently this drain
            dead_now: set[int] = set()
            for entry in drain_order(pending):
                scope = outbox_scope(entry)
                if scope in held and entry.action != "ENGAGE":
                    # A failing entry holds back its scope's later RELEASEs, never an ENGAGE (r3.4.2 review L-3):
                    # published ahead of an older RELEASE, the ENGAGE wins at the hubs (they drop a RELEASE older
                    # than the newest ENGAGE), which is the safe side.
                    continue
                try:
                    await self.publisher.publish_retained(entry.topic_suffix, entry.payload)
                except Exception as exc:
                    permanent = isinstance(exc, StopPublishError) and not exc.transient
                    attempts = await self.outbox.record_publish_failure(
                        entry.seq, str(exc), permanent=permanent
                    )
                    if not permanent:
                        logger.warning(
                            "stop publication queued until the broker is back",
                            extra={"stop_id": str(entry.stop_id), "action": entry.action, "error": str(exc)},
                        )
                        break
                    if attempts < self.outbox_max_attempts:
                        held.add(scope)
                    else:
                        await self._dead_letter(entry, str(exc))  # logged and traced once, at the cap
                        dead_now.add(entry.seq)
                    continue
                await self.outbox.mark_published(entry.seq)
                published += 1
            published += await self._sweep_dead_letters(batch, skip=dead_now)
        return published

    async def _sweep_dead_letters(self, batch: int, *, skip: set[int]) -> int:
        """Every drain (r3.4.2 review L-4): each dead-lettered entry -- including one dead-lettered by a crash
        between writes, or found at the cap after an upgrade (pre-upgrade attempts also counted broker outages)
        -- is alerted, idempotently, and TRACED when its alert is newly raised. A dead-lettered ENGAGE is a stop
        that never reached the hubs, so it is re-tried here on every drain (after the live queue, never ahead of
        it) and published as soon as it can be; a RELEASE is only alerted. Returns how many it published."""
        if self.outbox is None:
            return 0
        published = 0
        for dead in await self.outbox.dead_lettered_publications(
            limit=batch, max_attempts=self.outbox_max_attempts
        ):
            if dead.action == "ENGAGE" and dead.seq not in skip:
                try:
                    await self.publisher.publish_retained(dead.topic_suffix, dead.payload)
                except Exception as exc:
                    logger.warning(
                        "dead-lettered stop ENGAGE still cannot be published",
                        extra={"stop_id": str(dead.stop_id), "error": str(exc)},
                    )
                else:
                    await self.outbox.mark_published(dead.seq)
                    await self._trace_outbox("DEAD_LETTER_PUBLISHED", dead, "published on a later drain")
                    published += 1
                    continue
            error = "publication failed permanently at the attempt cap"
            if await self.outbox.raise_dead_letter_alert(dead, error) and dead.seq not in skip:
                await self._trace_outbox("DEAD_LETTER", dead, error)
        return published

    async def _trace_outbox(self, outcome: str, entry: OutboxEntry, error: str) -> None:
        """Best-effort audit trace of an outbox outcome (never raises: the outbox row is the durable record)."""
        if self.trace is None:
            return
        try:
            await self.trace.append(
                "safestop",
                "SAFE_STOP",
                "SAFE_STOP",
                {
                    "outbox": outcome,
                    "stop_id": str(entry.stop_id),
                    "action": entry.action,
                    "error": error[:500],
                },
                reason_codes=[f"SAFE_STOP_{outcome}"],
            )
        except Exception:
            logger.exception("stop outbox trace failed", extra={"stop_id": str(entry.stop_id)})

    async def _drain_after_accept(self) -> None:
        """Drain right after a stop was accepted and durably queued. Never raises: the stop is recorded and
        queued, so a failure here (another entry's error, a database hiccup) must neither fail the caller --
        which would make the L2 intake or the API intake retry or die -- nor lose anything; the next drain
        (tick or reconnect) publishes it."""
        try:
            await self.drain_outbox()
        except Exception:
            logger.exception("stop outbox drain after accept failed; the next drain retries it")

    async def _dead_letter(self, entry: OutboxEntry, error: str) -> None:
        logger.error(
            "stop publication dead-lettered: it can never be published",
            extra={"stop_id": str(entry.stop_id), "action": entry.action, "error": error},
        )
        if self.outbox is not None:
            await self.outbox.raise_dead_letter_alert(entry, error)
        await self._trace_outbox("DEAD_LETTER", entry, error)

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
