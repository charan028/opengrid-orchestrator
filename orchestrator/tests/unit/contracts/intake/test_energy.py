"""Hand-computed arbitrage-value tests for `opengrid.contracts.intake.energy` (task brief: "unit tests
with fake prices (hand-computed arbitrage values)")."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opengrid.contracts.intake.energy import DEFAULT_ETA_RT, compute_energy_candidates
from opengrid.core.timeutil import floor_to_interval

NOW = datetime(2026, 9, 25, 12, 3, tzinfo=UTC)  # mid-interval, so horizon starts at 12:15


def _slot(n: int) -> datetime:
    return floor_to_interval(NOW) + timedelta(minutes=15 * (n + 1))


def test_positive_spread_is_offered_at_hand_computed_value() -> None:
    # charge at $20/MWh, discharge at $50/MWh, eta_rt=0.90, degradation $0.03/kWh = $30/MWh.
    # spread = 50 - 20/0.90 - 30 = 50 - 22.222... - 30 = -2.222... -> NOT positive at this charge price.
    # Use a charge price low enough to clear the degradation + efficiency loss: $10/MWh.
    # spread = 50 - 10/0.90 - 30 = 50 - 11.111... - 30 = 8.888...
    candidates = compute_energy_candidates(
        now=NOW,
        charge_price_usd_per_mwh=10.0,
        discharge_p50_by_interval={_slot(0): 50.0},
        degradation_usd_per_kwh=Decimal("0.03"),
        eta_rt=DEFAULT_ETA_RT,
    )
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.window_start == _slot(0)
    assert candidate.window_end == _slot(0) + timedelta(minutes=15)
    expected = Decimal("50") - (Decimal("10") / Decimal("0.90")) - Decimal("30")
    assert abs(candidate.value_per_mwh - expected) < Decimal("0.0001")
    assert candidate.value_per_mwh > 0


def test_negative_spread_is_not_offered() -> None:
    # spread = 50 - 20/0.90 - 30 = -2.222... < 0
    candidates = compute_energy_candidates(
        now=NOW,
        charge_price_usd_per_mwh=20.0,
        discharge_p50_by_interval={_slot(0): 50.0},
        degradation_usd_per_kwh=Decimal("0.03"),
        eta_rt=DEFAULT_ETA_RT,
    )
    assert candidates == []


def test_only_intervals_with_a_forecast_point_are_considered() -> None:
    candidates = compute_energy_candidates(
        now=NOW,
        charge_price_usd_per_mwh=10.0,
        discharge_p50_by_interval={_slot(3): 100.0},  # no point at slots 0,1,2
        degradation_usd_per_kwh=Decimal("0.03"),
    )
    assert len(candidates) == 1
    assert candidates[0].window_start == _slot(3)


def test_multiple_positive_intervals_each_produce_a_candidate() -> None:
    candidates = compute_energy_candidates(
        now=NOW,
        charge_price_usd_per_mwh=10.0,
        discharge_p50_by_interval={_slot(0): 50.0, _slot(1): 60.0, _slot(2): 5.0},  # slot 2 -> negative
        degradation_usd_per_kwh=Decimal("0.03"),
    )
    windows = {c.window_start for c in candidates}
    assert windows == {_slot(0), _slot(1)}


def test_horizon_bound_excludes_intervals_beyond_it() -> None:
    candidates = compute_energy_candidates(
        now=NOW,
        charge_price_usd_per_mwh=10.0,
        discharge_p50_by_interval={_slot(0): 50.0, _slot(50): 999.0},
        degradation_usd_per_kwh=Decimal("0.03"),
        horizon_intervals=8,
    )
    windows = {c.window_start for c in candidates}
    assert _slot(50) not in windows
    assert _slot(0) in windows
