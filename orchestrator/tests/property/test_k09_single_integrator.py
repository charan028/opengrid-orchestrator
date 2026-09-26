"""K9 one loop per quantity (00-invariants.md K9): the DIST_DEFERRAL PI is the only integrator on bank kVA."""

from __future__ import annotations

from dataclasses import replace

from hypothesis import given
from hypothesis import strategies as st

from opengrid.allocator.dist_deferral_pi import DistDeferralPI
from opengrid.allocator.models import BankSnapshot, PiState, ScadaSample
from opengrid.core.physics import BankParams, recharge_headroom

CYCLE_S = 2.0
RAMP_DOWN_KW_PER_S = 150.0 / 60.0
_EPSILON = 1e-6
_banks = st.builds(
    BankSnapshot,
    bank_id=st.just("bank-000"),
    capability_kw=st.just(500.0),
    kva_rating=st.floats(min_value=100.0, max_value=1000.0),
    reserve_kva=st.floats(min_value=0.0, max_value=20.0),
    tau_eff=st.floats(min_value=10.0, max_value=200.0),
)
_samples = st.builds(
    ScadaSample,
    bank_id=st.just("bank-000"),
    apparent_power_kva=st.floats(min_value=0.0, max_value=1500.0),
    reactive_power_kvar=st.floats(min_value=-300.0, max_value=300.0),
    forecast_reactive_kvar_next=st.floats(min_value=-300.0, max_value=300.0),
    error_kw=st.floats(min_value=-500.0, max_value=500.0),
    sigma_n=st.floats(min_value=0.0, max_value=50.0),
    sigma_x=st.floats(min_value=0.0, max_value=50.0),
    beta_x=st.floats(min_value=0.1, max_value=2.0),
)


def _cap(bank: BankSnapshot) -> float:
    return recharge_headroom(0.0, BankParams(kva_rating=bank.kva_rating, reserve_kva=bank.reserve_kva))


def _assert_step_is_bounded(pi: DistDeferralPI, prev: PiState, output: float, new: PiState) -> None:
    ramp_up_kw_per_s = max(150.0, pi.bank.kva_rating / 3.0) / 60.0
    assert abs(new.integral) <= pi.i_max + _EPSILON
    assert -_EPSILON <= output <= _cap(pi.bank) + _EPSILON
    assert output - prev.prev_output_kw <= ramp_up_kw_per_s * CYCLE_S + _EPSILON
    assert prev.prev_output_kw - output <= RAMP_DOWN_KW_PER_S * CYCLE_S + _EPSILON
    assert (new.state == "SCHEDULE") == (output > 1e-9)


@given(_banks, st.lists(_samples, min_size=1, max_size=60))
def test_k09_every_step_keeps_the_integrator_output_and_ramp_inside_their_bounds(bank, samples):
    pi, state = DistDeferralPI(bank), PiState()
    for sample in samples:
        prev = replace(state)
        output, state = pi.step(sample, state, CYCLE_S)
        _assert_step_is_bounded(pi, prev, output, state)


@given(_banks, _samples, st.integers(min_value=50, max_value=400))
def test_k09_a_sustained_error_never_winds_the_integrator_past_its_clamp(bank, sample, steps):
    pi, state = DistDeferralPI(bank), PiState()
    saturating = replace(sample, error_kw=500.0, apparent_power_kva=1500.0)
    for _ in range(steps):
        _output, state = pi.step(saturating, state, CYCLE_S)

    assert abs(state.integral) <= pi.i_max + _EPSILON


@given(
    _banks, _samples, st.floats(min_value=-50.0, max_value=50.0), st.floats(min_value=0.0, max_value=200.0)
)
def test_k09_a_step_is_pure_and_leaves_the_callers_state_untouched(bank, sample, integral, prev_output):
    pi = DistDeferralPI(bank)
    state = PiState(integral=integral, n_tilde=0.0, prev_output_kw=prev_output)
    before = replace(state)

    first = pi.step(sample, state, CYCLE_S)
    second = pi.step(sample, state, CYCLE_S)

    assert state == before
    assert first == second
