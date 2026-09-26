"""K8 / crypto.md S2.3, og-safestop's half of a stop RELEASE: it relays ONLY a guardian-signed, Tier-2
RELEASE it has verified itself, publishes it on the ENGAGE's own retained topic, and records it. The
stop-only key still signs nothing but ENGAGE, and `release()` still always refuses."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

import opengrid.safestop as safestop
import opengrid.safestop.main as safestop_main
from opengrid.core.crypto import generate_keypair, sign_payload
from opengrid.guardian import stop_release
from opengrid.guardian.ports import EngagedStop, ReleaseRequest
from opengrid.platform.config import Config
from opengrid.safestop.confirmation import ConfirmationBroker
from opengrid.safestop.events import stop_topic_suffix
from opengrid.safestop.keys import StopSigningKey
from opengrid.safestop.service import ReleaseNotPermittedError, SafestopService

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
ENGAGE_ID = UUID("00000000-0000-4000-8000-0000000000e1")


@dataclass
class _Backend:
    rows: list[dict[str, Any]] = field(default_factory=list)

    async def insert_stop_event(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)

    async def has_signature(self, signature: str) -> bool:
        return any(row["signature"] == signature for row in self.rows)

    async def latest_action(self, scope_kind: str, scope_ref: str) -> str | None:
        return None


@dataclass
class _Publisher:
    published: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    cleared: list[str] = field(default_factory=list)

    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
        self.published.append((topic_suffix, payload))

    async def clear_retained(self, topic_suffix: str) -> None:
        self.cleared.append(topic_suffix)


@dataclass
class _Trace:
    rows: list[tuple] = field(default_factory=list)

    async def append(self, *args: Any, **kwargs: Any) -> None:
        self.rows.append((args, kwargs))


class _Keys:
    def __init__(self) -> None:
        self.guardian_seed, self.guardian_public = generate_keypair()
        self.safestop_seed, _ = generate_keypair()


@pytest.fixture
def keys() -> _Keys:
    return _Keys()


def _service(keys: _Keys, *, with_key: bool = True) -> tuple[SafestopService, _Backend, _Publisher, _Trace]:
    backend, publisher, trace = _Backend(), _Publisher(), _Trace()
    service = SafestopService(
        StopSigningKey("safestop-test", keys.safestop_seed),
        backend,
        publisher,
        trace,
        guardian_public_key=keys.guardian_public if with_key else None,
    )
    return service, backend, publisher, trace


def _guardian_release(keys: _Keys, **request_overrides: Any) -> dict[str, Any]:
    """A RELEASE exactly as og-guardian signs it (`stop_release.build_release_events`)."""
    request: dict[str, Any] = {
        "operator_action_id": uuid4(),
        "requested_by": "alice",
        "approved_by": "bob",
        "scope_kind": "BANK",
        "scope_ref": "bank-001",
        "reason": "feeder repaired",
        "requested_at": NOW - timedelta(minutes=2),
        "approved_at": NOW - timedelta(minutes=1),
        "trace_id": uuid4(),
    }
    request.update(request_overrides)
    (event,) = stop_release.build_release_events(
        ReleaseRequest(**request),
        [
            EngagedStop(
                stop_id=ENGAGE_ID, initiator_kind="SAFESTOP_AUTHORITY", engaged_at=NOW - timedelta(hours=1)
            )
        ],
        seed=keys.guardian_seed,
        key_id="guardian-2026a",
        issued_at=NOW,
    )
    return event.model_dump(mode="json")


def _resign(event: dict[str, Any], seed: bytes, **changes: Any) -> dict[str, Any]:
    changed = {**event, **changes}
    signed = {k: v for k, v in changed.items() if k not in ("key_id", "signature")}
    return {**changed, "signature": sign_payload(seed, signed)}


async def test_a_guardian_signed_release_is_published_on_the_engage_topic_and_recorded(keys):
    service, backend, publisher, trace = _service(keys)
    event = _guardian_release(keys)

    assert await service.relay_guardian_release(event) is True

    assert publisher.published == [(stop_topic_suffix("BANK", "bank-001", ENGAGE_ID), event)]
    (row,) = backend.rows
    assert (row["action"], row["scope_kind"], row["scope_ref"]) == ("RELEASE", "BANK", "bank-001")
    assert (row["initiator_kind"], row["initiator_ref"], row["approver_ref"]) == ("GUARDIAN", "alice", "bob")
    assert row["signature"] == event["signature"]
    assert len(trace.rows) == 1


async def test_relay_is_idempotent_by_signature(keys):
    service, backend, publisher, _ = _service(keys)
    event = _guardian_release(keys)
    assert await service.relay_guardian_release(event)
    assert await service.relay_guardian_release(event)
    assert len(publisher.published) == 1 and len(backend.rows) == 1


@pytest.mark.parametrize(
    "forge",
    [
        "safestop_key",
        "stranger_key",
        "unsigned",
        "tampered_scope",
        "no_approver",
        "same_person",
        "engage_action",
        "safestop_key_id",
        "not_an_event",
        "malformed",
    ],
)
async def test_anything_but_a_valid_guardian_tier2_release_is_refused(keys, forge):
    service, backend, publisher, _ = _service(keys)
    event = _guardian_release(keys)
    stranger_seed, _ = generate_keypair()
    candidate: Any = {
        "safestop_key": _resign(event, keys.safestop_seed),
        "stranger_key": _resign(event, stranger_seed),
        "unsigned": {**event, "signature": "x"},
        "tampered_scope": {**event, "scope_id": "bank-002"},
        "no_approver": _resign(event, keys.guardian_seed, approver_ref=None),
        "same_person": _resign(event, keys.guardian_seed, approver_ref="ALICE"),
        "engage_action": _resign(event, keys.guardian_seed, action="ENGAGE"),
        "safestop_key_id": {**event, "key_id": "safestop-2026a"},
        "not_an_event": ["RELEASE"],
        "malformed": {"action": "RELEASE"},
    }[forge]

    assert await service.relay_guardian_release(candidate) is False
    assert publisher.published == [] and backend.rows == []


async def test_without_the_guardian_public_key_nothing_is_relayed(keys):
    service, backend, publisher, _ = _service(keys, with_key=False)
    assert await service.relay_guardian_release(_guardian_release(keys)) is False
    assert publisher.published == [] and backend.rows == []


async def test_safestop_itself_still_never_releases(keys):
    service, *_ = _service(keys)
    with pytest.raises(ReleaseNotPermittedError):
        await service.release("BANK", "bank-001", "bob")


async def test_publish_release_requests_are_routed_to_the_relay(keys):
    service, backend, publisher, _ = _service(keys)
    safestop.configure_service(service)
    try:
        event = _guardian_release(keys)
        await safestop_main._handle_request(
            {"action": "PUBLISH_RELEASE", "event": event}, ConfirmationBroker()
        )
        await safestop_main._handle_request(
            {"action": "PUBLISH_RELEASE", "event": "nope"}, ConfirmationBroker()
        )
    finally:
        safestop.configure_service(None)
    assert len(publisher.published) == 1 and len(backend.rows) == 1


def test_guardian_public_key_loading(tmp_path, keys):
    good = tmp_path / "guardian.pub"
    good.write_text(keys.guardian_public.hex() + "\n", encoding="ascii")
    bad = tmp_path / "bad.pub"
    bad.write_text("zz", encoding="ascii")

    assert safestop_main.load_guardian_public_key(
        Config({"safestop": {"guardian_public_key_path": str(good)}})
    ) == (keys.guardian_public)
    assert (
        safestop_main.load_guardian_public_key(Config({"safestop": {"guardian_public_key_path": str(bad)}}))
        is None
    )
    missing = str(tmp_path / "missing.pub")
    assert (
        safestop_main.load_guardian_public_key(Config({"safestop": {"guardian_public_key_path": missing}}))
        is None
    )


_field = st.sampled_from(
    ["stop_id", "scope", "scope_id", "action", "reason", "issued_by", "issued_at", "approver_ref"]
)


@settings(max_examples=60, deadline=None)
@given(_field, st.one_of(st.none(), st.text(max_size=12), st.integers()), st.booleans())
def test_k8_property_no_altered_or_foreign_release_is_ever_relayed(field_name, value, resign_with_safestop):
    """Change any signed field of a valid guardian RELEASE (keeping the guardian signature), or re-sign it
    with the stop-only key: og-safestop relays nothing."""
    import asyncio

    keys = _Keys()
    service, backend, publisher, _ = _service(keys)
    event = _guardian_release(keys)
    candidate = _resign(event, keys.safestop_seed) if resign_with_safestop else {**event, field_name: value}
    if candidate == event:
        return
    assert asyncio.run(service.relay_guardian_release(candidate)) is False
    assert publisher.published == [] and backend.rows == []


# --- K8 housekeeping: clear a released stop's retained topic after the retention window -------------------


@dataclass
class _Housekeeping:
    due: list[dict[str, Any]]
    retain_s: float | None = None

    async def releases_due_for_clearing(self, *, retain_s: float, limit: int) -> list[dict[str, Any]]:
        self.retain_s = retain_s
        return list(self.due)


async def test_released_stop_topics_are_cleared_after_the_retention_window(keys):
    service, _backend, publisher, trace = _service(keys)
    event = _guardian_release(keys)
    housekeeping = _Housekeeping([event, {"not": "an event"}])

    cleared = await service.clear_released_retained(housekeeping, retain_s=86_400.0)

    assert cleared == 1 and housekeeping.retain_s == 86_400.0
    assert publisher.cleared == [stop_topic_suffix("BANK", "bank-001", ENGAGE_ID)]
    assert publisher.published == []  # housekeeping never publishes a stop event
    (args, _kwargs) = trace.rows[-1]
    assert args[3]["housekeeping"] == "RETAINED_CLEARED" and args[3]["stop_id"] == str(ENGAGE_ID)


async def test_aiomqtt_publisher_clears_with_a_zero_length_retained_payload():
    from opengrid.safestop.mqtt_publish import AiomqttStopPublisher

    sent: list[dict[str, Any]] = []

    class _Client:
        async def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
            sent.append({"topic": topic, "payload": payload, "qos": qos, "retain": retain})

    publisher = AiomqttStopPublisher(client=_Client(), config=Config({"mqtt": {"topic_root": "ogtest/x"}}))  # type: ignore[arg-type]
    await publisher.clear_retained("stop/bank/bank-001/abc")
    assert sent == [{"topic": "ogtest/x/stop/bank/bank-001/abc", "payload": b"", "qos": 1, "retain": True}]


async def test_due_releases_query_reads_only_uncleared_safestop_release_rows():
    from opengrid.safestop.pg_backend import PgStopEventBackend

    from ._fake_pool import FakePool

    pool = FakePool()
    pool.fetchall_result = [({"action": "RELEASE", "stop_id": "s"},)]
    backend = PgStopEventBackend(pool)  # type: ignore[arg-type]
    assert await backend.releases_due_for_clearing(retain_s=60.0, limit=5) == [
        {"action": "RELEASE", "stop_id": "s"}
    ]
