"""`opengrid.safestop.l2_intake.handle_instruction`: og-safestop's own BLOCK/ESTOP -> BANK stop (K5/K8)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest

from opengrid.core.crypto import generate_keypair
from opengrid.safestop.keys import StopSigningKey
from opengrid.safestop.l2_intake import handle_instruction
from opengrid.safestop.pg_backend import PgStopEventBackend
from opengrid.safestop.service import SafestopService

from ._fake_pool import FakePool

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _instruction(kind: str = "BLOCK", **overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "instruction_id": str(uuid4()),
        "bank_id": "bank-007",
        "kind": kind,
        "limit_kw": 50.0 if kind == "LIMIT" else None,
        "issued_at": (NOW - timedelta(seconds=5)).isoformat(),
        "expires_at": (NOW + timedelta(minutes=10)).isoformat(),
        "issued_by": "utility-scada-1",
    }
    data.update(overrides)
    return data


def _bytes(data: dict[str, Any]) -> bytes:
    return json.dumps(data).encode("utf-8")


@dataclass
class _Harness:
    engaged: list[tuple[str, str, str]] = field(default_factory=list)
    acted: set[tuple[UUID, str]] = field(default_factory=set)
    handled: set[UUID] = field(default_factory=set)

    async def engage(self, bank_id: str, reason: str, initiator_ref: str) -> None:
        self.engaged.append((bank_id, reason, initiator_ref))
        self.acted.add((UUID(reason.split()[2]), bank_id))  # what the stop_event row would record

    async def already_acted(self, instruction_id: UUID, bank_id: str) -> bool:
        return (instruction_id, bank_id) in self.acted

    async def handle(self, message: Any, **kw: Any) -> str:
        return await handle_instruction(
            message,
            now=kw.pop("now", NOW),
            handled=self.handled,
            engage_fn=self.engage,
            already_acted_fn=self.already_acted,
            **kw,
        )


@pytest.mark.parametrize("kind", ["BLOCK", "ESTOP"])
async def test_block_and_estop_engage_a_bank_stop(kind):
    h = _Harness()
    data = _instruction(kind)
    assert await h.handle(_bytes(data), topic_bank_id="bank-007") == "ENGAGED"
    assert h.engaged == [
        ("bank-007", f"L2 {kind} {data['instruction_id']} from utility-scada-1", "utility:utility-scada-1")
    ]


async def test_limit_is_ignored():
    h = _Harness()
    assert await h.handle(_bytes(_instruction("LIMIT"))) == "IGNORED_LIMIT"
    assert h.engaged == []


@pytest.mark.parametrize("offset", [timedelta(0), timedelta(seconds=-1)])
async def test_expired_is_ignored(offset):
    h = _Harness()
    data = _instruction("ESTOP", expires_at=(NOW + offset).isoformat())
    assert await h.handle(_bytes(data)) == "IGNORED_EXPIRED"
    assert h.engaged == []


async def test_no_expiry_engages():
    h = _Harness()
    assert await h.handle(_bytes(_instruction("BLOCK", expires_at=None))) == "ENGAGED"


async def test_duplicate_instruction_engages_once_in_memory():
    h = _Harness()
    data = _instruction("BLOCK")
    assert await h.handle(_bytes(data)) == "ENGAGED"
    assert await h.handle(_bytes(data)) == "DUPLICATE"
    assert await h.handle(_bytes(data)) == "DUPLICATE"
    assert len(h.engaged) == 1


async def test_duplicate_after_restart_is_caught_by_already_acted():
    h = _Harness()
    data = _instruction("ESTOP")
    assert await h.handle(_bytes(data)) == "ENGAGED"

    h.handled.clear()  # "restart": in-memory set lost, stop_event rows survive
    assert await h.handle(_bytes(data)) == "ALREADY_ACTED"
    assert len(h.engaged) == 1
    assert UUID(data["instruction_id"]) in h.handled


async def test_new_instruction_for_already_stopped_bank_still_engages():
    h = _Harness()
    assert await h.handle(_bytes(_instruction("BLOCK"))) == "ENGAGED"
    assert await h.handle(_bytes(_instruction("ESTOP"))) == "ENGAGED"
    assert len(h.engaged) == 2


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        b"[]",
        b"{}",
        _bytes(_instruction("REBOOT")),
        _bytes({**_instruction("BLOCK"), "instruction_id": "nope"}),
        _bytes({**_instruction("BLOCK"), "extra": 1}),
        _bytes(_instruction("BLOCK", bank_id="bank/#")),
    ],
)
async def test_malformed_is_ignored(payload):
    h = _Harness()
    assert await h.handle(payload) == "MALFORMED"
    assert h.engaged == []


async def test_topic_bank_mismatch_is_refused():
    h = _Harness()
    assert await h.handle(_bytes(_instruction("ESTOP")), topic_bank_id="bank-999") == "MALFORMED"
    assert h.engaged == []


async def test_failed_engage_is_not_marked_handled_and_retries_on_redelivery():
    h = _Harness()
    data = _instruction("BLOCK")
    calls = 0

    async def flaky(bank_id: str, reason: str, initiator_ref: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("broker down")
        await h.engage(bank_id, reason, initiator_ref)

    kw = {"now": NOW, "handled": h.handled, "already_acted_fn": h.already_acted}
    assert await handle_instruction(_bytes(data), engage_fn=flaky, **kw) == "FAILED"
    assert await handle_instruction(_bytes(data), engage_fn=flaky, **kw) == "ENGAGED"
    assert len(h.engaged) == 1


async def test_engage_through_real_service_traces_instruction_id():
    """End to end over in-memory fakes: the SAFE_STOP trace and the stop_event row both carry the id,
    and the row is UTILITY-initiated on the bank."""
    rows: list[dict[str, Any]] = []
    traced: list[dict[str, Any]] = []
    published: list[str] = []

    class _Backend:
        async def insert_stop_event(self, **kwargs: Any) -> None:
            rows.append(kwargs)

    class _Publisher:
        async def publish_retained(self, topic_suffix: str, payload: dict[str, Any]) -> None:
            published.append(topic_suffix)

    class _Trace:
        async def append(self, stream_id, decision_type, event_class, payload, reason_codes=None):
            traced.append(payload)

    seed, _pub = generate_keypair()
    service = SafestopService(StopSigningKey("safestop-test", seed), _Backend(), _Publisher(), _Trace())  # type: ignore[arg-type]

    async def engage(bank_id: str, reason: str, initiator_ref: str) -> None:
        await service.engage("BANK", bank_id, reason, initiator_ref, initiator_kind="UTILITY")

    async def never(_iid: UUID, _bank: str) -> bool:
        return False

    data = _instruction("ESTOP")
    outcome = await handle_instruction(
        _bytes(data), now=NOW, handled=set(), engage_fn=engage, already_acted_fn=never
    )
    assert outcome == "ENGAGED"
    assert data["instruction_id"] in traced[0]["reason"]
    assert traced[0]["issued_by"] == "utility:utility-scada-1"
    assert rows[0]["initiator_kind"] == "UTILITY"
    assert rows[0]["scope_kind"] == "BANK"
    assert rows[0]["scope_ref"] == "bank-007"
    assert data["instruction_id"] in rows[0]["reason"]
    assert published[0].startswith("stop/bank/bank-007/")


async def test_pg_has_l2_engage_queries_by_bank_and_instruction_id():
    iid = uuid4()
    pool = FakePool(fetchone_result=(1,))
    assert await PgStopEventBackend(pool).has_l2_engage(iid, "bank-007") is True  # type: ignore[arg-type]
    _kw, params = pool.executed[0]
    assert params == {"bank_id": "bank-007", "instruction_id": str(iid)}

    empty = FakePool(fetchone_result=None)
    assert await PgStopEventBackend(empty).has_l2_engage(iid, "bank-007") is False  # type: ignore[arg-type]
