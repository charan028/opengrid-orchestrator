"""Continuous per-obligation energy-sufficiency check (K1). Proves: missing SoC contributes zero kWh,
substitution rescues a marginal obligation using other eligible hubs' energy first, and the depletion
clock flags AT_RISK before the window actually ends -- plus property tests (d) from the build brief."""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.allocator.energy_sufficiency import (
    HubEnergyState,
    evaluate_energy_sufficiency,
    evaluate_with_substitution,
    hub_available_kwh_net_of_other_reservations,
)


def _hub(
    hub_id: str, soc_kwh: float | None, reserve_kwh: float = 7.84, eta_d: float = 0.9487
) -> HubEnergyState:
    return HubEnergyState(hub_id=hub_id, soc_kwh=soc_kwh, reserve_kwh=reserve_kwh, eta_d=eta_d)


def test_ample_energy_is_not_at_risk() -> None:
    hubs = [_hub("h1", soc_kwh=39.2), _hub("h2", soc_kwh=39.2)]
    result = evaluate_energy_sufficiency(
        "o1", committed_kw=5.0, remaining_window_h=1.0, eligible_hubs=hubs, reserved_kwh_by_hub_for_others={}
    )
    assert not result.at_risk
    assert result.margin_kwh > 0


def test_missing_soc_contributes_zero_kwh_and_flags_at_risk() -> None:
    """K1/K7: a hub with no live SoC this cycle contributes 0 kWh, never an assumed value -- an
    obligation relying on it for its whole delivery is AT_RISK even though the hub's nameplate energy
    would otherwise be ample."""
    hubs = [_hub("h1", soc_kwh=None)]
    result = evaluate_energy_sufficiency(
        "o1", committed_kw=5.0, remaining_window_h=1.0, eligible_hubs=hubs, reserved_kwh_by_hub_for_others={}
    )
    assert result.available_kwh == 0.0
    assert result.at_risk


def test_energy_already_reserved_for_another_obligation_is_excluded_k2() -> None:
    """K2: a hub's energy above reserve backs at most one obligation -- energy already promised
    elsewhere on a shared hub must not double-count toward this obligation's sufficiency."""
    hub = _hub("h1", soc_kwh=20.0)  # ~11.6 kWh above reserve at eta_d
    gross = hub_available_kwh_net_of_other_reservations(hub, {})
    net = hub_available_kwh_net_of_other_reservations(hub, {"h1": gross})
    assert net == 0.0  # fully claimed by the other obligation


def test_margin_negative_flags_at_risk() -> None:
    hubs = [_hub("h1", soc_kwh=8.84)]  # 1 kWh above reserve
    result = evaluate_energy_sufficiency(
        "o1", committed_kw=5.0, remaining_window_h=1.0, eligible_hubs=hubs, reserved_kwh_by_hub_for_others={}
    )
    assert result.margin_kwh < 0
    assert result.at_risk


def test_depletion_before_window_end_flags_at_risk_even_with_positive_margin_impossible_but_time_check() -> (
    None
):
    """Depletion-time check: with `committed_kw` fixed, margin<0 and time_to_depletion<window are the
    same condition algebraically for a flat draw -- this test pins down the depletion-time field itself,
    which the UI/alert needs even when margin alone would already have flagged it."""
    hubs = [_hub("h1", soc_kwh=8.84)]
    result = evaluate_energy_sufficiency(
        "o1", committed_kw=2.0, remaining_window_h=2.0, eligible_hubs=hubs, reserved_kwh_by_hub_for_others={}
    )
    assert result.time_to_depletion_h is not None
    assert result.time_to_depletion_h < 2.0
    assert result.at_risk


def test_zero_draw_has_no_depletion_time_and_is_never_at_risk_on_that_basis() -> None:
    hubs = [_hub("h1", soc_kwh=39.2)]
    result = evaluate_energy_sufficiency(
        "o1", committed_kw=0.0, remaining_window_h=1.0, eligible_hubs=hubs, reserved_kwh_by_hub_for_others={}
    )
    assert result.time_to_depletion_h is None
    assert not result.at_risk


def test_substitution_rescues_at_risk_obligation_using_other_eligible_hubs_energy_first() -> None:
    primary = [_hub("h1", soc_kwh=8.84)]  # 1 kWh above reserve -- not enough alone
    substitutes = [_hub("h2", soc_kwh=39.2)]  # ample
    result = evaluate_with_substitution(
        "o1",
        committed_kw=5.0,
        remaining_window_h=1.0,
        primary_hubs=primary,
        substitute_hubs=substitutes,
        reserved_kwh_by_hub_for_others={},
    )
    assert not result.at_risk
    assert result.used_substitution


def test_substitution_still_at_risk_when_even_combined_energy_is_insufficient() -> None:
    primary = [_hub("h1", soc_kwh=8.0)]
    substitutes = [_hub("h2", soc_kwh=8.0)]
    result = evaluate_with_substitution(
        "o1",
        committed_kw=100.0,
        remaining_window_h=5.0,
        primary_hubs=primary,
        substitute_hubs=substitutes,
        reserved_kwh_by_hub_for_others={},
    )
    assert result.at_risk
    assert not result.used_substitution


@given(
    soc_values=st.lists(
        st.one_of(st.none(), st.floats(min_value=0, max_value=100, allow_nan=False)), min_size=1, max_size=6
    ),
    committed_kw=st.floats(min_value=0.01, max_value=50, allow_nan=False),
    remaining_window_h=st.floats(min_value=0.01, max_value=48, allow_nan=False),
)
@settings(max_examples=100)
def test_property_at_risk_iff_depletion_before_window_end_or_margin_negative(
    soc_values: list[float | None], committed_kw: float, remaining_window_h: float
) -> None:
    """Property (d): the sufficiency check flags AT_RISK exactly when depletion would occur before the
    window ends (or the margin is already negative, the t=0 case of the same condition)."""
    hubs = [_hub(f"h{i}", soc_kwh=soc) for i, soc in enumerate(soc_values)]
    result = evaluate_energy_sufficiency(
        "o1", committed_kw, remaining_window_h, hubs, reserved_kwh_by_hub_for_others={}
    )
    expected_at_risk = result.margin_kwh < 0 or (
        result.time_to_depletion_h is not None and result.time_to_depletion_h < remaining_window_h
    )
    assert result.at_risk == expected_at_risk
    assert result.available_kwh >= 0.0
    # A hub with soc_kwh=None never contributes energy (K1/K7 conservative fallback).
    if all(soc is None for soc in soc_values):
        assert result.available_kwh == 0.0


@given(
    demands=st.lists(st.floats(min_value=0, max_value=20, allow_nan=False), min_size=1, max_size=5),
    soc_kwh=st.floats(min_value=0, max_value=100, allow_nan=False),
)
@settings(max_examples=50)
def test_property_k2_sum_of_available_energy_never_exceeds_hub_gross_energy(
    demands: list[float], soc_kwh: float
) -> None:
    """Property (b): summed across several obligations sharing one hub, the energy each is told is
    "available" (net of what's reserved for the others) never exceeds the hub's own gross energy above
    reserve -- K2's "one buyer" extended to energy."""
    hub = _hub("h1", soc_kwh=soc_kwh)
    gross = hub_available_kwh_net_of_other_reservations(hub, {})
    reserved_so_far = 0.0
    total_available = 0.0
    for demand in demands:
        net = hub_available_kwh_net_of_other_reservations(hub, {"h1": reserved_so_far})
        claimed = min(net, demand)
        total_available += claimed
        reserved_so_far += claimed
    assert total_available <= gross + 1e-9
