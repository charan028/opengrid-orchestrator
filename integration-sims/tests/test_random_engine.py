"""Tests for the autonomous random-mode engine: seed reproducibility, Poisson
rate statistics within tolerance, max-concurrency admission control, and
pause. All assertions use the pure planning/sampling functions or a fake
clock - no real `asyncio.sleep` is exercised, so nothing here is flaky."""

from __future__ import annotations

import numpy as np
import pytest

from ogsim.control.injector import Injector
from ogsim.control.random_config import ParamRange, RandomEngineConfig, TypeRandomConfig
from ogsim.control.random_engine import RandomEngine, plan_arrivals, sample_duration_s, sample_params

SECONDS_PER_HOUR = 3600.0
LARGE_WINDOW_HOURS = 200
RATE_TOLERANCE_FRACTION = 0.20  # Poisson counts are noisy; 20% is generous for a single large sample


def test_plan_arrivals_is_reproducible_for_the_same_seed():
    rate_per_hour = 5.0
    window_s = 10 * SECONDS_PER_HOUR
    plan_a = plan_arrivals(np.random.default_rng(42), rate_per_hour, window_s)
    plan_b = plan_arrivals(np.random.default_rng(42), rate_per_hour, window_s)
    assert plan_a == plan_b
    assert len(plan_a) > 0


def test_plan_arrivals_differs_for_different_seeds():
    rate_per_hour = 5.0
    window_s = 10 * SECONDS_PER_HOUR
    plan_a = plan_arrivals(np.random.default_rng(1), rate_per_hour, window_s)
    plan_b = plan_arrivals(np.random.default_rng(2), rate_per_hour, window_s)
    assert plan_a != plan_b


def test_plan_arrivals_rate_matches_configured_rate_within_tolerance():
    rate_per_hour = 3.0
    window_s = LARGE_WINDOW_HOURS * SECONDS_PER_HOUR
    arrivals = plan_arrivals(np.random.default_rng(123), rate_per_hour, window_s)
    observed_rate_per_hour = len(arrivals) / LARGE_WINDOW_HOURS
    assert observed_rate_per_hour == pytest.approx(rate_per_hour, rel=RATE_TOLERANCE_FRACTION)


def test_plan_arrivals_returns_nothing_for_a_zero_rate():
    assert plan_arrivals(np.random.default_rng(1), 0.0, SECONDS_PER_HOUR) == []


def test_plan_arrivals_are_strictly_increasing_and_within_window():
    arrivals = plan_arrivals(np.random.default_rng(1), 10.0, SECONDS_PER_HOUR)
    assert all(a < SECONDS_PER_HOUR for a in arrivals)
    assert arrivals == sorted(arrivals)


def _type_cfg(**overrides: object) -> TypeRandomConfig:
    defaults: dict[str, object] = {
        "type": "price_spike",
        "owner": "market",
        "enabled": True,
        "rate_per_hour": 1.0,
        "duration_s": (60.0, 60.0),
        "params": {"value_usd_per_mwh": ParamRange(1000.0, 1000.0)},
    }
    defaults.update(overrides)
    return TypeRandomConfig(**defaults)  # type: ignore[arg-type]


def test_sample_duration_is_fixed_when_range_has_equal_bounds():
    rng = np.random.default_rng(1)
    duration = sample_duration_s(rng, _type_cfg(duration_s=(60.0, 60.0)), duration_multiplier=1.0)
    assert duration == 60.0


def test_sample_duration_scales_with_the_profile_multiplier():
    rng = np.random.default_rng(1)
    duration = sample_duration_s(rng, _type_cfg(duration_s=(60.0, 60.0)), duration_multiplier=2.0)
    assert duration == 120.0


def test_sample_duration_stays_within_its_configured_range():
    rng = np.random.default_rng(1)
    cfg = _type_cfg(duration_s=(60.0, 300.0))
    for _ in range(50):
        duration = sample_duration_s(rng, cfg, duration_multiplier=1.0)
        assert 60.0 <= duration <= 300.0


def test_sample_params_returns_a_fixed_value_when_range_has_equal_bounds():
    rng = np.random.default_rng(1)
    params = sample_params(rng, _type_cfg(params={"value_usd_per_mwh": ParamRange(5000.0, 5000.0)}))
    assert params == {"value_usd_per_mwh": 5000.0}


def test_sample_params_stays_within_range():
    rng = np.random.default_rng(1)
    cfg = _type_cfg(params={"x": ParamRange(1.0, 2.0)})
    for _ in range(50):
        params = sample_params(rng, cfg)
        assert 1.0 <= params["x"] <= 2.0


def _engine_with_type(**config_overrides: object) -> tuple[RandomEngine, Injector]:
    injector = Injector()
    cfg = RandomEngineConfig(seed=1, types={"price_spike": _type_cfg()})
    for key, value in config_overrides.items():
        setattr(cfg, key, value)
    return RandomEngine(injector, cfg), injector


async def test_fire_injects_one_random_source_anomaly_when_admitted():
    engine, injector = _engine_with_type()
    anomaly_id = await engine.fire(_type_cfg())
    assert anomaly_id is not None
    active = injector.active(source="random")
    assert len(active) == 1
    assert active[0].source == "random"


async def test_fire_respects_the_max_concurrent_cap():
    engine, injector = _engine_with_type(max_concurrent=1)
    first = await engine.fire(_type_cfg(duration_s=(3600.0, 3600.0)))
    second = await engine.fire(_type_cfg(duration_s=(3600.0, 3600.0)))
    assert first is not None
    assert second is None  # dropped: cap already reached
    assert len(injector.active(source="random")) == 1


async def test_pause_prevents_any_random_injection():
    engine, injector = _engine_with_type()
    engine.pause()
    result = await engine.fire(_type_cfg())
    assert result is None
    assert injector.active(source="random") == []


async def test_resume_allows_injection_again_after_pause():
    engine, injector = _engine_with_type()
    engine.pause()
    engine.resume()
    result = await engine.fire(_type_cfg())
    assert result is not None


async def test_disabling_the_owning_sim_prevents_injection():
    engine, injector = _engine_with_type()
    engine.set_sim_enabled("market", False)
    result = await engine.fire(_type_cfg())
    assert result is None


def test_set_profile_rejects_an_unknown_profile():
    engine, _ = _engine_with_type()
    with pytest.raises(ValueError, match="unknown intensity profile"):
        engine.set_profile("extreme")


def test_status_reports_paused_profile_and_configured_types():
    engine, _ = _engine_with_type()
    status = engine.status()
    assert status["paused"] is False
    assert status["profile"] == "normal"
    assert "price_spike" in status["types"]
