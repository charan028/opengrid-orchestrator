from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from opengrid.core.crypto import generate_keypair, verify_payload
from opengrid.safestop.keys import StopSigningKey
from opengrid.safestop.service import ReleaseNotPermittedError, SafestopService


@dataclass
class FakeBackend:
    rows: list[dict[str, Any]] = field(default_factory=list)

    async def insert_stop_event(self, **kwargs: Any) -> None:
        self.rows.append(kwargs)

    async def latest_action(self, scope_kind: str, scope_ref: str) -> str | None:
        matching = [r for r in self.rows if r["scope_kind"] == scope_kind and r["scope_ref"] == scope_ref]
        return matching[-1]["action"] if matching else None


@dataclass
class FakePublisher:
    published: list[tuple[str, dict[str, Any]]] = field(default_factory=list)

    async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
        self.published.append((topic_suffix, payload))


@dataclass
class FakeTrace:
    appended: list[tuple[str, str, str, dict[str, Any], list[str] | None]] = field(default_factory=list)

    async def append(self, stream_id, decision_type, event_class, payload, reason_codes=None):
        self.appended.append((stream_id, decision_type, event_class, payload, reason_codes))
        return None


@pytest.fixture
def stop_key() -> StopSigningKey:
    seed, _pub = generate_keypair()
    return StopSigningKey(key_id="safestop-test", seed=seed)


@pytest.fixture
def service(stop_key: StopSigningKey) -> tuple[SafestopService, FakeBackend, FakePublisher, FakeTrace]:
    backend = FakeBackend()
    publisher = FakePublisher()
    trace = FakeTrace()
    return SafestopService(stop_key, backend, publisher, trace), backend, publisher, trace


async def test_engage_writes_trace_then_stop_event_then_publishes(service):
    svc, backend, publisher, trace = service

    await svc.engage("BANK", "bank-07", "overload drill", "operator:alice")

    assert len(trace.appended) == 1
    assert trace.appended[0][1] == "SAFE_STOP"
    assert len(backend.rows) == 1
    row = backend.rows[0]
    assert row["scope_kind"] == "BANK"
    assert row["scope_ref"] == "bank-07"
    assert row["action"] == "ENGAGE"
    assert row["initiator_ref"] == "operator:alice"
    assert row["approver_ref"] is None
    assert len(publisher.published) == 1
    topic_suffix, payload = publisher.published[0]
    assert topic_suffix.startswith("stop/bank/bank-07/")
    assert payload["action"] == "ENGAGE"


async def test_engage_fleet_scope_uses_fleet_scope_ref(service):
    svc, backend, _publisher, _trace = service
    await svc.engage("FLEET", "", "chaos drill", "operator:bob")
    assert backend.rows[0]["scope_kind"] == "FLEET"
    assert backend.rows[0]["scope_ref"] == "FLEET"


async def test_engage_signature_verifies_with_the_stop_key_public_key(service, stop_key):
    svc, _backend, publisher, _trace = service
    from opengrid.core.crypto import private_key_from_seed

    pub = private_key_from_seed(stop_key.seed).public_key().public_bytes_raw()

    await svc.engage("ZONE", "LZ_NORTH", "drill", "operator:carol")
    _topic_suffix, payload = publisher.published[0]
    signing_payload = {k: v for k, v in payload.items() if k not in ("key_id", "signature")}
    assert verify_payload(pub, signing_payload, payload["signature"])


async def test_engage_works_without_a_trace_store(stop_key):
    backend = FakeBackend()
    publisher = FakePublisher()
    svc = SafestopService(stop_key, backend, publisher, trace=None)

    await svc.engage("BANK", "bank-01", "no trace configured", "operator:dave")

    assert len(backend.rows) == 1
    assert len(publisher.published) == 1


@pytest.mark.parametrize(
    ("scope", "scope_ref", "approver_ref"),
    [
        ("FLEET", "", "operator:alice"),
        ("ZONE", "LZ_NORTH", "operator:bob"),
        ("BANK", "bank-07", ""),
        ("BANK", "bank-07", "guardian:tier2-co-signed"),
    ],
)
async def test_release_always_raises_regardless_of_input(service, scope, scope_ref, approver_ref):
    """K8: the stop-only key can never release, under any input -- including an approver_ref that
    *claims* guardian co-signature; safestop has no way to verify that claim and must not try."""
    svc, backend, publisher, _trace = service
    with pytest.raises(ReleaseNotPermittedError):
        await svc.release(scope, scope_ref, approver_ref)
    assert backend.rows == []  # never wrote a RELEASE row
    assert publisher.published == []  # never published anything
