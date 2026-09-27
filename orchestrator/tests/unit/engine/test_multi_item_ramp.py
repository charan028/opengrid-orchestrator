"""r3.4.3 MEDIUM: a single-hub utility-scale bank with two items in one batch -- a 0 kW hold item
(R-GRANT-AS-HOLD / closed-loop / territory) plus a discharge item. The hub executes their SUM
(`guardian.checks.hub_setpoints`); stepping each item from the same anchor asked for ~2x the G-04 bound
(probe r343_multi_item.py: 56/60 cycles vetoed, stuck at -400 kW). The engine now ramps the per-hub net once
(`engine.ramp_net_per_hub`) and records the sum as the anchor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opengrid import engine
from opengrid.core.models.engine import Grant
from opengrid.core.physics import HubParams, hub_ramp_kw_per_s
from opengrid.core.reasons import R_GRANT_AS_HOLD
from opengrid.engine.ramp_anchor import RampAnchors
from opengrid.guardian import checks
from opengrid.guardian.ports import ProposedItem

CYCLE_S = 2.0
T0 = datetime(2026, 9, 27, 4, 0, tzinfo=UTC)
PARAMS = HubParams(e_kwh=40_000.0, r_kwh=8_000.0, p_kw=20_000.0, utility_scale=True)
RAMP = hub_ramp_kw_per_s(PARAMS)


@dataclass
class _Set:
    hub_id: str = "sub-1"
    bank_id: str = "bank-sub"
    free_discharge_kw: float = 20_000.0
    health: str = "online"
    p_kw: float = 0.0
    last_seen_at: datetime | None = None
    ramp_kw_per_s: float = RAMP
    utility_scale: bool = True


class _Fleet:
    def __init__(self, hub: _Set) -> None:
        self.hub = hub

    def hub_capabilities(self, bank_id: str) -> list[_Set]:
        return [self.hub]


def _grant(kw: str, *, reason: str | None = None) -> Grant:
    return Grant(
        grant_id=uuid4(),
        cycle_id="c",
        obligation_id=uuid4(),
        bank_id="bank-sub",
        granted_kw=Decimal(kw),
        ledger_version=1,
        reason_code=reason,
    )


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> RampAnchors:
    anchors = RampAnchors()
    monkeypatch.setattr(engine, "_ramp_anchors", anchors)
    return anchors


def test_a_hold_item_plus_a_discharge_item_step_the_hub_net_once_and_converge(_fresh: RampAnchors) -> None:
    fleet = _Fleet(_Set())
    grants = [_grant("0", reason=R_GRANT_AS_HOLD), _grant("5000")]
    signed: tuple[float, datetime, datetime] | None = None
    vetoes = 0
    net = 0.0
    for k in range(60):
        now = T0 + timedelta(seconds=CYCLE_S * k)
        items = engine._distribute_hub_items(
            "bank-sub", grants, fleet_module=fleet, cycle_interval_s=CYCLE_S, now=now
        )
        assert len(items) == 2
        hold = next(i for i in items if i["reason_code"] == R_GRANT_AS_HOLD)
        assert hold["p_kw_setpoint"] == 0.0  # carries the reason only; never duplicates the anchor
        batch = uuid4()
        _fresh.record_proposal(batch, items, now + timedelta(seconds=30))
        (hub_net,) = checks.hub_setpoints(
            [
                ProposedItem(str(i["hub_id"]), float(str(i["p_kw_setpoint"])), str(i["reason_code"]))
                for i in items
            ]
        )
        anchor = checks.g04_anchor_kw(
            prev_telemetry_kw=0.0,  # telemetry stays stale at 0 the whole time
            telemetry_ts=None,
            last_signed_kw=signed[0] if signed else None,
            last_signed_at=signed[1] if signed else None,
            lease_expires_at=signed[2] if signed else None,
            now=now,
            utility_scale=True,
            cycle_interval_s=CYCLE_S,
        )
        ok = checks.check_g04_hub_ramp(hub_net, anchor.kw, CYCLE_S, RAMP).ok
        if ok:
            signed = (hub_net.p_kw_setpoint, now, now + timedelta(seconds=30))
            net = hub_net.p_kw_setpoint
        else:
            vetoes += 1
        _fresh.apply_verdicts({batch: "PASS" if ok else "VETOED"}, now + timedelta(seconds=CYCLE_S))
    assert vetoes == 0
    expected = min(5000.0, 60 * RAMP * engine.RAMP_SAFETY_FACTOR * CYCLE_S)
    assert net == pytest.approx(-expected)  # one bound per cycle, not stuck at -400 kW


def test_items_netting_to_zero_put_the_single_step_on_the_first_item() -> None:
    hub = _Set(p_kw=-1000.0, utility_scale=False, ramp_kw_per_s=50.0)
    items: list[dict[str, object]] = [
        {"hub_id": "sub-1", "p_kw_setpoint": 0.0, "reason_code": R_GRANT_AS_HOLD},
        {"hub_id": "sub-1", "p_kw_setpoint": 0.0, "reason_code": "R-GRANT-CLOSED-LOOP"},
    ]
    out = engine.ramp_net_per_hub(items, [hub], CYCLE_S)
    step = 50.0 * CYCLE_S * engine.RAMP_SAFETY_FACTOR
    assert out[0]["p_kw_setpoint"] == pytest.approx(-1000.0 + step)
    assert out[1]["p_kw_setpoint"] == 0.0


def test_two_discharge_items_share_one_step_in_proportion() -> None:
    hub = _Set(p_kw=0.0, utility_scale=False, ramp_kw_per_s=50.0)
    items: list[dict[str, object]] = [
        {"hub_id": "sub-1", "p_kw_setpoint": -3000.0},
        {"hub_id": "sub-1", "p_kw_setpoint": -1000.0},
    ]
    out = engine.ramp_net_per_hub(items, [hub], CYCLE_S)
    step = 50.0 * CYCLE_S * engine.RAMP_SAFETY_FACTOR
    total = float(str(out[0]["p_kw_setpoint"])) + float(str(out[1]["p_kw_setpoint"]))
    assert total == pytest.approx(-step)
    assert float(str(out[0]["p_kw_setpoint"])) == pytest.approx(-0.75 * step)
