"""`DIST_DEFERRAL` PI controller on bank kVA (02a S5.4, K9): the ONE integrating controller that
regulates a given bank's apparent power. Every other loop treats bank kVA as feed-forward only.

Ported at MVP-S scale from `03` S8.6.1, `OUTCOME` performance basis only (02a S5.4's scope note).
One `DistDeferralPI` instance must be kept per bank across cycles (K9) -- the caller (the cycle
orchestrator) owns that lifetime; this class itself is pure state-in/state-out so it is trivial to
unit test without a clock or DB.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from opengrid.allocator.models import BankSnapshot, PiState, ScadaSample
from opengrid.core.physics import BankParams, apply_ramp_limit, recharge_headroom

_FEED_FORWARD_TAU_S = 45.0
_KP = 0.3  # class A1 SCADA gain (02a S5.4)
_I_MAX_FRACTION_OF_KC = 0.2
_RAMP_DOWN_KW_PER_MIN = 150.0
_RAMP_UP_MIN_KW_PER_MIN = 150.0
_SECONDS_PER_MINUTE = 60.0


def gross_need_kva(scada: ScadaSample, bank: BankSnapshot) -> float:
    """n_k = P^G_k - sqrt((S^lim_b)^2 - (Q^G_k - Qhat^F_{k+1})^2) (02a S5.4). `S^lim_b`, the bank's
    apparent-power ceiling, is `opengrid.core.physics.recharge_headroom` evaluated at zero existing
    load (ALLOC-03: never re-derive `kva_rating - reserve_kva` locally) -- the ONE formula the
    allocator and guardian G-03 both call (02b S12). "Two fixed-point iterations" (02a S5.4) describes
    how this closed-form expression was itself derived (solving the fixed point of the coupled P/Q
    apparent-power equation offline); it is evaluated once per call, not iterated at runtime. Falls
    back to the linear P-only need if the radicand goes negative (over-limit on reactive alone), which
    the ramp/clip stage downstream will still bound safely.
    """
    s_lim = recharge_headroom(0.0, BankParams(kva_rating=bank.kva_rating, reserve_kva=bank.reserve_kva))
    q_term = scada.reactive_power_kvar - scada.forecast_reactive_kvar_next
    radicand = s_lim * s_lim - q_term * q_term
    p_budget = math.sqrt(radicand) if radicand > 0 else 0.0
    p_gross = scada.apparent_power_kva  # MVP-S simplification: unity-PF gross real power proxy
    return max(p_gross - p_budget, 0.0)


def _deadband(error_kw: float, db_kw: float) -> float:
    if abs(error_kw) <= db_kw:
        return 0.0
    return error_kw - math.copysign(db_kw, error_kw)


@dataclass(frozen=True, slots=True)
class DistDeferralPI:
    """Stateless computation over an externally-owned `PiState` (K9: exactly one integrator per
    bank; the state object IS that integrator, kept alive by the caller across cycles).
    """

    bank: BankSnapshot

    @property
    def ki(self) -> float:
        tau_eff = max(self.bank.tau_eff, 1e-6)
        return min(0.02, math.pi / (6.0 * tau_eff))

    @property
    def i_max(self) -> float:
        return _I_MAX_FRACTION_OF_KC * max(self.bank.kva_rating, 0.0)

    def step(self, scada: ScadaSample, state: PiState, dt_c_s: float) -> tuple[float, PiState]:
        """Advance the PI loop by one 2 s tick. Returns (output_kw, new_state); never mutates the
        input `state` (callers are expected to hold onto and replace it, keeping the pure-logic
        core side-effect-free -- easy to hypothesis-test for stability/anti-windup).
        """
        n_k = gross_need_kva(scada, self.bank)

        # 02a S5.4's 3-sigma fast path: jump straight to n_k instead of low-pass filtering toward
        # it, so a large step disturbance isn't needlessly delayed by the feed-forward time constant.
        fast_path = abs(n_k - state.n_tilde) > 3 * scada.sigma_n
        n_tilde = n_k if fast_path else state.n_tilde + (dt_c_s / _FEED_FORWARD_TAU_S) * (n_k - state.n_tilde)

        db_kw = max(25.0, 2 * scada.sigma_x)
        e_db = _deadband(scada.error_kw, db_kw)
        beta_x = scada.beta_x if abs(scada.beta_x) > 1e-9 else 1.0

        u_raw = n_tilde + _KP * e_db / beta_x + state.integral
        cap = recharge_headroom(0.0, BankParams(kva_rating=self.bank.kva_rating, reserve_kva=self.bank.reserve_kva))
        u_sat = min(max(u_raw, 0.0), max(cap, 0.0))

        # Conditional-integration anti-windup: only integrate while not clipped by saturation.
        new_integral = state.integral
        if abs(u_raw - u_sat) < 1e-9:
            new_integral = _clip(state.integral + self.ki * dt_c_s * e_db / beta_x, -self.i_max, self.i_max)

        # ALLOC-03: the PI's asymmetric up/down ramp is `opengrid.core.physics.apply_ramp_limit`'s
        # `ramp_down_kw_per_s` extension (never a locally re-derived ramp clamp), converting the
        # loop's per-minute rates (02a S5.4) to the function's per-second units.
        ramp_up_kw_per_min = max(_RAMP_UP_MIN_KW_PER_MIN, self.bank.kva_rating / 3.0)
        output = apply_ramp_limit(
            state.prev_output_kw,
            u_sat,
            dt_c_s,
            ramp_up_kw_per_min / _SECONDS_PER_MINUTE,
            ramp_down_kw_per_s=_RAMP_DOWN_KW_PER_MIN / _SECONDS_PER_MINUTE,
        )

        new_state = PiState(
            integral=new_integral,
            n_tilde=n_tilde,
            prev_output_kw=output,
            state="SCHEDULE" if output > 1e-9 else "STANDBY",
        )
        return output, new_state


def _clip(value: float, lo: float, hi: float) -> float:
    return min(max(value, lo), hi)
