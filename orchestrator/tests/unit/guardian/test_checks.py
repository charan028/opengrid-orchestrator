"""One negative test per canonical G-check (00-invariants.md), plus property tests for the K1/K4/K13
checks per BUILD.md's test list ("A negative test per G-check, and property tests")."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

from opengrid.core.physics import BankParams, HubParams
from opengrid.guardian import checks
from opengrid.guardian.config import DEFAULT_INVERTER_CAP_KW
from opengrid.guardian.ports import L2Instruction, ProposedItem

HUB = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0)
BANK = BankParams(kva_rating=75.0, reserve_kva=5.0)
NOW = datetime(2026, 9, 26, 18, 0, 0, tzinfo=UTC)


def _item(p_kw: float = 3.0) -> ProposedItem:
    return ProposedItem(hub_id="hub-1", p_kw_setpoint=p_kw, reason_code="SELECTOR")


def test_g01_reserve_floor_negative():
    r = checks.check_g01_reserve(_item(), HUB, soc_kwh=7.84)
    assert not r.ok and r.rule_id == "G-01" and r.reason == "RESERVE_FLOOR"


def test_g01_reserve_floor_positive():
    assert checks.check_g01_reserve(_item(), HUB, soc_kwh=20.0).ok


def test_g01_energy_lease_negative_discharge_drains_below_reserve_before_lease_expires():
    """K1: 3 kWh above reserve (10.84 - 7.84) can sustain 3 kW discharge for only ~1h before hitting
    reserve; a 2h lease at that rate would breach reserve partway through even though the instantaneous
    G-01 check (current SoC only) passes right now."""
    r = checks.check_g01_energy_lease(_item(p_kw=-3.0), HUB, soc_kwh=10.84, lease_ttl_h=2.0)
    assert not r.ok and r.rule_id == "G-01-ENERGY" and r.reason == "RESERVE_FLOOR_LEASE"
    # The instantaneous check alone would have passed -- proving this is a genuinely independent check.
    assert checks.check_g01_reserve(_item(p_kw=-3.0), HUB, soc_kwh=10.84).ok


def test_g01_energy_lease_positive_ample_energy_for_whole_lease():
    assert checks.check_g01_energy_lease(_item(p_kw=-3.0), HUB, soc_kwh=39.2, lease_ttl_h=2.0).ok


def test_g01_energy_lease_negative_charge_overfills_before_lease_expires():
    """Symmetric charge-direction projection: charging at 11 kW for a 3h lease from near-full SoC
    would overfill above e_kwh partway through."""
    r = checks.check_g01_energy_lease(_item(p_kw=11.0), HUB, soc_kwh=38.0, lease_ttl_h=3.0)
    assert not r.ok and r.rule_id == "G-01-ENERGY" and r.reason == "CHARGE_CEILING_LEASE"


def test_g02_hub_power_negative():
    r = checks.check_g02_hub_power(_item(p_kw=20.0), HUB, inverter_cap_kw=11.0)
    assert not r.ok and r.rule_id == "G-02"


def test_g02_hub_power_positive():
    assert checks.check_g02_hub_power(_item(p_kw=10.0), HUB, inverter_cap_kw=11.0).ok


def test_g02_dual_unit_home_at_20_kw_passes_and_21_kw_is_vetoed():
    """Regression (demo-critical): the guardian's default 11 kW cap vetoed every dual-unit home command
    above 11 kW. The home's own 20 kW rating is the cap."""
    dual = HubParams(e_kwh=78.4, r_kwh=15.68, p_kw=20.0)
    assert checks.check_g02_hub_power(_item(p_kw=-20.0), dual, inverter_cap_kw=DEFAULT_INVERTER_CAP_KW).ok
    r = checks.check_g02_hub_power(_item(p_kw=21.0), dual, inverter_cap_kw=DEFAULT_INVERTER_CAP_KW)
    assert not r.ok and r.rule_id == "G-02"


def test_g02_single_unit_home_above_11_kw_is_vetoed():
    single = HubParams(e_kwh=39.2, r_kwh=7.84, p_kw=11.0)
    assert not checks.check_g02_hub_power(
        _item(p_kw=11.5), single, inverter_cap_kw=DEFAULT_INVERTER_CAP_KW
    ).ok


def test_g03_bank_kva_negative():
    r = checks.check_g03_bank_kva("bank-1", 20.0, 70.0, BANK)
    assert not r.ok and r.rule_id == "G-03"


def test_g03_bank_kva_positive():
    assert checks.check_g03_bank_kva("bank-1", 2.0, 10.0, BANK).ok


def test_g04_hub_ramp_negative():
    r = checks.check_g04_hub_ramp(_item(p_kw=100.0), prev_p_kw=0.0, dt_s=2.0, ramp_kw_per_s=2.0)
    assert not r.ok and r.rule_id == "G-04"


def test_g04_hub_ramp_positive():
    assert checks.check_g04_hub_ramp(_item(p_kw=4.0), prev_p_kw=0.0, dt_s=2.0, ramp_kw_per_s=2.0).ok


def test_g05_fleet_ramp_negative():
    r = checks.check_g05_fleet_ramp(1000.0, dt_s=2.0, is_firm_event=False)
    assert not r.ok and r.rule_id == "G-05"


def test_g05_fleet_ramp_positive_firm():
    assert checks.check_g05_fleet_ramp(1000.0, dt_s=2.0, is_firm_event=True).ok


def test_g06_feeder_ramp_negative_firm_event():
    r = checks.check_g06_feeder_ramp(9999.0, dt_s=2.0, feeder_ceiling_kw_per_min=100.0, is_firm_event=True)
    assert not r.ok and r.rule_id == "G-06"


def test_g06_feeder_ramp_ignored_when_not_firm():
    assert checks.check_g06_feeder_ramp(
        9999.0, dt_s=2.0, feeder_ceiling_kw_per_min=100.0, is_firm_event=False
    ).ok


def test_g09_ledger_version_negative():
    r = checks.check_g09_ledger_version(1, 2)
    assert not r.ok and r.rule_id == "G-09" and r.reason == "STALE_LEDGER_VERSION"


def test_g09_ledger_version_positive():
    assert checks.check_g09_ledger_version(5, 5).ok


def test_g13_freshness_negative_stale_epoch():
    r = checks.check_g13_freshness(
        epoch=1,
        seq=1,
        last_accepted_epoch=2,
        last_accepted_seq=0,
        issued_at=NOW,
        expires_at=NOW + timedelta(10),
        now=NOW,
    )
    assert not r.ok and r.rule_id == "G-13" and r.reason == "STALE_EPOCH"


def test_g13_freshness_positive():
    r = checks.check_g13_freshness(
        epoch=2,
        seq=1,
        last_accepted_epoch=1,
        last_accepted_seq=5,
        issued_at=NOW,
        expires_at=NOW + timedelta(10),
        now=NOW,
    )
    assert r.ok


def test_g14_trace_preimage_negative():
    r = checks.check_g14_trace_preimage(False)
    assert not r.ok and r.rule_id == "G-14" and r.reason == "TRACE_PREIMAGE_MISSING"


def test_g14_trace_preimage_positive():
    assert checks.check_g14_trace_preimage(True).ok


def test_g15_l2_boundary_negative_estop():
    r = checks.check_g15_l2_boundary(L2Instruction(kind="ESTOP", limit_kw=None), "bank-1", 3.0)
    assert not r.ok and r.rule_id == "G-15"


def test_g15_l2_boundary_negative_limit_exceeded():
    r = checks.check_g15_l2_boundary(L2Instruction(kind="LIMIT", limit_kw=5.0), "bank-1", 8.0)
    assert not r.ok


def test_g15_l2_boundary_positive_no_instruction():
    assert checks.check_g15_l2_boundary(None, "bank-1", 100.0).ok


def test_g19_commitment_lock_negative_no_reason():
    r = checks.check_g19_commitment_lock("obl-1", new_kw=5.0, frozen_kw=10.0, prior_kw=10.0, reason_code=None)
    assert not r.ok and r.rule_id == "G-19" and r.reason == "R-COMMIT-LOCK-VIOLATION"


def test_g19_commitment_lock_positive_with_override():
    r = checks.check_g19_commitment_lock(
        "obl-1", new_kw=5.0, frozen_kw=10.0, prior_kw=10.0, reason_code="R-COMMIT-LOCK-OVERRIDE-L1"
    )
    assert r.ok


def test_g20_clock_quality_negative():
    r = checks.check_g20_clock_quality(250.0, 200.0)
    assert not r.ok and r.rule_id == "G-20" and r.reason == "CLOCK_OFFSET_EXCEEDED"


def test_g20_clock_quality_positive():
    assert checks.check_g20_clock_quality(50.0, 200.0).ok


def test_obligation_totals_sums_per_obligation():
    a = "11111111-1111-1111-1111-111111111111"
    items = [
        ProposedItem(
            hub_id="h1", p_kw_setpoint=1.0, reason_code="x", obligation_id=a, obligation_granted_kw=Decimal(3)
        ),
        ProposedItem(
            hub_id="h2", p_kw_setpoint=1.0, reason_code="x", obligation_id=a, obligation_granted_kw=Decimal(4)
        ),
        ProposedItem(hub_id="h3", p_kw_setpoint=1.0, reason_code="x"),
    ]
    totals = checks.obligation_totals(items)
    assert totals == {a: Decimal(7)}


@given(soc_kwh=st.floats(min_value=0, max_value=39.2), margin_pct=st.just(0.01))
def test_g01_reserve_floor_property(soc_kwh, margin_pct):
    r = checks.check_g01_reserve(_item(), HUB, soc_kwh, margin_pct=margin_pct)
    assert r.ok == (soc_kwh >= HUB.r_kwh + HUB.e_kwh * margin_pct)


@given(
    prev_p_kw=st.floats(min_value=-11, max_value=11),
    target_p_kw=st.floats(min_value=-11, max_value=11),
    ramp_kw_per_s=st.floats(min_value=0.1, max_value=20),
)
def test_g04_hub_ramp_property(prev_p_kw, target_p_kw, ramp_kw_per_s):
    r = checks.check_g04_hub_ramp(_item(p_kw=target_p_kw), prev_p_kw, dt_s=2.0, ramp_kw_per_s=ramp_kw_per_s)
    assert r.ok == (abs(target_p_kw - prev_p_kw) <= ramp_kw_per_s * 2.0 + 1e-9)


@given(
    new_kw=st.floats(min_value=-50, max_value=50),
    frozen_kw=st.floats(min_value=0, max_value=50),
    prior_kw=st.floats(min_value=0, max_value=50),
    reason_code=st.sampled_from([None, "R-COMMIT-LOCK-OVERRIDE-L0", "BOGUS"]),
)
def test_g19_commitment_lock_property(new_kw, frozen_kw, prior_kw, reason_code):
    r = checks.check_g19_commitment_lock("obl-1", new_kw, frozen_kw, prior_kw, reason_code)
    floor = min(frozen_kw, prior_kw)
    if new_kw >= floor - 1e-9 or reason_code == "R-COMMIT-LOCK-OVERRIDE-L0":
        assert r.ok
    else:
        assert not r.ok
