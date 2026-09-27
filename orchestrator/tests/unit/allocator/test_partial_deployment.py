"""D-33 partial call (r3.4.4 HIGH, ECRS deployment 046a2ebb: 135/276 batches vetoed G-19): a deployed capacity
hold called for LESS than its commitment is granted its pro-rata share on every bank and carries
R-AS-PARTIAL-DEPLOYMENT on the grant and on every hub item, so the guardian's G-19 judges it against the
deployed share (its own og.as_deployment read), not as a lock dip."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid.allocator.cycle import cycle
from opengrid.allocator.models import (
    BankSnapshot,
    FleetState,
    HubSnapshot,
    LedgerView,
    ObligationCall,
    Schedule,
)
from opengrid.core import reasons
from opengrid.core.models.engine import Grant
from opengrid.engine import _distribute_hub_items
from opengrid.engine.gateways import called_kw_scale, is_partial_call

T = datetime(2026, 9, 27, 10, 9, 33, tzinfo=UTC)


def _hub(hub_id: str, bank_id: str) -> HubSnapshot:
    return HubSnapshot(
        hub_id=hub_id, bank_id=bank_id, free_discharge_kw=200.0, soc_kwh=1e6, reserve_kwh=0.0, e_kwh=1e6
    )


def _call(oid: str, bank_id: str, kw: float, hubs: tuple[str, ...], **extra: object) -> ObligationCall:
    return ObligationCall(
        obligation_id=oid,
        bank_id=bank_id,
        service_type="ERCOT_AS",
        tier="T1",
        committed_kw=kw,
        eligible_hub_ids=hubs,
        as_deployed=True,
        **extra,  # type: ignore[arg-type]
    )


def _run(calls: tuple[ObligationCall, ...]):
    hubs = tuple(_hub(f"{b}-h{i}", b) for b in ("bank-027", "bank-034") for i in range(2))
    banks = tuple(
        BankSnapshot(bank_id=b, capability_kw=1000.0, kva_rating=1000.0) for b in ("bank-027", "bank-034")
    )
    return cycle(T, FleetState(hubs=hubs, banks=banks), LedgerView(calls=calls), Schedule(prices=()), {}, ())


def test_the_prod_rows_flag_a_partial_call_on_both_banks_and_a_full_call_on_neither() -> None:
    partial, full = str(uuid4()), str(uuid4())
    rows = [
        # obligation, bank, amount, service, tier, value, state, as_deployed, duration, end, deploy_kw
        (partial, "bank-027", 250.707, "ERCOT_AS", "T1", 0, "DELIVERING", True, 60, None, 300.0),
        (partial, "bank-034", 249.293, "ERCOT_AS", "T1", 0, "DELIVERING", True, 60, None, 300.0),
        (full, "bank-040", 500.0, "ERCOT_AS", "T1", 0, "DELIVERING", True, 60, None, None),
    ]
    scale = called_kw_scale(rows)
    assert scale[partial] == pytest.approx(0.6)
    assert is_partial_call(True, partial, scale) is True
    assert is_partial_call(True, full, scale) is False
    assert is_partial_call(False, partial, scale) is False  # not called now: a hold, not a partial call


def test_a_partial_call_grants_the_pro_rata_share_with_its_own_reason_on_every_bank() -> None:
    oid = "eeef3bd0"
    result = _run(
        (
            _call(oid, "bank-027", 250.707 * 0.6, ("bank-027-h0", "bank-027-h1"), partial_call=True),
            _call(oid, "bank-034", 249.293 * 0.6, ("bank-034-h0", "bank-034-h1"), partial_call=True),
        )
    )
    grants = {g.bank_id: g for g in result.grants if g.obligation_id == oid}
    assert grants["bank-027"].granted_kw == pytest.approx(150.4242)
    assert grants["bank-034"].granted_kw == pytest.approx(149.5758)
    assert {g.reason_code for g in grants.values()} == {reasons.R_AS_PARTIAL_DEPLOYMENT}


def test_a_full_call_keeps_r_grant_committed() -> None:
    result = _run((_call("full", "bank-027", 100.0, ("bank-027-h0",)),))
    (grant,) = [g for g in result.grants if g.obligation_id == "full"]
    assert grant.reason_code == reasons.R_GRANT_COMMITTED


def test_a_shortfall_below_the_deployed_share_still_carries_its_k13_reason() -> None:
    """The partial-call reason covers only "the call asked for less"; a hub loss below that share is a real
    shortfall and keeps its override reason (G-19 corroborates it separately)."""
    result = _run((_call("p", "bank-027", 900.0, ("bank-027-h0",), partial_call=True),))  # 200 kW hub
    (grant,) = [g for g in result.grants if g.obligation_id == "p"]
    assert grant.reason_code != reasons.R_AS_PARTIAL_DEPLOYMENT
    assert grant.reason_code in reasons.COMMIT_LOCK_OVERRIDE_REASONS | set(reasons.LOCK_REASON_BY_SHORTFALL)


class _Fleet:
    def __init__(self, hubs: list[HubSnapshot]) -> None:
        self._hubs = hubs

    def hub_capabilities(self, bank_id: str) -> list[object]:
        return [
            type(
                "Cap",
                (),
                {"hub_id": h.hub_id, "bank_id": bank_id, "free_discharge_kw": 200.0, "health": "online"},
            )()
            for h in self._hubs
        ]


def test_every_hub_item_of_a_partial_call_carries_the_reason_and_its_share() -> None:
    oid = uuid4()
    grant = Grant(
        grant_id=uuid4(),
        cycle_id="c",
        obligation_id=oid,
        bank_id="bank-034",
        granted_kw=Decimal("149.576"),
        ledger_version=1,
        reason_code=reasons.R_AS_PARTIAL_DEPLOYMENT,
    )
    fleet = _Fleet([_hub(f"bank-034-h{i}", "bank-034") for i in range(4)])
    items = _distribute_hub_items("bank-034", [grant], fleet_module=fleet, cycle_interval_s=None)
    assert {i["reason_code"] for i in items} == {reasons.R_AS_PARTIAL_DEPLOYMENT}
    assert sum(float(str(i["obligation_granted_kw"])) for i in items) == pytest.approx(149.576)
