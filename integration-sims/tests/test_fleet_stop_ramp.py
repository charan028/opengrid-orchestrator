"""ogsim.fleet.runtime.FleetEngine / ogsim.fleet.stop.StopRampTracker: live bug fix (2026-09-26,
FLEET-SIM/R3) -- the stop ramp never completed. `effective_commanded` was rebuilt fresh from
`state.p_kw_commanded` every tick, and `ramp_toward_zero` took one step from THAT unchanged value each
time, so an 11 kW hub commanded at 9 kW sat at one ramp step (3.5 kW at a 2 s tick / 4 s ramp) for as
long as it stayed stopped, instead of reaching 0 within `stop_ramp_s`. The fix keeps per-hub ramp state
(`StopRampTracker`) that starts from the hub's actual last output and accumulates down each tick, reset
on release."""

from __future__ import annotations

from dataclasses import replace as dc_replace

import pytest

from ogsim.common.config import MqttSettings, load_fleet_config
from ogsim.fleet.runtime import FleetEngine

MQTT = MqttSettings(host="127.0.0.1", port=1883, username="og_sim", password="", topic_root="og/v1")


def _engine(**overrides: object) -> FleetEngine:
    config = dc_replace(
        load_fleet_config(),
        mqtt=MQTT,
        hub_count=1,
        bank_count=1,
        zones=("LZ_NORTH",),
        stop_ramp_s=4.0,
        telemetry_interval_s=2.0,
        **overrides,
    )
    return FleetEngine(config, seed=1)


def _discharging_hub(engine: FleetEngine, p_kw: float) -> tuple[str, int]:
    """Sets up hub 0 as already discharging at `p_kw` (held by a lease, full SoC so nothing else
    clips it) -- the "actual current output" a stop ramp must start from."""
    hub_id = engine.state.hub_ids[0]
    idx = engine.state.hub_index[hub_id]
    engine.state.soc_kwh[idx] = engine.state.e_kwh[idx]
    engine.state.lease_expires_at[idx] = 1_000_000.0  # far-future: stays leased, no local-autonomy zero
    engine.state.p_kw_commanded[idx] = -p_kw
    engine.state.p_kw_applied[idx] = -p_kw  # "actual current output" before the stop engages
    return hub_id, idx


def test_9kw_hub_reaches_zero_within_stop_ramp_s() -> None:
    """11 kW-rated hub (p_kw_limit=11.0 default), commanded/actual at -9 kW, stop_ramp_s=4.0, 2 s
    ticks: must reach (and hold at) 0 within stop_ramp_s / telemetry_interval_s = 2 ticks -- not sit at
    one ramp step forever."""
    engine = _engine()
    hub_id, idx = _discharging_hub(engine, 9.0)
    engine.stops.engage("bank", "bank-000")

    now = 0.0
    seen = []
    for _ in range(6):  # well past stop_ramp_s (4.0 s) at a 2 s tick
        now += 2.0
        engine.tick(now)
        seen.append(float(engine.state.p_kw_applied[idx]))

    assert seen[-1] == 0.0, f"hub never reached 0: {seen}"
    # Must actually have DECREASED in magnitude tick over tick (accumulated), not sat at the same
    # non-zero value every tick (the bug's exact symptom).
    assert abs(seen[0]) > abs(seen[1]) or seen[1] == 0.0
    assert len({round(v, 6) for v in seen[:2]}) > 1 or seen[0] == 0.0


def test_ramp_reaches_zero_strictly_faster_than_sitting_at_one_step_forever() -> None:
    """Regression guard for the exact bug report: an 11 kW hub commanded at 9 kW must NOT sit at
    3.5 kW (one ramp step: 11 kW-limit * 2s/4s = 5.5 kW/step from 9 -> 3.5) forever."""
    engine = _engine()
    _hub_id, idx = _discharging_hub(engine, 9.0)
    engine.stops.engage("bank", "bank-000")

    engine.tick(2.0)
    first_step = float(engine.state.p_kw_applied[idx])
    engine.tick(4.0)
    second_step = float(engine.state.p_kw_applied[idx])

    assert first_step == pytest.approx(-3.5, abs=1e-6)  # first step: -9 + 5.5 = -3.5 (matches the bug report)
    assert second_step == 0.0  # the FIX: it keeps going, reaching 0 on the second step, not stuck at -3.5


def test_release_restores_normal_command_following() -> None:
    """After a verified RELEASE (simulated here by simply un-stopping the scope, since
    `StopRegistry`'s own release mechanics are covered by test_fleet_stop.py), the hub must resume
    following its full commanded setpoint immediately -- not continue ramping from wherever it was, and
    not require a fresh command to "unstick" it."""
    engine = _engine()
    hub_id, idx = _discharging_hub(engine, 9.0)
    engine.stops.engage("bank", "bank-000", stop_id="s1")

    engine.tick(2.0)
    assert engine.state.p_kw_applied[idx] == pytest.approx(-3.5, abs=1e-6)  # ramping down

    assert engine.stops.release_stop("bank", "bank-000", "s1", issued_at=3.0)
    engine.tick(4.0)

    # Back to the full commanded -9 kW (still commanded the whole time), not continuing from -3.5:
    assert engine.state.p_kw_applied[idx] == pytest.approx(-9.0, abs=1e-6)


def test_ramp_state_does_not_leak_into_a_later_stop() -> None:
    """A hub released, then stopped again later, must ramp from ITS THEN-CURRENT actual output, not
    from a stale ramped-down value left over from the earlier stop."""
    engine = _engine()
    hub_id, idx = _discharging_hub(engine, 9.0)
    engine.stops.engage("bank", "bank-000", stop_id="s1")
    engine.tick(2.0)
    assert engine.state.p_kw_applied[idx] == pytest.approx(-3.5, abs=1e-6)

    assert engine.stops.release_stop("bank", "bank-000", "s1", issued_at=3.0)
    engine.tick(4.0)
    assert engine.state.p_kw_applied[idx] == pytest.approx(-9.0, abs=1e-6)  # back to full command

    engine.stops.engage("bank", "bank-000", stop_id="s2", issued_at=5.0)
    engine.tick(6.0)
    # Ramps from -9 kW again (fresh), not from the -3.5 kW the earlier stop left behind.
    assert engine.state.p_kw_applied[idx] == pytest.approx(-3.5, abs=1e-6)
