"""`opengrid.guardian.repo`'s Postgres-backed port adapters, exercised against a minimal in-memory fake
of the psycopg async pool/cursor protocol (no real database -- BUILD.md S5's "Local: unit and property
tests, no DB/MQTT")."""

from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.guardian import repo


class FakeCursor:
    """Each `.execute()` call consumes the next canned response from `responses` (a row tuple/None for
    a fetchone-style query, or a list of rows for a fetchall-style query)."""

    def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self.executed: list[tuple[str, dict]] = []
        self._current = None

    async def execute(self, sql, params=None):
        self.executed.append((sql, params))
        self._current = self._responses.pop(0) if self._responses else None

    async def fetchone(self):
        return self._current

    async def fetchall(self):
        return self._current or []

    @property
    def rowcount(self) -> int:
        return len(self._current) if isinstance(self._current, list) else 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeConn:
    def __init__(self, cursor: FakeCursor) -> None:
        self._cursor = cursor
        self.committed = False

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, cursor: FakeCursor) -> None:
        self._conn = FakeConn(cursor)

    def connection(self):
        return self._conn


async def test_pg_hub_state_port_found():
    cursor = FakeCursor([(39.2, 7.84, 11.0, 0.9487, 0.9487, 20.0, 3.0, "online")])
    port = repo.PgHubStatePort(FakePool(cursor))
    snap = await port.snapshot("hub-1")
    assert snap is not None
    assert snap.soc_kwh == 20.0 and snap.prev_p_kw == 3.0 and snap.health == "online"
    assert snap.params.p_kw == 11.0


async def test_pg_hub_state_port_missing():
    port = repo.PgHubStatePort(FakePool(FakeCursor([None])))
    assert await port.snapshot("nope") is None


async def test_pg_bank_state_port_found_with_load():
    cursor = FakeCursor([(75.0, 5.0, "feeder-1"), (42.0,)])
    port = repo.PgBankStatePort(FakePool(cursor))
    snap = await port.snapshot("bank-1")
    assert snap is not None
    assert snap.feeder_id == "feeder-1"
    assert snap.bank_load_kva == 42.0


async def test_pg_bank_state_port_missing_bank():
    cursor = FakeCursor([None])
    port = repo.PgBankStatePort(FakePool(cursor))
    assert await port.snapshot("nope") is None


async def test_pg_bank_state_port_no_load_defaults_zero():
    cursor = FakeCursor([(75.0, 5.0, None), None])
    port = repo.PgBankStatePort(FakePool(cursor))
    snap = await port.snapshot("bank-1")
    assert snap is not None
    assert snap.bank_load_kva == 0.0


async def test_pg_commitment_port_found_and_default():
    obligation_id = uuid4()
    found = repo.PgCommitmentPort(FakePool(FakeCursor([(Decimal("5.0"),)])))
    assert await found.active_kw(obligation_id, "cycle-1") == Decimal("5.0")

    missing = repo.PgCommitmentPort(FakePool(FakeCursor([None])))
    assert await missing.active_kw(obligation_id, "cycle-1") == Decimal(0)


async def test_pg_prior_grant_port_found_and_missing():
    obligation_id = uuid4()
    found = repo.PgPriorGrantPort(FakePool(FakeCursor([(Decimal("3.0"),)])))
    assert await found.prior_granted_kw(obligation_id) == Decimal("3.0")

    missing = repo.PgPriorGrantPort(FakePool(FakeCursor([None])))
    assert await missing.prior_granted_kw(obligation_id) is None


async def test_in_memory_lease_state_defaults_and_records():
    leases = repo.InMemoryLeaseStatePort()
    assert await leases.last_accepted("bank-1") == (0, 0)
    leases.record_accepted("bank-1", 3, 7)
    assert await leases.last_accepted("bank-1") == (3, 7)


async def test_pg_l2_instruction_port_active_and_none():
    active = repo.PgL2InstructionPort(FakePool(FakeCursor([("LIMIT", 10.0)])))
    instruction = await active.active_instruction("bank-1")
    assert instruction is not None and instruction.kind == "LIMIT" and instruction.limit_kw == 10.0

    none_port = repo.PgL2InstructionPort(FakePool(FakeCursor([None])))
    assert await none_port.active_instruction("bank-1") is None


async def test_pg_safe_stop_port_engaged_and_clear():
    engaged = repo.PgSafeStopPort(FakePool(FakeCursor([("ENGAGE",)])))
    assert await engaged.is_stopped("BANK", "bank-1") is True

    released = repo.PgSafeStopPort(FakePool(FakeCursor([("RELEASE",)])))
    assert await released.is_stopped("BANK", "bank-1") is False

    never = repo.PgSafeStopPort(FakePool(FakeCursor([None])))
    assert await never.is_stopped("BANK", "bank-1") is False


def _proposal_payload(command_batch_id) -> dict:
    return {
        "command_batch_id": str(command_batch_id),
        "bank_id": "bank-1",
        "cycle_id": "cycle-1",
        "epoch": 1,
        "seq": 1,
        "issued_at": "2026-09-26T18:00:00+00:00",
        "expires_at": "2026-09-26T18:00:10+00:00",
        "ledger_version": 1,
        "is_firm_event": False,
        "items": [
            {
                "hub_id": "hub-1",
                "p_kw_setpoint": 3.0,
                "reason_code": "SELECTOR",
                "obligation_id": None,
                "obligation_granted_kw": None,
            }
        ],
    }


async def test_pg_proposal_port_found():
    command_batch_id = uuid4()
    cursor = FakeCursor([(_proposal_payload(command_batch_id),)])
    port = repo.PgProposalPort(FakePool(cursor))
    proposal = await port.fetch(command_batch_id)
    assert proposal is not None
    assert proposal.bank_id == "bank-1"
    assert proposal.items[0].hub_id == "hub-1"


async def test_pg_proposal_port_missing():
    port = repo.PgProposalPort(FakePool(FakeCursor([None])))
    assert await port.fetch(uuid4()) is None


async def test_pg_proposal_port_malformed_payload_returns_none():
    cursor = FakeCursor([({"bank_id": "bank-1"},)])  # missing required keys
    port = repo.PgProposalPort(FakePool(cursor))
    assert await port.fetch(uuid4()) is None


async def test_load_hub_params():
    cursor = FakeCursor([[("hub-1", 39.2, 7.84, 11.0, 0.9487, 0.9487)]])
    params = await repo.load_hub_params(FakePool(cursor))
    assert set(params) == {"hub-1"}
    assert params["hub-1"].health == "stale"
    assert params["hub-1"].params.p_kw == 11.0


async def test_chrony_clock_port_falls_back_to_zero_when_unavailable(monkeypatch):
    async def fail_exec(*args, **kwargs):
        raise OSError("chronyc not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_exec)
    clock = repo.ChronyClockPort()
    assert await clock.offset_from_ntp_ms() == 0.0


async def test_chrony_clock_port_parses_system_time_line(monkeypatch):
    class FakeProcess:
        async def communicate(self):
            return b"System time     : 0.000123456 seconds fast of NTP time\n", b""

    async def fake_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    clock = repo.ChronyClockPort()
    offset = await clock.offset_from_ntp_ms()
    assert offset == pytest.approx(0.123456)


async def test_chrony_clock_port_missing_system_time_line_returns_zero(monkeypatch):
    class FakeProcess:
        async def communicate(self):
            return b"Reference ID    : ABCD1234\n", b""

    async def fake_exec(*args, **kwargs):
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    clock = repo.ChronyClockPort()
    assert await clock.offset_from_ntp_ms() == 0.0


async def test_build_pg_ports_wires_everything():
    pool = FakePool(FakeCursor([]))
    trace_store = object()  # opaque -- only passed through to TraceStorePort's constructor
    ports, leases = repo.build_pg_ports(pool, trace_store, zones_by_bank={"bank-1": "zone-a"})  # type: ignore[arg-type]
    assert ports.zones_by_bank == {"bank-1": "zone-a"}
    assert isinstance(leases, repo.InMemoryLeaseStatePort)
