"""Closed-loop controllers for DATA_CENTER (S4.b) and PIPELINE_AC (S4.a): proportional only (K9), bounded
by the commitment and capacity (K13/K4), signal loss handled per profile, PCC PQ fed to the S5.4 ladder,
and fast enough for the 2 s cycle."""

from __future__ import annotations

import time
import tomllib
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.allocator import closed_loop_data_center as dc
from opengrid.allocator import closed_loop_pipeline_ac as pac
from opengrid.allocator.closed_loop_common import (
    PQ_MEASUREMENT_MISSING,
    PQ_PF_BELOW_MIN,
    ControlMode,
    closed_loop_caps,
    monitor_pq,
)
from opengrid.allocator.models import ObligationCall
from opengrid.allocator.pq_monitor import LadderState
from opengrid.core.pq import ComplianceState, HysteresisConfig, PqEnvelopeLimits
from opengrid.site_ingest.models import CorridorCurrentReading, FeedbackValue, SiteMeterReading

TS = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
PROFILE = Path(__file__).resolve().parents[3] / "config" / "service_profiles" / "data_center.toml"
DC_PARAMS = dc.DataCenterParams(deadband_kw=5.0, ramp_kw_per_min=30_000.0, freshness_s=2.0, kp=0.5)
PAC_PARAMS = pac.PipelineAcParams(
    line_kv=138.0, power_factor=0.98, shift_factor=0.2, direction=1, band_kw=500.0, freshness_s=4.0
)
DC_LIMITS = PqEnvelopeLimits(
    max_phase_imbalance_pct=1.5,
    voltage_band_pct=2.0,
    freq_tolerance_hz=0.5,
    pf_min=0.95,
    thd_voltage_limit_pct=2.5,
    thd_current_limit_pct=2.5,
)
FAST = HysteresisConfig(warn_dwell_s=0.0, breach_dwell_s=0.0, recovery_dwell_s=0.0)


def fb(value: float, *, age_s: float = 0.5, quality: str = "GOOD", max_age_s: float = 2.0) -> FeedbackValue:
    return FeedbackValue("ref", value, TS, age_s, quality, max_age_s)  # type: ignore[arg-type]


def dc_inputs(meter: FeedbackValue | None, **kw: float | None) -> dc.DataCenterInputs:
    values: dict[str, float | None] = {
        "target_import_kw": 400.0,
        "scheduled_kw": 300.0,
        "committed_kw": 500.0,
        "available_kw": 600.0,
    }
    values |= kw
    return dc.DataCenterInputs(meter=meter, **values)  # type: ignore[arg-type]


# -- DATA_CENTER ---------------------------------------------------------------------------------------


def test_dc_output_is_feed_forward_plus_proportional_error() -> None:
    out = dc.step(dc_inputs(fb(500.0)), DC_PARAMS, dc.DataCenterState(300.0), now_s=0.0, dt_s=2.0)
    # e = 500 - 400 = 100, deadband 5 -> 95; u = 300 + 0.5 * 95
    assert out.setpoint_kw == pytest.approx(347.5)
    assert out.mode is ControlMode.TRACKING and out.error_kw == 100.0


def test_dc_constant_error_never_accumulates() -> None:
    """Proportional only (K9): a persistent error gives a constant output, not a growing one."""
    state = dc.DataCenterState(300.0)
    outputs = []
    for k in range(200):
        out = dc.step(dc_inputs(fb(460.0)), DC_PARAMS, state, now_s=2.0 * k, dt_s=2.0)
        state = out.state
        outputs.append(out.setpoint_kw)
    assert outputs[-1] == outputs[1] == pytest.approx(300.0 + 0.5 * 55.0)


@given(prev=st.floats(0, 500), meter=st.floats(-2000, 5000))
def test_dc_output_does_not_depend_on_history_without_a_ramp_limit(prev: float, meter: float) -> None:
    fresh = dc.step(dc_inputs(fb(meter)), DC_PARAMS, dc.DataCenterState(0.0), now_s=0.0, dt_s=2.0)
    later = dc.step(dc_inputs(fb(meter)), DC_PARAMS, dc.DataCenterState(prev), now_s=0.0, dt_s=2.0)
    assert fresh.setpoint_kw == pytest.approx(later.setpoint_kw)


@settings(max_examples=300)
@given(
    meter=st.one_of(st.none(), st.floats(-5000, 5000)),
    target=st.one_of(st.none(), st.floats(-1000, 3000)),
    scheduled=st.floats(-100, 3000),
    committed=st.floats(0, 2000),
    available=st.floats(-50, 2000),
    prev=st.floats(0, 3000),
    age=st.floats(0, 10),
    now=st.floats(0, 5000),
)
def test_dc_output_stays_inside_the_committed_envelope(
    meter, target, scheduled, committed, available, prev, age, now
) -> None:
    value = None if meter is None else fb(meter, age_s=age)
    inputs = dc_inputs(
        value, target_import_kw=target, scheduled_kw=scheduled, committed_kw=committed, available_kw=available
    )
    params = dc.DataCenterParams(deadband_kw=1.0, ramp_kw_per_min=600.0, freshness_s=2.0, hold_s=30.0)
    out = dc.step(inputs, params, dc.DataCenterState(prev, signal_lost_since_s=None), now_s=now, dt_s=2.0)
    assert 0.0 <= out.setpoint_kw <= max(min(committed, available), 0.0) + 1e-9


def test_dc_closed_loop_converges_against_the_site_load() -> None:
    """Plant: the site meter reads load minus last cycle's fleet output; the loop settles (pole -Kp)."""
    load, state, meter = 900.0, dc.DataCenterState(0.0), 900.0
    outputs = []
    for k in range(40):
        out = dc.step(dc_inputs(fb(meter)), DC_PARAMS, state, now_s=2.0 * k, dt_s=2.0)
        state = out.state
        outputs.append(out.setpoint_kw)
        meter = load - out.setpoint_kw
    assert abs(outputs[-1] - outputs[-2]) < 1e-6
    assert 300.0 < outputs[-1] <= 500.0


@pytest.mark.parametrize(
    "meter", [None, fb(500.0, age_s=2.5), fb(500.0, quality="SUSPECT"), fb(500.0, quality="BAD")]
)
def test_dc_signal_loss_holds_then_follows_the_schedule(meter: FeedbackValue | None) -> None:
    params = dc.DataCenterParams(deadband_kw=5.0, ramp_kw_per_min=60.0, freshness_s=2.0, hold_s=900.0)
    held = dc.step(dc_inputs(meter), params, dc.DataCenterState(420.0), now_s=100.0, dt_s=2.0)
    assert held.mode is ControlMode.HOLD and held.setpoint_kw == 420.0
    still = dc.step(dc_inputs(meter), params, held.state, now_s=999.0, dt_s=2.0)
    assert still.mode is ControlMode.HOLD and still.setpoint_kw == 420.0
    after = dc.step(dc_inputs(meter), params, still.state, now_s=1001.0, dt_s=2.0)
    assert after.mode is ControlMode.SCHEDULE
    assert after.setpoint_kw == pytest.approx(418.0)  # toward 300 kW scheduled at 60 kW/min, never a step
    recovered = dc.step(dc_inputs(fb(400.0)), params, after.state, now_s=1003.0, dt_s=2.0)
    assert recovered.mode is ControlMode.TRACKING and recovered.state.signal_lost_since_s is None


def test_dc_never_above_the_commitment_even_when_scheduled_higher() -> None:
    out = dc.step(
        dc_inputs(fb(5000.0), scheduled_kw=800.0), DC_PARAMS, dc.DataCenterState(500.0), now_s=0, dt_s=2
    )
    assert out.setpoint_kw == 500.0


def test_dc_capacity_drop_binds_at_once() -> None:
    out = dc.step(
        dc_inputs(fb(400.0), available_kw=100.0),
        dc.DataCenterParams(5, 60, 2),
        dc.DataCenterState(450.0),
        now_s=0,
        dt_s=2,
    )
    assert out.setpoint_kw == 100.0


def test_dc_after_a_shortfall_delivers_the_maximum_feasible_and_restores_fast() -> None:
    """Best effort after a mid-window SHORTFALL: lost capacity lowers the output to what is feasible,
    never to 0, and the output returns within one cycle when capacity comes back (profile ramp)."""
    profile = tomllib.loads(PROFILE.read_text(encoding="utf-8"))
    params = dc.params_from_profile(profile, 500.0)  # 15,000 kW/min: the full 500 kW within one 2 s cycle
    state = dc.DataCenterState(450.0)
    short = dc.step(dc_inputs(fb(900.0), available_kw=180.0), params, state, now_s=0.0, dt_s=2.0)
    assert short.setpoint_kw == 180.0 and short.mode is ControlMode.TRACKING
    restored = dc.step(dc_inputs(fb(900.0), available_kw=600.0), params, short.state, now_s=2.0, dt_s=2.0)
    assert restored.setpoint_kw == 500.0


def test_dc_stale_signal_during_a_shortfall_holds_the_feasible_level_not_zero() -> None:
    params = dc.DataCenterParams(deadband_kw=5.0, ramp_kw_per_min=60.0, freshness_s=2.0, hold_s=900.0)
    held = dc.step(
        dc_inputs(None, available_kw=200.0), params, dc.DataCenterState(420.0), now_s=0.0, dt_s=2.0
    )
    assert held.mode is ControlMode.HOLD and held.setpoint_kw == 200.0
    back = dc.step(
        dc_inputs(None, available_kw=600.0), params, dc.with_delivered(held.state, 200.0), now_s=2.0, dt_s=2.0
    )
    assert back.mode is ControlMode.HOLD and back.setpoint_kw == 200.0  # holds the delivered level


def test_dc_without_a_target_runs_the_schedule() -> None:
    out = dc.step(
        dc_inputs(fb(700.0), target_import_kw=None), DC_PARAMS, dc.DataCenterState(0.0), now_s=0, dt_s=2
    )
    assert out.mode is ControlMode.SCHEDULE and out.setpoint_kw == 300.0


@pytest.mark.parametrize("kp", [0.0, 1.0, 1.5, -0.1])
def test_dc_gain_outside_the_stable_range_is_refused(kp: float) -> None:
    with pytest.raises(ValueError):
        dc.DataCenterParams(deadband_kw=1.0, ramp_kw_per_min=60.0, freshness_s=2.0, kp=kp)


def test_dc_params_from_the_profile_template() -> None:
    profile = tomllib.loads(PROFILE.read_text(encoding="utf-8"))
    params = dc.params_from_profile(profile, 400.0)
    assert (params.deadband_kw, params.ramp_kw_per_min, params.freshness_s) == (4.0, 12_000.0, 2.0)


def test_dc_with_delivered_lowers_the_held_level_only() -> None:
    state = dc.DataCenterState(400.0)
    assert dc.with_delivered(state, 250.0).output_kw == 250.0
    assert dc.with_delivered(state, 900.0).output_kw == 400.0


def site_reading(**overrides: float | str) -> SiteMeterReading:
    base: dict[str, float | str] = {
        "site_id": "s",
        "customer_id": "c",
        "ts": TS.isoformat(),
        "p_kw": 500.0,
        "q_kvar": 0.0,
        "v_rms_a_v": 277.0,
        "v_rms_b_v": 277.0,
        "v_rms_c_v": 277.0,
        "i_rms_a_a": 100.0,
        "i_rms_b_a": 100.0,
        "i_rms_c_a": 100.0,
        "freq_hz": 60.0,
        "pf": 0.98,
        "thd_v_pct": 1.0,
        "thd_i_pct": 1.0,
        "quality": "GOOD",
    }
    return SiteMeterReading.model_validate(base | overrides)


def test_pcc_measurement_uses_the_worse_imbalance_and_worst_phase() -> None:
    m = dc.pcc_measurement(site_reading(i_rms_a_a=110.0, v_rms_c_v=282.54), nominal_v=277.0)
    assert m.imbalance_pct == pytest.approx(100 * (110 - 310 / 3) / (310 / 3))
    assert m.voltage_deviation_pct == pytest.approx(2.0, abs=1e-3)
    assert m.current_a == 110.0


def test_pcc_imbalance_breach_escalates_through_the_ladder() -> None:
    measurement = dc.pcc_measurement(site_reading(i_rms_a_a=120.0), nominal_v=277.0)
    ladder = LadderState()
    for k in range(4):  # one observation to start the (zero) dwell, then three breach cycles
        result = monitor_pq("ob-1", measurement, DC_LIMITS, ladder, float(k), hysteresis_config=FAST)
        ladder = result.ladder_state
    assert result.monitor is not None and result.monitor.verdict is ComplianceState.BREACH
    assert result.monitor.worst_dimension == "imbalance_pct"
    assert ladder.at_risk


def test_pcc_missing_measurement_is_never_compliant() -> None:
    ladder = LadderState(cycles_in_breach=2)
    stale = dc.usable_pcc_measurement(site_reading(), fb(500.0, age_s=5.0), nominal_v=277.0)
    result = monitor_pq("ob-1", stale, DC_LIMITS, ladder, 0.0)
    assert result.monitor is None and result.flags == (PQ_MEASUREMENT_MISSING,)
    assert result.ladder_state is ladder


def test_pcc_low_power_factor_is_flagged() -> None:
    measurement = dc.pcc_measurement(site_reading(pf=-0.9), nominal_v=277.0)
    assert monitor_pq("ob-1", measurement, DC_LIMITS, LadderState(), 0.0).flags == (PQ_PF_BELOW_MIN,)


# -- PIPELINE_AC ---------------------------------------------------------------------------------------


def pac_inputs(current: FeedbackValue | None, **kw: float) -> pac.PipelineAcInputs:
    values = {"scheduled_kw": 120.0, "committed_kw": 400.0, "available_kw": 450.0} | kw
    return pac.PipelineAcInputs(current=current, **values)


def test_pac_kw_per_amp_matches_the_spec_scale() -> None:
    radial = pac.PipelineAcParams(
        line_kv=138.0, power_factor=0.98, shift_factor=1.0, direction=1, band_kw=500, freshness_s=4
    )
    assert radial.kw_per_amp == pytest.approx(234.2, abs=0.1)  # 03 S8.6.6: 234 kW per ampere at SF = 1


def test_pac_first_reading_initialises_the_reference_without_a_step() -> None:
    out = pac.step(pac_inputs(fb(12.0, max_age_s=4.0)), PAC_PARAMS, pac.PipelineAcState(), dt_s=2.0)
    assert out.state.reference_a == 12.0 and out.setpoint_kw == 0.0


def test_pac_current_step_requests_smoothing_that_decays_without_integration() -> None:
    state = pac.PipelineAcState(reference_a=10.0)
    requests = []
    for _ in range(40):
        out = pac.step(pac_inputs(fb(12.0, max_age_s=4.0)), PAC_PARAMS, state, dt_s=2.0)
        state = out.state
        requests.append(out.requested_kw)
    # the reference moves 1 A per 2 s cycle (30 A/min), so the request falls to zero and stays there
    assert requests[0] == pytest.approx(min(PAC_PARAMS.kw_per_amp * 1.0, PAC_PARAMS.band_kw))
    assert requests[-1] == 0.0 and state.reference_a == pytest.approx(12.0)
    assert all(a >= b for a, b in pairwise(requests))


def test_pac_absorption_request_is_reported_not_served() -> None:
    out = pac.step(
        pac_inputs(fb(5.0, max_age_s=4.0)), PAC_PARAMS, pac.PipelineAcState(reference_a=10.0), dt_s=2.0
    )
    assert out.requested_kw < 0 and out.setpoint_kw == 0.0
    assert out.absorption_unserved_kw == -out.requested_kw


@pytest.mark.parametrize(
    "current", [None, fb(12.0, age_s=5.0, max_age_s=4.0), fb(12.0, quality="BAD", max_age_s=4.0)]
)
def test_pac_signal_loss_goes_neutral_and_resumes_without_a_step(current: FeedbackValue | None) -> None:
    params = pac.PipelineAcParams(
        line_kv=138.0,
        power_factor=0.98,
        shift_factor=0.2,
        direction=1,
        band_kw=500.0,
        freshness_s=4.0,
        release_ramp_kw_per_min=300.0,
    )
    lost = pac.step(
        pac_inputs(current), params, pac.PipelineAcState(reference_a=10.0, output_kw=100.0), dt_s=2.0
    )
    assert lost.mode is ControlMode.NEUTRAL and lost.setpoint_kw == pytest.approx(90.0)  # ramped toward 0
    assert lost.state.reference_a is None
    resumed = pac.step(pac_inputs(fb(30.0, max_age_s=4.0)), params, lost.state, dt_s=2.0)
    assert resumed.requested_kw == 0.0 and resumed.state.reference_a == 30.0


def test_pac_capacity_loss_limits_but_never_zeroes_a_usable_request() -> None:
    """Best effort after a SHORTFALL: only signal loss is neutral; lost capacity just caps the output."""
    state = pac.PipelineAcState(reference_a=10.0)
    short = pac.step(pac_inputs(fb(12.0, max_age_s=4.0), available_kw=60.0), PAC_PARAMS, state, dt_s=2.0)
    assert short.mode is ControlMode.TRACKING and short.setpoint_kw == 60.0


def test_pac_unknown_shift_factor_runs_the_open_loop_schedule() -> None:
    params = pac.PipelineAcParams(
        line_kv=138.0, power_factor=0.98, shift_factor=None, direction=1, band_kw=500, freshness_s=4
    )
    out = pac.step(pac_inputs(fb(12.0, max_age_s=4.0)), params, pac.PipelineAcState(), dt_s=2.0)
    assert out.mode is ControlMode.OPEN_LOOP and out.setpoint_kw == 120.0 and out.achieved_delta_a is None


@settings(max_examples=300)
@given(
    measured=st.one_of(st.none(), st.floats(0, 200)),
    reference=st.one_of(st.none(), st.floats(0, 200)),
    committed=st.floats(0, 2000),
    available=st.floats(-10, 2000),
    band=st.floats(0, 1000),
    scheduled=st.floats(-100, 3000),
    sf=st.one_of(st.none(), st.floats(0.01, 1.0)),
)
def test_pac_output_stays_inside_the_envelope(
    measured, reference, committed, available, band, scheduled, sf
) -> None:
    params = pac.PipelineAcParams(
        line_kv=138.0, power_factor=0.98, shift_factor=sf, direction=1, band_kw=band, freshness_s=4
    )
    current = None if measured is None else fb(measured, max_age_s=4.0)
    inputs = pac.PipelineAcInputs(
        current=current, scheduled_kw=scheduled, committed_kw=committed, available_kw=available
    )
    out = pac.step(inputs, params, pac.PipelineAcState(reference_a=reference), dt_s=2.0)
    assert 0.0 <= out.setpoint_kw <= max(min(committed, available, band), 0.0) + 1e-9


def test_pac_corridor_limit_is_the_tighter_one_and_feeds_the_ladder() -> None:
    reading = CorridorCurrentReading.model_validate(
        {
            "corridor_id": "c",
            "line_id": "l",
            "customer_id": "p",
            "ts": TS.isoformat(),
            "i_ac_a": 16.0,
            "limit_a": 15.0,
            "quality": "GOOD",
        }
    )
    envelope = PqEnvelopeLimits(1.5, 2.0, 0.5, 0.9, 5.0, 5.0, current_limit_a=20.0)
    limits = pac.corridor_limits(envelope, reading)
    assert limits.current_limit_a == 15.0
    ladder = LadderState()
    for k in range(2):  # the first observation starts the (zero) dwell, the second commits it
        result = monitor_pq(
            "ob-p", pac.corridor_measurement(reading), limits, ladder, float(k), hysteresis_config=FAST
        )
        ladder = result.ladder_state
    assert result.monitor is not None and result.monitor.worst_dimension == "current_a"
    assert result.monitor.verdict is ComplianceState.BREACH


# -- allocator-cycle caps and performance --------------------------------------------------------------


def test_caps_split_the_setpoint_by_committed_share_per_bank() -> None:
    calls = [
        ObligationCall("ob-1", "bank-1", "DATA_CENTER", "T1", 300.0, ()),
        ObligationCall("ob-1", "bank-2", "DATA_CENTER", "T1", 100.0, ()),
        ObligationCall("ob-2", "bank-1", "ERCOT_ENERGY", "T2", 50.0, ()),
    ]
    caps = closed_loop_caps(calls, {"ob-1": 200.0})
    assert caps == {("ob-1", "bank-1"): 150.0, ("ob-1", "bank-2"): 50.0}


def test_twenty_obligations_run_well_inside_five_ms_per_cycle() -> None:
    measurement = dc.pcc_measurement(site_reading(), nominal_v=277.0)
    dc_states = [dc.DataCenterState(100.0) for _ in range(10)]
    pac_states = [pac.PipelineAcState(reference_a=10.0) for _ in range(10)]
    ladders = [LadderState() for _ in range(20)]
    cycles = 200
    started = time.process_time()  # CPU time: the shared build host's load is not this code's cost
    for k in range(cycles):
        for i in range(10):
            dc_states[i] = dc.step(
                dc_inputs(fb(450.0 + i)), DC_PARAMS, dc_states[i], now_s=2.0 * k, dt_s=2.0
            ).state
            pac_states[i] = pac.step(
                pac_inputs(fb(10.0 + (k % 5), max_age_s=4.0)), PAC_PARAMS, pac_states[i], dt_s=2.0
            ).state
        for j in range(20):
            ladders[j] = monitor_pq(f"ob-{j}", measurement, DC_LIMITS, ladders[j], 2.0 * k).ladder_state
    per_cycle_ms = (time.process_time() - started) / cycles * 1000.0
    assert per_cycle_ms < 5.0
