"""`opengrid.engine.gateways.EnergySufficiencyGateway`: the continuous per-obligation ENERGY check's
engine-side hook (K1, build brief item 3). No Postgres -- a minimal fake pool/cursor stands in for the
one query this gateway issues, and `opengrid.fleet`/`opengrid.health.queries` are monkeypatched at the
module level the gateway imports them into (matching how the rest of this test suite fakes `fleet`)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from opengrid.engine import gateways as gw
from opengrid.trace.store import TraceStore

NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)


@dataclass
class _FakeCursor:
    rows: list[tuple]
    executed: list[tuple] = field(default_factory=list)

    async def execute(self, sql=None, params=None) -> None:
        self.executed.append((sql, params))

    async def fetchall(self) -> list[tuple]:
        return self.rows

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@dataclass
class _FakeConn:
    cursor_obj: _FakeCursor
    committed: bool = False

    def cursor(self):
        return self.cursor_obj

    async def commit(self) -> None:
        self.committed = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@dataclass
class _FakePool:
    rows: list[tuple]
    conns: list[_FakeConn] = field(default_factory=list)

    def connection(self):
        conn = _FakeConn(_FakeCursor(self.rows))
        self.conns.append(conn)
        return conn


@dataclass
class _FakeHubCap:
    hub_id: str
    bank_id: str
    free_discharge_kw: float
    health: str = "online"
    soc_kwh: float | None = None
    reserve_kwh: float | None = 7.84
    eta_d: float = 0.9487


@dataclass
class _FakeTraceBackend:
    rows: list[tuple] = field(default_factory=list)

    async def last_head(self, stream_id: str) -> tuple[int, str | None]:
        return -1, None

    async def insert_trace_row(self, **kwargs) -> None:
        self.rows.append((kwargs["stream_id"], kwargs["decision_type"], kwargs["payload"]))

    async def exists_preimage(self, decision_ref) -> bool:
        return False

    async def fetch_range(self, *args, **kwargs):
        return []

    async def stream_ids(self):
        return []

    async def insert_checkpoint(self, **kwargs):
        return None

    async def prune_before(self, *args, **kwargs):
        return 0

    async def retention_days_for(self, event_class):
        return 400


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    """Records `contracts.set_obligation_at_risk` calls (the obligation-row flag, lead finding 4)."""
    calls: list[tuple[str, bool]] = []

    async def _fake_set(obligation_id, at_risk, *, reason_code, payload=None):
        calls.append((str(obligation_id), at_risk))

    monkeypatch.setattr(gw.contracts, "set_obligation_at_risk", _fake_set)
    return calls


@pytest.fixture(autouse=True)
def _patch_alert_raising(monkeypatch):
    raised: list = []

    async def _fake_raise_alert(pool, finding, *, opened_at):
        raised.append(finding)
        return 1

    monkeypatch.setattr(gw, "raise_alert", _fake_raise_alert)
    return raised


async def test_ample_energy_obligation_is_not_flagged(monkeypatch, _patch_alert_raising):
    obligation_id = uuid4()
    rows = [(obligation_id, "bank-01", 5.0, NOW + timedelta(hours=2), uuid4())]
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=39.2)]
    )
    trace = TraceStore(_FakeTraceBackend())
    pool = _FakePool(rows)
    gateway = gw.EnergySufficiencyGateway(pool, trace)

    results = await gateway.run(NOW)

    assert len(results) == 1
    assert not results[0].at_risk
    assert _patch_alert_raising == []

    # merge task item 5: EVERY result (not just AT_RISK ones) is persisted to
    # og.obligation_energy_status so the API/UI can show a live energy_margin_kwh even when it's fine.
    status_writes = [
        c
        for c in pool.conns
        if c.cursor_obj.executed and "og.obligation_energy_status" in (c.cursor_obj.executed[0][0] or "")
    ]
    assert len(status_writes) == 1
    _sql, params = status_writes[0].cursor_obj.executed[0]
    assert params["obligation_id"] == str(obligation_id)
    assert params["at_risk"] is False
    assert status_writes[0].committed is True


async def test_missing_soc_flags_at_risk_and_raises_alert(monkeypatch, _patch_alert_raising):
    obligation_id = uuid4()
    customer_id = uuid4()
    rows = [(obligation_id, "bank-01", 5.0, NOW + timedelta(hours=2), customer_id)]
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=None)]
    )
    trace_backend = _FakeTraceBackend()
    trace = TraceStore(trace_backend)
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), trace)

    results = await gateway.run(NOW)

    assert len(results) == 1
    assert results[0].at_risk
    assert results[0].available_kwh == 0.0
    assert len(_patch_alert_raising) == 1
    assert _patch_alert_raising[0].rule == "ALR-ENERGY-SHORTFALL-RISK"
    assert _patch_alert_raising[0].detail["obligation_id"] == str(obligation_id)
    # ALSO traced (K10-adjacent: record via trace since no AT_RISK lifecycle transition exists).
    assert len(trace_backend.rows) == 1
    assert trace_backend.rows[0][1] == "ALERT"


async def test_two_obligations_sharing_a_bank_the_other_ones_energy_is_excluded_k2(
    monkeypatch, _patch_alert_raising
):
    """K2: a second obligation's own committed draw on the SAME bank must reduce this obligation's
    available energy -- it is not double-counted as available to both."""
    obl_a, obl_b = uuid4(), uuid4()
    # One hub with ~11.6 kWh above reserve (soc=20, reserve=7.84, eta_d~0.9487). Two obligations each
    # drawing 5 kW for 2h (10 kWh required each) on the SAME single hub -- together they need 20 kWh,
    # more than the hub has, so at least one must be AT_RISK.
    rows = [
        (obl_a, "bank-01", 10.0, NOW + timedelta(hours=2), uuid4()),
        (obl_b, "bank-01", 10.0, NOW + timedelta(hours=2), uuid4()),
    ]
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=20.0)]
    )
    trace = TraceStore(_FakeTraceBackend())
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), trace)

    results = await gateway.run(NOW)

    assert len(results) == 2
    assert any(r.at_risk for r in results)


async def test_at_risk_alert_is_raised_on_entry_only(monkeypatch, _patch_alert_raising):
    """Regression (live 2026-09-26): the gateway raised a new ALR-ENERGY-SHORTFALL-RISK alert and trace
    row for every at-risk obligation on EVERY 2 s cycle (6,748 alerts in 6 minutes), stalling the
    engine tick. One alert per entry into AT_RISK; a recovery re-arms it."""
    obligation_id = uuid4()
    rows = [(obligation_id, "bank-01", 5.0, NOW + timedelta(hours=2), uuid4())]
    soc = {"value": None}
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=soc["value"])]
    )
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), TraceStore(_FakeTraceBackend()))

    await gateway.run(NOW)
    await gateway.run(NOW + timedelta(seconds=2))
    assert len(_patch_alert_raising) == 1

    soc["value"] = 39.2  # recovered
    await gateway.run(NOW + timedelta(seconds=4))
    soc["value"] = None  # at risk again -> a new entry
    await gateway.run(NOW + timedelta(seconds=6))
    assert len(_patch_alert_raising) == 2


async def test_query_is_scoped_to_remaining_energy_within_the_lookahead(monkeypatch, _patch_alert_raising):
    """The SQL sums only not-yet-elapsed reservation energy of obligations delivering now or within the
    look-ahead; the third column is kWh (not kW), so the gateway divides by the remaining hours."""
    obligation_id = uuid4()
    rows = [(obligation_id, "bank-01", 11.0, NOW + timedelta(hours=1), uuid4())]
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=39.2)]
    )
    pool = _FakePool(rows)
    gateway = gw.EnergySufficiencyGateway(pool, TraceStore(_FakeTraceBackend()), lookahead_s=600.0)

    (result,) = await gateway.run(NOW)

    sql, params = pool.conns[0].cursor_obj.executed[0]
    assert "GROUP BY" in sql
    assert params == {"now": NOW, "lookahead_end": NOW + timedelta(seconds=600)}
    assert result.required_kwh == pytest.approx(11.0)


async def test_at_risk_flag_is_set_on_entry_and_cleared_on_recovery(monkeypatch, _flags):
    """Lead finding 4: the check traced and alerted but never set og.obligation.at_risk."""
    obligation_id = uuid4()
    rows = [(obligation_id, "bank-01", 5.0, NOW + timedelta(hours=2), uuid4())]
    soc = {"value": None}
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=soc["value"])]
    )
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), TraceStore(_FakeTraceBackend()))

    await gateway.run(NOW)
    await gateway.run(NOW + timedelta(seconds=2))
    soc["value"] = 39.2
    await gateway.run(NOW + timedelta(seconds=4))

    assert _flags == [(str(obligation_id), True), (str(obligation_id), False)]
