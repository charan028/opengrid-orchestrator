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
    e_kwh: float | None = None


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
def _alert_clears(monkeypatch):
    """Records `clear_open_alerts` calls as (rule, detail -> matches) predicates to probe."""
    calls: list = []

    async def _fake_clear(pool, rule, matches):
        calls.append((rule, matches))
        return 0

    monkeypatch.setattr(gw, "clear_open_alerts", _fake_clear)
    return calls


@pytest.fixture(autouse=True)
def _open_alerts(monkeypatch):
    """Details of ALR-ENERGY-SHORTFALL-RISK alerts 'left open by a previous engine' (default none)."""
    details: list[dict] = []

    async def _fake_open(pool, rule):
        return list(details)

    monkeypatch.setattr(gw, "open_alert_details", _fake_open)
    return details


async def test_an_alert_left_open_is_adopted_not_raised_again(
    monkeypatch, _open_alerts, _alert_clears, _patch_alert_raising
):
    """Review #14: after a restart the first run cleared and re-raised a still-valid alert, losing its
    ack state. An obligation whose alert is still open and still at risk is adopted: no new alert, and
    the open one is not cleared."""
    obligation_id = uuid4()
    _open_alerts.append({"obligation_id": str(obligation_id)})
    rows = [(obligation_id, "bank-01", 5.0, NOW + timedelta(hours=2), uuid4())]
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=None)]
    )
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), TraceStore(_FakeTraceBackend()))

    results = await gateway.run(NOW)

    assert results[0].at_risk
    assert _patch_alert_raising == []  # not raised a second time
    ((_rule, matches),) = _alert_clears
    assert not matches({"obligation_id": str(obligation_id)})  # still open


async def test_the_hook_clears_its_own_energy_alerts(monkeypatch, _alert_clears):
    """Health only auto-clears its own rules, so ALR-ENERGY-SHORTFALL-RISK is cleared here: on the first
    run (alerts a previous engine left open) and whenever an obligation leaves AT_RISK -- never for an
    obligation that is still at risk."""
    at_risk_id, fine_id = uuid4(), uuid4()
    soc = {"value": None}
    rows = [(at_risk_id, "bank-01", 5.0, NOW + timedelta(hours=2), uuid4())]
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=soc["value"])]
    )
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), TraceStore(_FakeTraceBackend()))

    await gateway.run(NOW)  # first run: at risk (no SoC), sweep leaves it alone
    ((rule, matches),) = _alert_clears
    assert rule == "ALR-ENERGY-SHORTFALL-RISK"
    assert not matches({"obligation_id": str(at_risk_id)})
    assert matches({"obligation_id": str(fine_id)})

    await gateway.run(NOW)  # still at risk: no further clearing
    assert len(_alert_clears) == 1

    soc["value"] = 39.2
    await gateway.run(NOW)  # recovered: its alert is cleared
    assert len(_alert_clears) == 2
    assert _alert_clears[1][1]({"obligation_id": str(at_risk_id)})


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
        if any("og.obligation_energy_status" in (sql or "") for sql, _ in c.cursor_obj.executed)
    ]
    assert len(status_writes) == 1
    _sql, params = next(e for e in status_writes[0].cursor_obj.executed if e[1])
    assert params["obligation_id"] == str(obligation_id)
    assert params["at_risk"] is False
    assert status_writes[0].committed is True


async def test_energy_statuses_are_one_asynchronous_commit_per_cycle(monkeypatch):
    """A11 (live 2026-09-26 06:04, host disk stall): every obligation's status row was its own synchronous
    commit, one after another inside the dispatch tick -- 22 obligations = 22 fsyncs, and the tick's
    energy_check phase reached 5.7 s. The display-only status rows now go out in one transaction with an
    asynchronous commit (they are recomputed every 2 s); AT_RISK writes stay synchronous."""
    rows = [
        (uuid4(), "bank-01", 5.0, NOW + timedelta(hours=2), uuid4()),
        (uuid4(), "bank-01", 5.0, NOW + timedelta(hours=2), uuid4()),
    ]
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=39.2)]
    )
    pool = _FakePool(rows)
    await gw.EnergySufficiencyGateway(pool, TraceStore(_FakeTraceBackend())).run(NOW)

    status_conns = [
        c
        for c in pool.conns
        if any("og.obligation_energy_status" in (sql or "") for sql, _ in c.cursor_obj.executed)
    ]
    assert len(status_conns) == 1
    executed = [sql for sql, _ in status_conns[0].cursor_obj.executed]
    assert "synchronous_commit" in executed[0]
    assert sum("og.obligation_energy_status" in s for s in executed) == 2
    assert status_conns[0].committed is True


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


@pytest.mark.parametrize(("duration_minutes", "at_risk"), [(240, True), (60, False)])
async def test_an_as_award_must_hold_energy_for_its_full_deployment(
    monkeypatch, _patch_alert_raising, duration_minutes, at_risk
):
    """Frank #6 / NPRR1282 energy hold: an ERCOT_AS award needs committed_kw x duration of deliverable
    energy (kW x duration / eta_d stored) above the reserve floor, whatever is left of its window. 5 kW
    Non-Spin (4 h) needs 20 kWh; one hub at 20 kWh SoC (7.84 reserve) can deliver ~11.5 kWh -> AT_RISK. The
    same award as ECRS (1 h, 5 kWh) is covered."""
    obligation_id = uuid4()
    # (obligation, bank, remaining-window kWh, draw end, customer, service, hold kW, duration, deployed)
    rows = [
        (
            obligation_id,
            "bank-01",
            1.25,
            NOW + timedelta(minutes=15),
            uuid4(),
            "ERCOT_AS",
            5.0,
            duration_minutes,
            False,
        )
    ]
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=20.0)]
    )
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), TraceStore(_FakeTraceBackend()))

    (result,) = await gateway.run(NOW)

    assert result.required_kwh == pytest.approx(5.0 * duration_minutes / 60)
    assert result.at_risk is at_risk
    assert gateway.as_hold_ids == {str(obligation_id)}  # undeployed: AT_RISK only, never escalated


async def test_a_deployed_as_award_is_not_a_hold(monkeypatch, _patch_alert_raising):
    obligation_id = uuid4()
    rows = [
        (obligation_id, "bank-01", 1.25, NOW + timedelta(minutes=15), uuid4(), "ERCOT_AS", 5.0, 240, True)
    ]
    monkeypatch.setattr(
        gw.fleet, "hub_capabilities", lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=39.2)]
    )
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), TraceStore(_FakeTraceBackend()))
    await gateway.run(NOW)
    assert gateway.as_hold_ids == set()


async def test_the_as_energy_hold_includes_the_guardians_one_percent_floor(monkeypatch, _patch_alert_raising):
    """Lead (matches G-01-ENERGY): the hold keeps reserve + 1% of capacity + kW x duration / eta_d, else
    the last leases of a full deployment are vetoed ~2 minutes before its end."""
    obligation_id = uuid4()
    rows = [
        (obligation_id, "bank-01", 1.25, NOW + timedelta(minutes=15), uuid4(), "ERCOT_AS", 5.0, 60, False)
    ]
    monkeypatch.setattr(
        gw.fleet,
        "hub_capabilities",
        lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=39.2, e_kwh=39.2)],
    )
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), TraceStore(_FakeTraceBackend()))

    (result,) = await gateway.run(NOW)

    assert result.required_kwh == pytest.approx(5.0 + 0.01 * 39.2 * 0.9487)


async def test_a_utility_toll_holds_energy_for_its_90_minute_call(monkeypatch, _patch_alert_raising):
    """D-29: the toll (REGULATED_CAPACITY / TOLLING) is a capacity hold like ERCOT_AS; its energy hold uses
    the product rule's 90 min, and an uncalled toll short of it is AT_RISK, never escalated."""
    obligation_id = uuid4()
    rows = [
        (
            obligation_id,
            "bank-01",
            1.25,
            NOW + timedelta(minutes=15),
            uuid4(),
            "REGULATED_CAPACITY",
            10.0,
            90,
            False,
        )
    ]
    monkeypatch.setattr(
        gw.fleet,
        "hub_capabilities",
        lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=39.2, e_kwh=39.2)],
    )
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), TraceStore(_FakeTraceBackend()))
    (result,) = await gateway.run(NOW)
    assert result.required_kwh == pytest.approx(10.0 * 1.5 + 0.01 * 39.2 * 0.9487)
    assert gateway.as_hold_ids == {str(obligation_id)}


async def test_a_deployed_award_needs_only_the_rest_of_its_call(monkeypatch, _patch_alert_raising):
    """Review R3: while deployed, kW x (end_at - now) capped by the product duration -- not a fresh full
    duration, which escalated a correctly delivering award to SHORTFALL. Held: the full duration."""
    deployed_id, held_id = uuid4(), uuid4()
    rows = [
        (
            deployed_id,
            "bank-01",
            1.0,
            NOW + timedelta(minutes=15),
            uuid4(),
            "ERCOT_AS",
            10.0,
            240,
            True,
            NOW + timedelta(minutes=30),
        ),
        (held_id, "bank-02", 1.0, NOW + timedelta(minutes=15), uuid4(), "ERCOT_AS", 10.0, 240, False, None),
    ]
    monkeypatch.setattr(
        gw.fleet,
        "hub_capabilities",
        lambda bank_id: [_FakeHubCap("h1", bank_id, 20.0, soc_kwh=1000.0, e_kwh=1000.0)],
    )
    gateway = gw.EnergySufficiencyGateway(_FakePool(rows), TraceStore(_FakeTraceBackend()))
    results = {r.obligation_id: r for r in await gateway.run(NOW)}
    margin = 0.01 * 1000.0 * 0.9487
    assert results[str(deployed_id)].required_kwh == pytest.approx(10.0 * 0.5 + margin)
    assert results[str(held_id)].required_kwh == pytest.approx(10.0 * 4.0 + margin)
    assert gateway.as_hold_ids == {str(held_id)}


def test_as_energy_hold_caps_the_remaining_call_at_the_product_duration():
    kw, end = gw.as_energy_hold(NOW, 5.0, 60, NOW + timedelta(hours=3))
    assert (kw, end) == (5.0, NOW + timedelta(minutes=60))
    assert gw.as_energy_hold(NOW, 5.0, 60, NOW + timedelta(minutes=10))[1] == NOW + timedelta(minutes=10)
    assert gw.as_energy_hold(NOW, 5.0, 60)[1] == NOW + timedelta(minutes=60)
