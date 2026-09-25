"""Unit tests for `opengrid.safestop.main`'s request-handling logic (the PROPOSE/CONFIRM protocol
documented in README.md), independent of any real Postgres/MQTT connection."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import pytest

import opengrid.safestop as safestop
import opengrid.safestop.main as safestop_main
from opengrid.safestop.confirmation import ConfirmationBroker


@dataclass
class _FakeService:
    engaged: list[tuple[str, str, str, str]] = field(default_factory=list)

    async def engage(self, scope: str, scope_ref: str, reason: str, initiator_ref: str) -> None:
        self.engaged.append((scope, scope_ref, reason, initiator_ref))

    async def release(self, scope: str, scope_ref: str, approver_ref: str) -> None:
        raise AssertionError("release should never be called from request handling")


@pytest.fixture(autouse=True)
def _wired_fake_service():
    fake: Any = _FakeService()
    safestop.configure_service(fake)
    yield fake
    safestop.configure_service(None)


async def test_propose_alone_does_not_engage(_wired_fake_service):
    broker = ConfirmationBroker()
    await safestop_main._handle_request(
        {
            "action": "PROPOSE",
            "proposal_id": str(uuid4()),
            "scope": "BANK",
            "scope_ref": "bank-07",
            "reason": "drill",
            "initiator_ref": "operator:alice",
        },
        broker,
    )
    assert _wired_fake_service.engaged == []
    assert broker.pending_count() == 1


async def test_propose_then_confirm_engages_exactly_once(_wired_fake_service):
    broker = ConfirmationBroker()
    proposal_id = str(uuid4())
    await safestop_main._handle_request(
        {
            "action": "PROPOSE",
            "proposal_id": proposal_id,
            "scope": "ZONE",
            "scope_ref": "LZ_NORTH",
            "reason": "drill",
            "initiator_ref": "operator:bob",
        },
        broker,
    )
    await safestop_main._handle_request({"action": "CONFIRM", "proposal_id": proposal_id}, broker)

    assert _wired_fake_service.engaged == [("ZONE", "LZ_NORTH", "drill", "operator:bob")]
    assert broker.pending_count() == 0


async def test_confirm_without_matching_propose_does_not_engage(_wired_fake_service):
    broker = ConfirmationBroker()
    await safestop_main._handle_request({"action": "CONFIRM", "proposal_id": str(uuid4())}, broker)
    assert _wired_fake_service.engaged == []


async def test_unrecognised_action_is_ignored(_wired_fake_service):
    broker = ConfirmationBroker()
    await safestop_main._handle_request({"action": "RELEASE_PLEASE"}, broker)
    assert _wired_fake_service.engaged == []
