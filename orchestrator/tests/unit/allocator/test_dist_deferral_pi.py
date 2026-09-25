"""TS-05: DIST_DEFERRAL PI controller on bank kVA (02a S5.4, K9). Stability, anti-windup, ramp."""

from __future__ import annotations

from itertools import pairwise

from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.allocator.dist_deferral_pi import DistDeferralPI, gross_need_kva
from opengrid.allocator.models import BankSnapshot, PiState, ScadaSample

_BANK = BankSnapshot(bank_id="b1", capability_kw=500.0, kva_rating=1000.0, reserve_kva=50.0, tau_eff=60.0)


def _sample(kva: float, error_kw: float = 0.0) -> ScadaSample:
    return ScadaSample(bank_id="b1", apparent_power_kva=kva, error_kw=error_kw, sigma_n=1.0, sigma_x=1.0)


def test_ts_05_30_output_never_exceeds_bank_kva_capability() -> None:
    pi = DistDeferralPI(_BANK)
    state = PiState()
    for _ in range(50):
        output, state = pi.step(_sample(10_000.0, error_kw=5000.0), state, dt_c_s=2.0)
        assert 0.0 <= output <= _BANK.kva_rating - _BANK.reserve_kva + 1e-6


def test_ts_05_31_ramp_limited_step_response() -> None:
    """A large step disturbance must not move the output faster than the configured ramp."""
    pi = DistDeferralPI(_BANK)
    state = PiState()
    outputs = []
    for _ in range(5):
        output, state = pi.step(_sample(10_000.0, error_kw=10_000.0), state, dt_c_s=2.0)
        outputs.append(output)
    max_up_per_tick = max(150.0, _BANK.kva_rating / 3.0) * (2.0 / 60.0)
    for prev, cur in pairwise(outputs):
        assert cur - prev <= max_up_per_tick + 1e-6


def test_ts_05_32_anti_windup_integral_bounded() -> None:
    pi = DistDeferralPI(_BANK)
    state = PiState()
    for _ in range(500):
        _, state = pi.step(_sample(50_000.0, error_kw=50_000.0), state, dt_c_s=2.0)
        assert abs(state.integral) <= pi.i_max + 1e-6


def test_ts_05_33_zero_error_converges_to_zero_output() -> None:
    pi = DistDeferralPI(_BANK)
    state = PiState()
    for _ in range(200):
        output, state = pi.step(_sample(0.0, error_kw=0.0), state, dt_c_s=2.0)
    assert abs(output) < 1e-3
    assert abs(state.integral) < 1e-3


def test_ts_05_34_gross_need_kva_never_negative() -> None:
    bank = BankSnapshot(bank_id="b1", capability_kw=500.0, kva_rating=100.0, reserve_kva=0.0)
    sample = ScadaSample(bank_id="b1", apparent_power_kva=50.0, reactive_power_kvar=200.0)
    assert gross_need_kva(sample, bank) >= 0.0


@given(
    steps=st.integers(min_value=1, max_value=200),
    disturbance_kva=st.floats(min_value=-5000, max_value=5000, allow_nan=False),
)
@settings(max_examples=50)
def test_ts_05_35_property_pi_stable_and_bounded_on_step_disturbance(
    steps: int, disturbance_kva: float
) -> None:
    """K9: a single PI loop, step disturbance never drives the output outside the bank's own
    envelope or leaves the integral unbounded (no windup)."""
    pi = DistDeferralPI(_BANK)
    state = PiState()
    for _ in range(steps):
        output, state = pi.step(_sample(disturbance_kva, error_kw=disturbance_kva), state, dt_c_s=2.0)
        assert -1e-6 <= output <= _BANK.kva_rating - _BANK.reserve_kva + 1e-6
        assert abs(state.integral) <= pi.i_max + 1e-6
