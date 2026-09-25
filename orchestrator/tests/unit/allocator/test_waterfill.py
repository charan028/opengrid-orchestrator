"""TS-05: water-filling with stickiness (02a S5.5). Unit + property tests."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from opengrid.allocator.models import HubSnapshot
from opengrid.allocator.waterfill import water_fill

_EPS = 1e-6


def _hub(hub_id: str, free_kw: float, *, tau: float = 1.0, served: bool = False) -> HubSnapshot:
    return HubSnapshot(
        hub_id=hub_id, bank_id="b1", free_discharge_kw=free_kw, tau=tau, served_last_cycle=served
    )


def test_ts_05_01_splits_within_each_hub_capability() -> None:
    hubs = (_hub("h1", 10.0), _hub("h2", 20.0), _hub("h3", 5.0))
    out = water_fill(hubs, 15.0)
    assert sum(out.values()) == pytest.approx(15.0, abs=1e-6)
    for h in hubs:
        assert -_EPS <= out[h.hub_id] <= h.free_discharge_kw + _EPS


def test_ts_05_02_target_exceeds_total_capability_grants_everything() -> None:
    hubs = (_hub("h1", 10.0), _hub("h2", 5.0))
    out = water_fill(hubs, 1000.0)
    assert out == {"h1": 10.0, "h2": 5.0}


def test_ts_05_03_zero_target_grants_nothing() -> None:
    hubs = (_hub("h1", 10.0), _hub("h2", 5.0))
    out = water_fill(hubs, 0.0)
    assert out == {"h1": 0.0, "h2": 0.0}


def test_ts_05_04_stickiness_favors_hub_served_last_cycle() -> None:
    # Two identical-capability hubs, one served last cycle: it should get a larger share.
    hubs = (_hub("h1", 10.0, served=True), _hub("h2", 10.0, served=False))
    out = water_fill(hubs, 10.0)
    assert out["h1"] > out["h2"]


def test_ts_05_05_deterministic_tie_break_on_equal_ratio() -> None:
    hubs = (_hub("hZ", 10.0), _hub("hA", 10.0))
    out1 = water_fill(hubs, 10.0)
    out2 = water_fill(tuple(reversed(hubs)), 10.0)
    assert out1 == out2


def test_ts_05_06_empty_hub_set() -> None:
    assert water_fill((), 10.0) == {}


@given(
    caps=st.lists(st.floats(min_value=0.0, max_value=1000.0, allow_nan=False), min_size=1, max_size=25),
    target_frac=st.floats(min_value=0.0, max_value=1.5, allow_nan=False),
)
def test_ts_05_07_property_never_exceeds_hub_capability(caps: list[float], target_frac: float) -> None:
    """K1/K4: water-filling never proposes more than a hub's own (already reserve-safe) capability."""
    hubs = tuple(_hub(f"h{i}", cap) for i, cap in enumerate(caps))
    target = target_frac * sum(caps)
    out = water_fill(hubs, target)
    for h in hubs:
        assert out[h.hub_id] <= h.free_discharge_kw + 1e-6
        assert out[h.hub_id] >= -1e-6


@given(
    caps=st.lists(st.floats(min_value=0.1, max_value=1000.0, allow_nan=False), min_size=1, max_size=25),
    target_frac=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
)
def test_ts_05_08_property_sum_matches_target_when_feasible(caps: list[float], target_frac: float) -> None:
    hubs = tuple(_hub(f"h{i}", cap) for i, cap in enumerate(caps))
    total = sum(caps)
    target = target_frac * total
    out = water_fill(hubs, target)
    assert abs(sum(out.values()) - target) < 1e-3 * max(total, 1.0)
