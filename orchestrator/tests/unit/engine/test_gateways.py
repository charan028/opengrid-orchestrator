"""opengrid.engine.gateways: the allocator's real FleetGateway/LedgerGateway/ScadaGateway/
ScheduleGateway adapters (dispatch-live pass). Fleet-backed pieces reuse `opengrid.fleet`'s own
in-memory twin (configured with a fake backend, no DB); DB-backed pieces use a minimal fake psycopg
pool/cursor, mirroring `tests/unit/guardian/test_repo.py`'s pattern."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid import fleet, ledger
from opengrid.allocator.models import SubstitutionEvent
from opengrid.core.models.platform import Bank, Hub
from opengrid.engine import gateways as gw
from opengrid.engine.gateways import (
    EngineFleetGateway,
    EngineLedgerGateway,
    EngineScadaGateway,
    EngineScheduleGateway,
    FleetCapabilityProvider,
)
from opengrid.ledger import GrantRecord
from opengrid.platform.config import Config

pytestmark = pytest.mark.asyncio


class FakeFleetBackend:
    def __init__(self, hubs, banks):
        self._hubs = hubs
        self._banks = banks

    async def load_hubs(self):
        return self._hubs

    async def load_banks(self):
        return self._banks

    async def load_hub_states(self):
        return []

    async def upsert_hub_states(self, states):
        pass

    async def copy_telemetry(self, rows):
        pass

    async def record_scada_observations(self, signals):
        pass

    async def insert_acks(self, acks):
        pass


class FakeCursor:
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


@pytest.fixture(autouse=True)
def _reset_fleet():
    fleet.configure(FakeFleetBackend([], []), Config({}))
    yield
    fleet.configure(FakeFleetBackend([], []), Config({}))


async def _seed_fleet() -> None:
    hubs = [
        Hub(hub_id="hub-00000", bank_id="bank-000", zone="LZ_NORTH", e_kwh=13.5, r_kwh=2.7, p_kw=5.0),
        Hub(hub_id="hub-00001", bank_id="bank-000", zone="LZ_NORTH", e_kwh=13.5, r_kwh=2.7, p_kw=5.0),
    ]
    banks = [Bank(bank_id="bank-000", zone="LZ_NORTH", kva_rating=75.0, reserve_kva=0.0)]
    fleet.configure(FakeFleetBackend(hubs, banks), Config({}))
    await fleet.load_topology()
    now = datetime.now(UTC)
    await fleet.ingest_telemetry(
        {
            "hub_id": "hub-00000",
            "bank_id": "bank-000",
            "zone": "LZ_NORTH",
            "ts": now.isoformat(),
            "soc_kwh": 10.0,
            "p_kw": 0.0,
            "health": "online",
            "seq": 1,
            "epoch": 1,
        }
    )


async def test_fleet_gateway_bank_ids_and_fleet_state():
    await _seed_fleet()
    gw = EngineFleetGateway()

    assert await gw.bank_ids() == ["bank-000"]

    state = await gw.fleet_state(["bank-000"], datetime.now(UTC))
    assert len(state.banks) == 1
    assert state.banks[0].bank_id == "bank-000"
    assert state.banks[0].zone == "LZ_NORTH"
    hub_ids = {h.hub_id for h in state.hubs}
    assert hub_ids == {"hub-00000", "hub-00001"}
    online = next(h for h in state.hubs if h.hub_id == "hub-00000")
    assert online.health == "OK"
    # Nameplate kW for every hub, online or not (K13 L0/L1 shortfall attribution).
    assert {h.hub_id: h.rated_kw for h in state.hubs} == {"hub-00000": 5.0, "hub-00001": 5.0}


async def test_fleet_gateway_skips_unknown_bank_id():
    await _seed_fleet()
    gw = EngineFleetGateway()
    state = await gw.fleet_state(["bank-999"], datetime.now(UTC))
    assert state.banks == ()
    assert state.hubs == ()


async def test_scada_gateway_reads_apparent_power_only():
    await _seed_fleet()
    now = datetime.now(UTC)
    await fleet.ingest_scada_signal(
        {
            "bank_id": "bank-000",
            "signal": "APPARENT_POWER_KVA",
            "value": 42.0,
            "unit": "kVA",
            "quality": "good",
            "ts": now.isoformat(),
        }
    )
    gw = EngineScadaGateway()
    samples = await gw.samples(["bank-000"])
    assert samples["bank-000"].apparent_power_kva == 42.0


async def test_schedule_gateway_instructions_from_fleet_twin():
    await _seed_fleet()
    await fleet.ingest_utility_instruction(
        {
            "instruction_id": str(uuid4()),
            "bank_id": "bank-000",
            "kind": "LIMIT",
            "limit_kw": 10.0,
            "issued_at": datetime.now(UTC).isoformat(),
            "issued_by": "utility-x",
        }
    )
    gw = EngineScheduleGateway(pool=FakePool(FakeCursor([])))
    instructions = await gw.instructions(["bank-000"])
    assert len(instructions) == 1
    assert instructions[0].kind == "LIMIT"
    assert instructions[0].limit_kw == 10.0


async def test_schedule_gateway_prices_each_bank_at_its_own_zone():
    """Architect finding (b): one price row (whichever zone was written last) was applied to every bank.
    Each bank now gets its own load zone's SPP; a bank with no zone price gets the zones' mean."""
    await _seed_fleet()  # bank-000 is LZ_NORTH; bank-001 is unknown to the twin
    cursor = FakeCursor([[("LZ_NORTH", 23.45), ("LZ_WEST", 40.0), ("LZ_SOUTH", 30.0)], []])
    gw = EngineScheduleGateway(pool=FakePool(cursor))
    schedule = await gw.schedule(["bank-000", "bank-001"])
    by_bank = {p.bank_id: p.price_usd_per_mwh for p in schedule.prices}
    assert by_bank["bank-000"] == 23.45
    assert by_bank["bank-001"] == pytest.approx((23.45 + 40.0 + 30.0) / 3)
    assert schedule.conservative_bank_ids == frozenset()


async def test_schedule_gateway_defaults_to_zero_price_when_no_data():
    gw = EngineScheduleGateway(pool=FakePool(FakeCursor([[], []])))
    schedule = await gw.schedule(["bank-000"])
    assert schedule.prices[0].price_usd_per_mwh == 0.0


@pytest.mark.parametrize("scope", [("BANK", "bank-000"), ("ZONE", "LZ_NORTH")])
async def test_schedule_gateway_marks_conservative_scopes(scope):
    """K7 escalation (og.scope_posture): a CONSERVATIVE bank, or any bank in a CONSERVATIVE zone, gets no
    new uncommitted dispatch; the allocator reads it from the schedule."""
    await _seed_fleet()
    cursor = FakeCursor([[("LZ_NORTH", 20.0)], [scope]])
    schedule = await EngineScheduleGateway(pool=FakePool(cursor)).schedule(["bank-000"])
    assert schedule.conservative_bank_ids == frozenset({"bank-000"})


async def test_schedule_gateway_survives_a_missing_scope_posture_table():
    class _Failing(FakeCursor):
        async def execute(self, sql, params=None):
            if "scope_posture" in sql:
                raise RuntimeError('relation "og.scope_posture" does not exist')
            await super().execute(sql, params)

    await _seed_fleet()
    schedule = await EngineScheduleGateway(pool=FakePool(_Failing([[("LZ_NORTH", 20.0)]]))).schedule(
        ["bank-000"]
    )
    assert schedule.conservative_bank_ids == frozenset()
    assert schedule.prices[0].price_usd_per_mwh == 20.0


async def test_ledger_gateway_ledger_view_builds_obligation_calls():
    await _seed_fleet()
    obligation_id = uuid4()
    call_row = (obligation_id, "bank-000", Decimal("3.5"), "HOME", "L1", None, "SHORTFALL", False)
    prior_rows: list = []
    cursor = FakeCursor([[call_row], prior_rows])
    gw = EngineLedgerGateway(pool=FakePool(cursor))

    view = await gw.ledger_view(["bank-000"], datetime.now(UTC))

    assert len(view.calls) == 1
    call = view.calls[0]
    assert call.obligation_id == str(obligation_id)
    assert call.bank_id == "bank-000"
    assert call.committed_kw == 3.5
    assert call.eligible_hub_ids == ("hub-00000",)  # only the online hub
    assert call.prior_granted_kw is None
    assert call.value_per_mwh == 0.0
    assert call.in_shortfall  # still dispatched best-effort (owner decision 2026-09-26)
    assert not call.as_deployed


@pytest.mark.parametrize("deployed", [False, True])
async def test_ledger_gateway_carries_the_as_deployment_flag(deployed):
    """ERCOT_AS is a capacity hold: the call says whether ERCOT has deployed it right now."""
    await _seed_fleet()
    row = (uuid4(), "bank-000", Decimal("100"), "ERCOT_AS", "T2", Decimal("5.37"), "DELIVERING", deployed)
    view = await EngineLedgerGateway(pool=FakePool(FakeCursor([[row], []]))).ledger_view(
        ["bank-000"], datetime.now(UTC)
    )
    (call,) = view.calls
    assert call.as_deployed is deployed
    assert call.is_as_hold is (not deployed)


async def test_ledger_gateway_persist_grants_and_version(monkeypatch: pytest.MonkeyPatch):
    from opengrid.allocator.models import ProposedGrant

    recorded: list[list[GrantRecord]] = []

    class FakeGrantBackend:
        async def insert_grants(self, records):
            recorded.append(records)

    class FakeLedgerBackend:
        async def next_version(self):
            return 7

        async def current_version(self):
            return 0

        async def active_reservations(self, bank_id, interval_start):
            return []

        async def reservations_for_obligation(self, obligation_id):
            return []

        async def get_reservation(self, reservation_id):
            return None

        async def insert_reservations(self, records):
            pass

        async def mark_released(self, reservation_id, *, reason, version):
            pass

    class FakeCapability:
        async def capability_kw(self, bank_id, interval_start):
            return Decimal(100)

    ledger.configure(
        ledger.ReservationLedger(FakeLedgerBackend(), FakeCapability(), grant_backend=FakeGrantBackend())
    )
    gw = EngineLedgerGateway(pool=FakePool(FakeCursor([])))
    grant = ProposedGrant(bank_id="bank-000", granted_kw=12.345, obligation_id=None, is_headroom=True)

    await gw.persist_grants("cycle-1", [grant])

    assert len(recorded) == 1
    assert recorded[0][0].bank_id == "bank-000"
    assert recorded[0][0].granted_kw == Decimal("12.345")
    assert recorded[0][0].is_headroom is True


async def test_fleet_capability_provider_reads_max_discharge_kw():
    await _seed_fleet()
    provider = FleetCapabilityProvider()
    kw = await provider.capability_kw("bank-000", datetime.now(UTC))
    assert isinstance(kw, Decimal)


async def test_ledger_gateway_records_substitutions_as_trace_events() -> None:
    """S5.3: hub swaps are recorded as SUBSTITUTION trace events with reason R-SUBSTITUTION (it raised
    NotImplementedError before, so no swap was ever recorded)."""
    appended: list[tuple] = []

    class _Trace:
        async def append(self, stream_id, decision_type, event_class, payload, reason_codes=None):
            appended.append((stream_id, decision_type, payload, reason_codes))

    gateway = gw.EngineLedgerGateway(pool=None, trace=_Trace())  # type: ignore[arg-type]
    await gateway.record_substitution("o1", "h1", "h2", "R-SUBSTITUTION")
    await gateway.record_substitution_events(
        "c1", [SubstitutionEvent(obligation_id="o2", bank_id="b1", from_hub_ids=("h3",), to_hub_ids=("h4",))]
    )

    assert [(a[0], a[1], a[3]) for a in appended] == [
        ("substitution-o1", "SUBSTITUTION", ["R-SUBSTITUTION"]),
        ("substitution-o2", "SUBSTITUTION", ["R-SUBSTITUTION"]),
    ]
    assert appended[1][2]["bank_id"] == "b1"


async def test_ledger_gateway_traces_a_k13_shortfall_once_per_episode_and_interval() -> None:
    """K13 invariant finding (live 2026-09-26): two dips below a committed 150 kW (88 kW at 00:49, 11 kW
    at 01:09, right after engine restarts while the twin's hubs were not yet fresh) carried no traced
    exception -- the allocator reported R-COMMIT-LOCK-INFEASIBLE, but only in memory. A K13-exception
    shortfall is now traced when it starts, and again in each new 15-min interval it spans."""
    from opengrid.allocator.models import ShortfallReport

    appended: list[tuple] = []

    class _Trace:
        async def append(self, stream_id, decision_type, event_class, payload, reason_codes=None):
            appended.append((stream_id, decision_type, event_class, payload, reason_codes))

    gateway = gw.EngineLedgerGateway(pool=None, trace=_Trace())  # type: ignore[arg-type]
    infeasible = ShortfallReport(
        obligation_id="o1", bank_id="b1", shortfall_kw=62.0, reason_code="R-COMMIT-LOCK-INFEASIBLE"
    )
    t0 = datetime(2026, 9, 26, 5, 49, 0, tzinfo=UTC)

    await gateway.record_shortfalls("c1", [infeasible], now=t0)
    await gateway.record_shortfalls("c2", [infeasible], now=t0 + timedelta(seconds=2))  # same episode
    await gateway.record_shortfalls("c3", [], now=t0 + timedelta(seconds=4))  # cleared
    await gateway.record_shortfalls("c4", [infeasible], now=t0 + timedelta(seconds=6))  # new episode
    await gateway.record_shortfalls("c5", [infeasible], now=datetime(2026, 9, 26, 6, 0, 1, tzinfo=UTC))

    assert [(a[0], a[1], a[2], a[4]) for a in appended] == [
        ("shortfall-o1", "SHORTFALL", "ALLOCATOR_SHORTFALL", ["R-COMMIT-LOCK-INFEASIBLE"]),
    ] * 3
    assert appended[0][3]["obligation_id"] == "o1"
    assert appended[0][3]["shortfall_kw"] == 62.0
    assert gateway.last_shortfalls == [("o1", "R-COMMIT-LOCK-INFEASIBLE")]


async def test_a_shortfall_without_a_k13_reason_is_not_traced() -> None:
    from opengrid.allocator.models import ShortfallReport

    appended: list[tuple] = []

    class _Trace:
        async def append(self, *args, **kwargs):
            appended.append(args)

    gateway = gw.EngineLedgerGateway(pool=None, trace=_Trace())  # type: ignore[arg-type]
    report = ShortfallReport(
        obligation_id="o1", bank_id="b1", shortfall_kw=1.0, reason_code="R-GRANT-HEADROOM"
    )
    await gateway.record_shortfalls("c1", [report], now=datetime(2026, 9, 26, 5, 0, tzinfo=UTC))
    assert appended == []
