"""TS-05: free-headroom price response, S6 dwell/hysteresis (02a S5.5). No-flapping invariant."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.allocator.models import DwellState
from opengrid.allocator.price_response import price_responsive_schedule

_T0 = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


def test_ts_05_40_switches_to_high_above_threshold_plus_hysteresis() -> None:
    state = DwellState(mode="LOW", last_switch_at=None)
    kw, state = price_responsive_schedule(100.0, 40.0, 30.0, state, _T0)
    assert state.mode == "HIGH"
    assert kw == 100.0


def test_ts_05_41_stays_low_inside_hysteresis_band() -> None:
    state = DwellState(mode="LOW", last_switch_at=None)
    kw, state = price_responsive_schedule(100.0, 31.0, 30.0, state, _T0)  # inside +-2.5 band
    assert state.mode == "LOW"
    assert kw == 0.0


def test_ts_05_42_no_flap_within_dwell_window() -> None:
    state = DwellState(mode="LOW", last_switch_at=None)
    _kw, state = price_responsive_schedule(100.0, 100.0, 30.0, state, _T0)
    assert state.mode == "HIGH"
    # Price crashes back below the lower threshold 1 minute later: dwell blocks the switch back.
    kw2, state2 = price_responsive_schedule(100.0, 0.0, 30.0, state, _T0 + timedelta(minutes=1))
    assert state2.mode == "HIGH"
    assert kw2 == 100.0


def test_ts_05_43_switch_allowed_after_dwell_elapses() -> None:
    state = DwellState(mode="LOW", last_switch_at=_T0)
    later = _T0 + timedelta(minutes=6)
    _kw, state = price_responsive_schedule(100.0, 100.0, 30.0, state, later)
    assert state.mode == "HIGH"
    assert state.last_switch_at == later


def test_ts_05_44_switch_back_to_low_below_threshold_minus_hysteresis() -> None:
    state = DwellState(mode="HIGH", last_switch_at=_T0)
    later = _T0 + timedelta(minutes=10)
    kw, state = price_responsive_schedule(100.0, 20.0, 30.0, state, later)
    assert state.mode == "LOW"
    assert kw == 0.0
