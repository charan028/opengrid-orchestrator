"""K8 durable publish outbox: a stop ENGAGED while the broker is down is recorded, queued and published on
reconnect, in acceptance order, exactly once per (stop_id, action)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import aiomqtt
import pytest

from opengrid.core.crypto import generate_keypair
from opengrid.platform.mqtt_session import MqttSession
from opengrid.safestop.backend import OutboxEntry
from opengrid.safestop.keys import StopSigningKey
from opengrid.safestop.mqtt_publish import StopPublishError
from opengrid.safestop.service import SafestopService


@dataclass
class _Backend:
    """In-memory `StopEventBackend` + `StopOutboxBackend` with the same once-per-(stop_id, action) rule."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    outbox: list[dict[str, Any]] = field(default_factory=list)

    async def insert_stop_event(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)

    async def enqueue_publication(
        self, *, stop_id: UUID, action: str, topic_suffix: str, payload: dict
    ) -> None:
        if any(e["stop_id"] == stop_id and e["action"] == action for e in self.outbox):
            return
        self.outbox.append(
            {
                "seq": len(self.outbox) + 1,
                "stop_id": stop_id,
                "action": action,
                "topic_suffix": topic_suffix,
                "payload": payload,
                "published": False,
                "attempts": 0,
            }
        )

    #: set to make record_and_enqueue fail mid-transaction (the whole write must then be absent)
    fail_enqueue: bool = False
    alerts: list[tuple[UUID, str]] = field(default_factory=list)

    async def record_and_enqueue(self, row, *, stop_id: UUID, topic_suffix: str, payload: dict) -> None:
        """Both writes or neither, like the one Postgres transaction."""
        if self.fail_enqueue:
            raise RuntimeError("connection lost mid-transaction")
        self.rows.append(
            {"stop_event_id": row.stop_event_id, "action": row.action, "scope_ref": row.scope_ref,
             "reason": row.reason, "initiator_ref": row.initiator_ref, "initiator_kind": row.initiator_kind,
             "signature": row.signature}
        )  # fmt: skip
        await self.enqueue_publication(
            stop_id=stop_id, action=row.action, topic_suffix=topic_suffix, payload=payload
        )

    async def pending_publications(self, *, limit: int, max_attempts: int) -> list[OutboxEntry]:
        """Like `_PENDING_SQL`: the oldest `limit` live entries plus every entry of a scope with a queued ENGAGE."""
        live = [e for e in self.outbox if not e["published"] and e["attempts"] < max_attempts]
        oldest = {e["seq"] for e in live[:limit]}
        engage_scopes = {e["topic_suffix"].rsplit("/", 1)[0] for e in live if e["action"] == "ENGAGE"}
        chosen = [
            e for e in live if e["seq"] in oldest or e["topic_suffix"].rsplit("/", 1)[0] in engage_scopes
        ]
        return [
            OutboxEntry(e["seq"], e["stop_id"], e["action"], e["topic_suffix"], e["payload"]) for e in chosen
        ]

    async def mark_published(self, seq: int) -> None:
        self.outbox[seq - 1]["published"] = True

    async def record_publish_failure(self, seq: int, error: str, *, permanent: bool) -> int:
        if permanent:
            self.outbox[seq - 1]["attempts"] += 1
        return int(self.outbox[seq - 1]["attempts"])

    async def dead_lettered_publications(self, *, limit: int, max_attempts: int) -> list[OutboxEntry]:
        dead = [e for e in self.outbox if not e["published"] and e["attempts"] >= max_attempts]
        return [
            OutboxEntry(e["seq"], e["stop_id"], e["action"], e["topic_suffix"], e["payload"]) for e in dead
        ]

    async def raise_dead_letter_alert(self, entry: OutboxEntry, error: str) -> bool:
        if (entry.stop_id, entry.action) not in self.alerts:  # one open alert per entry, like og.alert
            self.alerts.append((entry.stop_id, entry.action))
            return True
        return False

    async def l2_engage_record(self, instruction_id: UUID, bank_id: str):
        from opengrid.safestop.backend import RecordedL2Engage

        for row in reversed(self.rows):
            if (
                row["action"] == "ENGAGE"
                and row["scope_ref"] == bank_id
                and str(instruction_id) in row["reason"]
            ):
                has = any(
                    e["stop_id"] == row["stop_event_id"] and e["action"] == "ENGAGE" for e in self.outbox
                )
                return RecordedL2Engage(
                    row["stop_event_id"], bank_id, row["reason"], row["initiator_ref"], has, self.released
                )
        return None

    released: bool = False

    async def latest_engage_at(self, scope_kind: str, scope_ref: str) -> None:
        return None  # L-1 is covered in test_release_relay

    async def has_signature(self, signature: str) -> bool:
        return any(row.get("signature") == signature for row in self.rows)


@dataclass
class _Publisher:
    up: bool = False
    published: list[tuple[str, str]] = field(default_factory=list)  # (topic_suffix, action)

    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
        if not self.up:
            raise StopPublishError("MQTT connection 'safestop' is down")
        self.published.append((topic_suffix, payload["action"]))

    async def clear_retained(self, topic_suffix: str) -> None:
        return None


@pytest.fixture
def world() -> tuple[SafestopService, _Backend, _Publisher]:
    seed, _pub = generate_keypair()
    backend, publisher = _Backend(), _Publisher()
    svc = SafestopService(StopSigningKey("safestop-test", seed), backend, publisher, None, outbox=backend)  # type: ignore[arg-type]
    return svc, backend, publisher


async def test_a_stop_engaged_while_the_broker_is_down_is_recorded_and_published_on_reconnect(world):
    svc, backend, publisher = world

    stop_id = await svc.engage("BANK", "bank-07", "overload", "operator:alice")  # does not raise

    assert backend.rows[0]["stop_event_id"] == stop_id  # accepted and recorded
    assert publisher.published == []
    assert [e["published"] for e in backend.outbox] == [False]

    publisher.up = True  # the broker is back: the session's on-connect hook drains
    assert await svc.drain_outbox() == 1
    assert publisher.published == [(backend.outbox[0]["topic_suffix"], "ENGAGE")]
    assert await svc.drain_outbox() == 0  # acknowledged: never re-published


async def test_order_is_preserved_across_an_outage(world):
    svc, _backend, publisher = world
    first = await svc.engage("BANK", "bank-01", "a", "op:a")
    second = await svc.engage("ZONE", "LZ_NORTH", "b", "op:b")
    third = await svc.engage("BANK", "bank-02", "c", "op:c")

    publisher.up = True
    await svc.drain_outbox()

    order = [topic.rsplit("/", 1)[-1] for topic, _action in publisher.published]
    assert order == [str(first), str(second), str(third)]


async def test_a_failure_mid_drain_stops_there_so_order_is_never_broken(world):
    svc, backend, publisher = world
    for ref in ("bank-01", "bank-02", "bank-03"):
        await svc.engage("BANK", ref, "x", "op")
    publisher.up = True
    calls = {"n": 0}
    real = publisher.publish_retained

    async def flaky(topic_suffix: str, payload: dict[str, Any]) -> None:
        calls["n"] += 1
        if calls["n"] == 2:
            raise StopPublishError("dropped mid-drain")
        await real(topic_suffix, payload)

    publisher.publish_retained = flaky  # type: ignore[method-assign]
    assert await svc.drain_outbox() == 1
    assert [e["published"] for e in backend.outbox] == [True, False, False]
    assert await svc.drain_outbox() == 2
    assert [topic.split("/")[2] for topic, _ in publisher.published] == ["bank-01", "bank-02", "bank-03"]


async def test_the_same_event_is_queued_once(world):
    svc, backend, _publisher = world
    stop_id = await svc.engage("BANK", "bank-07", "x", "op")
    entry = backend.outbox[0]
    await backend.enqueue_publication(
        stop_id=stop_id, action="ENGAGE", topic_suffix=entry["topic_suffix"], payload=entry["payload"]
    )
    assert len(backend.outbox) == 1


async def test_the_session_drains_the_outbox_on_every_reconnect(world):
    """End to end over `MqttSession`: the stop is engaged while the connection is down; the next connect's
    on-connect hook publishes it through that connection."""
    svc, backend, _publisher = world
    await svc.engage("BANK", "bank-07", "x", "op")

    sent: list[str] = []
    connected = asyncio.Event()

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
            sent.append(topic)

        @property
        def messages(self):
            return self._stream()

        async def _stream(self):
            await connected.wait()
            raise aiomqtt.MqttError("Disconnected")
            yield  # pragma: no cover

    session = MqttSession(_Client, name="t-outbox")

    class _SessionPublisher:
        async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
            await session.publish(topic_suffix, payload=b"{}", qos=1, retain=True, wait_s=0.1)

    svc.publisher = _SessionPublisher()  # type: ignore[assignment]

    async def drain_then_drop() -> None:
        await svc.drain_outbox()
        connected.set()

    session.on_connect = drain_then_drop
    await session.run(max_connections=1)

    assert sent == [backend.outbox[0]["topic_suffix"]]
    assert backend.outbox[0]["published"] is True


# --- H5: atomic record + enqueue, redelivery repair, dead-letter, ENGAGE priority ---------------------------------


async def test_a_stop_is_never_recorded_without_its_publication(world):
    """H5: the og.stop_event row and its outbox entry are ONE write. If it fails, neither exists -- so a
    redelivered L2 instruction is not ALREADY_ACTED and engages again, instead of a recorded stop that is never
    published."""
    svc, backend, _publisher = world
    backend.fail_enqueue = True
    with pytest.raises(RuntimeError):
        await svc.engage("BANK", "bank-07", "L2 instruction x", "utility:x")
    assert backend.rows == [] and backend.outbox == []


async def test_a_redelivered_instruction_repairs_a_recorded_engage_that_was_never_queued(world):
    """A stop recorded before the fix (or by any path that left no outbox row): the redelivery re-signs it under
    the SAME stop_id and queues it; hubs dedupe by stop_id."""
    svc, backend, publisher = world
    instruction, stop_id = uuid4(), uuid4()
    backend.rows.append(
        {"stop_event_id": stop_id, "action": "ENGAGE", "scope_ref": "bank-07",
         "reason": f"utility L2 BLOCK {instruction}", "initiator_ref": "utility:aen", "initiator_kind": "UTILITY"}
    )  # fmt: skip
    publisher.up = True

    assert await svc.ensure_l2_engage_published(instruction, "bank-07") is True
    ((topic, action),) = publisher.published
    assert action == "ENGAGE" and topic.endswith(str(stop_id))
    assert await svc.ensure_l2_engage_published(instruction, "bank-07") is False  # now queued: a no-op


async def test_the_repair_never_re_stops_a_released_bank(world):
    svc, backend, publisher = world
    instruction = uuid4()
    backend.rows.append(
        {"stop_event_id": uuid4(), "action": "ENGAGE", "scope_ref": "bank-07",
         "reason": f"utility L2 BLOCK {instruction}", "initiator_ref": "utility:aen", "initiator_kind": "UTILITY"}
    )  # fmt: skip
    backend.released = True
    publisher.up = True
    assert await svc.ensure_l2_engage_published(instruction, "bank-07") is False
    assert publisher.published == [] and backend.outbox == []


async def test_the_l2_intake_calls_the_repair_on_an_already_acted_instruction():
    from datetime import UTC, datetime, timedelta

    from opengrid.core.models.mqtt import ScadaUtilityInstruction
    from opengrid.safestop.l2_intake import handle_instruction

    now = datetime.now(UTC)
    instruction = ScadaUtilityInstruction.model_validate(
        {"instruction_id": str(uuid4()), "bank_id": "bank-07", "kind": "BLOCK", "limit_kw": None,
         "issued_at": now, "expires_at": now + timedelta(minutes=5), "issued_by": "utility"}
    )  # fmt: skip
    repaired: list[tuple[UUID, str]] = []

    async def acted(_i: UUID, _b: str) -> bool:
        return True

    async def repair(i: UUID, b: str) -> None:
        repaired.append((i, b))

    async def engage(*_a: object) -> None:
        raise AssertionError("must not engage twice")

    outcome = await handle_instruction(
        instruction,
        now=now,
        handled=set(),
        engage_fn=engage,
        already_acted_fn=acted,
        ensure_published_fn=repair,
    )
    assert outcome == "ALREADY_ACTED" and repaired == [(instruction.instruction_id, "bank-07")]


async def test_a_poison_entry_is_dead_lettered_and_never_blocks_later_stops(world):
    svc, backend, publisher = world
    svc.outbox_max_attempts = 2
    poison = await svc.engage("BANK", "bank-01", "x", "op")
    good = await svc.engage("BANK", "bank-02", "y", "op")
    publisher.up = True
    real = publisher.publish_retained

    async def reject_poison(topic_suffix: str, payload: dict[str, Any]) -> None:
        if topic_suffix.endswith(str(poison)):
            raise StopPublishError("refusing to publish invalid stop payload", transient=False)
        await real(topic_suffix, payload)

    publisher.publish_retained = reject_poison  # type: ignore[method-assign]
    assert await svc.drain_outbox() == 1  # the good stop goes out behind the poison one
    assert [t.rsplit("/", 1)[-1] for t, _ in publisher.published] == [str(good)]
    assert backend.alerts == []  # one permanent failure: not yet dead
    await svc.drain_outbox()
    assert backend.alerts == [(poison, "ENGAGE")]  # capped: dead-lettered and alerted
    assert await svc.drain_outbox() == 0 and backend.outbox[0]["attempts"] == 2  # skipped now


async def test_a_broker_outage_never_dead_letters_a_stop(world):
    svc, backend, publisher = world
    svc.outbox_max_attempts = 2
    await svc.engage("BANK", "bank-01", "x", "op")
    for _ in range(5):
        await svc.drain_outbox()  # the broker is down: transient failures
    assert backend.outbox[0]["attempts"] == 0 and backend.alerts == []
    publisher.up = True
    assert await svc.drain_outbox() == 1


async def test_an_engage_goes_before_an_older_release(world):
    from .test_release_relay import _guardian_release, _Keys

    keys = _Keys()
    svc, _backend, publisher = world
    svc.guardian_public_key = keys.guardian_public
    await svc.relay_guardian_release(_guardian_release(keys))  # queued while the broker is down
    newer = await svc.engage("BANK", "bank-09", "z", "op")
    publisher.up = True
    await svc.drain_outbox()
    assert [action for _t, action in publisher.published] == ["ENGAGE", "RELEASE"]
    assert publisher.published[0][0].endswith(str(newer))


async def test_a_recorded_release_without_a_queued_publication_is_queued_on_redelivery(world):
    """H5 mirror for RELEASE: the guardian's re-hand-off of an event already recorded queues its publication."""
    from .test_release_relay import _guardian_release, _Keys

    keys = _Keys()
    svc, backend, publisher = world
    svc.guardian_public_key = keys.guardian_public
    event = _guardian_release(keys)
    backend.rows.append({"signature": event["signature"], "action": "RELEASE", "scope_ref": "bank-001"})
    publisher.up = True
    assert await svc.relay_guardian_release(event) is True
    assert [action for _t, action in publisher.published] == ["RELEASE"]
    await svc.relay_guardian_release(event)
    assert len(backend.outbox) == 1  # queued once


# --- r3.4.1: order within a scope, ENGAGE priority only across scopes; dead-letter sweep; non-fatal drain -------


def _entry(seq: int, action: str, scope: str) -> OutboxEntry:
    stop_id = uuid4()
    return OutboxEntry(seq, stop_id, action, f"stop/bank/{scope}/{stop_id}", {"action": action})  # type: ignore[arg-type]


def test_drain_order_keeps_a_scopes_own_order_and_prioritises_engages_across_scopes():
    from opengrid.safestop.service import drain_order

    release_x = _entry(1, "RELEASE", "bank-x")
    release_y = _entry(2, "RELEASE", "bank-y")
    engage_x = _entry(3, "ENGAGE", "bank-x")  # after X's older RELEASE: must not overtake it
    engage_z = _entry(4, "ENGAGE", "bank-z")  # another scope: goes before the RELEASEs
    order = drain_order([release_x, release_y, engage_x, engage_z])
    assert [e.seq for e in order] == [4, 1, 3, 2]
    assert order.index(release_x) < order.index(engage_x)


async def test_a_newer_engage_never_overtakes_an_older_release_of_its_own_scope(world):
    """The review scenario: RELEASE A for bank X queued while the broker is down, then ENGAGE B on X. On
    reconnect A must go first -- the hubs drop a RELEASE older than the newest ENGAGE on its scope, which would
    leave X stopped after B is released."""
    from .test_release_relay import _guardian_release, _Keys

    keys = _Keys()
    svc, _backend, publisher = world
    svc.guardian_public_key = keys.guardian_public
    await svc.relay_guardian_release(_guardian_release(keys))  # scope bank-001
    newer = await svc.engage("BANK", "bank-001", "again", "op")
    publisher.up = True
    await svc.drain_outbox()
    assert [action for _t, action in publisher.published] == ["RELEASE", "ENGAGE"]
    assert publisher.published[1][0].endswith(str(newer))


async def test_a_permanently_failing_entry_holds_back_only_its_own_scopes_releases(world):
    from .test_release_relay import _guardian_release, _Keys

    keys = _Keys()
    svc, _backend, publisher = world
    svc.guardian_public_key = keys.guardian_public
    svc.outbox_max_attempts = 3
    first = await svc.engage("BANK", "bank-001", "a", "op")
    await svc.relay_guardian_release(_guardian_release(keys))  # a RELEASE on bank-001, behind `first`
    other = await svc.engage("BANK", "bank-y", "c", "op")
    publisher.up = True
    real = publisher.publish_retained

    async def reject_first(topic_suffix: str, payload: dict[str, Any]) -> None:
        if topic_suffix.endswith(str(first)):
            raise StopPublishError("invalid", transient=False)
        await real(topic_suffix, payload)

    publisher.publish_retained = reject_first  # type: ignore[method-assign]
    await svc.drain_outbox()
    assert publisher.published == [(publisher.published[0][0], "ENGAGE")]  # bank-001's RELEASE held
    assert publisher.published[0][0].endswith(str(other))
    await svc.drain_outbox()
    await svc.drain_outbox()  # `first` reaches the cap: dead-lettered, bank-001 moves on
    assert [action for _t, action in publisher.published] == ["ENGAGE", "RELEASE"]


async def test_a_failing_entry_never_holds_back_an_engage_on_its_scope(world):
    """r3.4.2 review L-3: an ENGAGE never waits behind a permanently failing entry of its own scope."""
    svc, _backend, publisher = world
    first = await svc.engage("BANK", "bank-x", "a", "op")
    second = await svc.engage("BANK", "bank-x", "b", "op")
    publisher.up = True
    real = publisher.publish_retained

    async def reject_first(topic_suffix: str, payload: dict[str, Any]) -> None:
        if topic_suffix.endswith(str(first)):
            raise StopPublishError("invalid", transient=False)
        await real(topic_suffix, payload)

    publisher.publish_retained = reject_first  # type: ignore[method-assign]
    assert await svc.drain_outbox() == 1
    assert publisher.published[0][0].endswith(str(second))


async def test_an_engage_behind_a_long_backlog_is_published_first(world):
    """r3.4.2 review L-2: ENGAGE priority reaches past the drain batch (the oldest 100 entries)."""
    svc, backend, publisher = world
    for i in range(120):  # 120 queued RELEASE publications on other scopes, older than the stop
        await backend.enqueue_publication(
            stop_id=uuid4(),
            action="RELEASE",
            topic_suffix=f"stop/bank/r-{i}/{uuid4()}",
            payload={"action": "RELEASE"},
        )
    publisher.up = True
    stop_id = await svc.engage("BANK", "bank-late", "x", "op")  # the drain after accept publishes it
    assert publisher.published[0] == (publisher.published[0][0], "ENGAGE")
    assert publisher.published[0][0].endswith(str(stop_id))


@dataclass
class _Trace:
    events: list[dict[str, Any]] = field(default_factory=list)

    async def append(self, stream_id, decision_type, event_class, payload, reason_codes=None) -> object:
        self.events.append(dict(payload))
        return None


async def test_a_dead_lettered_engage_is_published_by_the_sweep_and_traced(world):
    """r3.4.2 review L-4: an ENGAGE dead-lettered before the upgrade (its attempts also counted outages) is a stop
    that never reached the hubs: the sweep publishes it as soon as it can, and traces it."""
    svc, backend, publisher = world
    svc.trace = _Trace()  # type: ignore[assignment]
    stop_id = await svc.engage("BANK", "bank-07", "x", "op")
    backend.outbox[0]["attempts"] = svc.outbox_max_attempts  # found at the cap after an upgrade
    publisher.up = True
    assert await svc.drain_outbox() == 1
    assert publisher.published[0][0].endswith(str(stop_id)) and backend.outbox[0]["published"] is True
    assert [e["outbox"] for e in svc.trace.events if "outbox" in e] == ["DEAD_LETTER_PUBLISHED"]  # type: ignore[attr-defined]


async def test_a_dead_lettered_release_is_alerted_and_traced_once(world):
    from .test_release_relay import _guardian_release, _Keys

    keys = _Keys()
    svc, backend, publisher = world
    svc.guardian_public_key = keys.guardian_public
    svc.trace = _Trace()  # type: ignore[assignment]
    await svc.relay_guardian_release(_guardian_release(keys))
    backend.outbox[0]["attempts"] = svc.outbox_max_attempts
    publisher.up = True
    await svc.drain_outbox()
    await svc.drain_outbox()
    assert publisher.published == []  # a RELEASE is never re-tried from the dead letters
    assert len(backend.alerts) == 1
    assert [e["outbox"] for e in svc.trace.events if "outbox" in e] == ["DEAD_LETTER"]  # type: ignore[attr-defined]


async def test_an_entry_dead_lettered_without_its_alert_is_alerted_on_the_next_drain(world):
    """A crash between the failure write and the alert, or an upgrade finding rows already at the cap: the
    drain's sweep raises the missing alert (idempotently)."""
    svc, backend, _publisher = world
    stop_id = await svc.engage("BANK", "bank-07", "x", "op")
    backend.outbox[0]["attempts"] = svc.outbox_max_attempts  # at the cap, no alert yet
    await svc.drain_outbox()
    await svc.drain_outbox()
    assert backend.alerts == [(stop_id, "ENGAGE")]


async def test_a_drain_failure_after_accept_never_fails_the_caller(world):
    """Another entry's failure (here a database error in the drain) must not fail engage() -- the L2 intake
    would retry it and the API intake task would die -- the stop is recorded and queued for the next drain."""
    svc, backend, _publisher = world

    async def broken(*_a: Any, **_k: Any) -> list[OutboxEntry]:
        raise RuntimeError("database hiccup")

    backend.pending_publications = broken  # type: ignore[method-assign]
    stop_id = await svc.engage("BANK", "bank-07", "x", "op")  # does not raise
    assert backend.rows[0]["stop_event_id"] == stop_id and backend.outbox[0]["published"] is False
