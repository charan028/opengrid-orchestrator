"""Cross-inverter aggregation formulas (07-delivery/06 S3.2): random-offset averaging, harmonic
vector summation with phase diversity, per-phase imbalance from a single-phase home mix, and the
aggregate kVA/PQ capability circle. Pure math -- the pre-delivery planning/forecast cross-check (S3)
and the measured pipeline (S6.5 step 3) both call these, on characterized vs. measured inputs
respectively; there is exactly one implementation either way.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

import numpy as np

from opengrid.core.pq.constants import (
    QUALITY_SCORE_EQUAL_WEIGHT,
    QUALITY_SCORE_FREQ_REF_HZ,
    QUALITY_SCORE_PHASE_REF_DEG,
    QUALITY_SCORE_THD_REF_PCT,
    QUALITY_SCORE_VOLTAGE_REF_PCT,
)
from opengrid.core.pq.types import KvaCircle
from opengrid.core.pq.waveform import phase_unbalance_pct


def aggregate_offset_std(sigma: float, n: int) -> float:
    """S3.2(a): the aggregate RANDOM component of an independently-drawn per-unit offset (frequency
    or voltage bias) shrinks as sigma/sqrt(N). A systematic bias `mu` does NOT shrink with fleet size
    and must be tracked/returned separately by the caller -- it is never averaged away here."""
    if n <= 0:
        raise ValueError("aggregate_offset_std() requires n >= 1")
    return sigma / math.sqrt(n)


def harmonic_vector_sum(phasors: Sequence[complex]) -> complex:
    """S3.2(b): the bank's k-th harmonic phasor is the VECTOR sum of member phasors, not the scalar
    sum -- identical angles (firmware-locked PWM carrier) stack linearly (worst case, THD does not
    fall with fleet size); i.i.d. uniform angles (diverse firmware/switching) grow only as sqrt(N),
    improving aggregate THD with fleet size."""
    return complex(sum(phasors, start=0j))


def bank_thd_current_pct(
    per_hub_harmonics: Sequence[Mapping[int, complex]],
    per_hub_fundamental_a: Sequence[float],
) -> float:
    """S3.2(b) applied across the full spectrum: vector-sum each harmonic order across hubs, then
    combine into a bank-level THD_I using the bank's summed fundamental current as the base -- S4.a's
    "measured, waveform-derived per-hub harmonic summaries ... vector-summed exactly per S3.2(b)"."""
    if len(per_hub_harmonics) != len(per_hub_fundamental_a):
        raise ValueError("per_hub_harmonics and per_hub_fundamental_a must be the same length")
    if not per_hub_harmonics:
        raise ValueError("bank_thd_current_pct() requires at least one hub")
    bank_fundamental_a = float(np.sum(per_hub_fundamental_a))
    if bank_fundamental_a <= 0.0:
        raise ValueError("bank_thd_current_pct() requires a positive bank fundamental current")
    orders: set[int] = set()
    for spectrum in per_hub_harmonics:
        orders.update(spectrum.keys())
    sum_sq = 0.0
    for order in orders:
        phasors = [spectrum.get(order, 0j) for spectrum in per_hub_harmonics]
        sum_sq += abs(harmonic_vector_sum(phasors)) ** 2
    return float(math.sqrt(sum_sq) / bank_fundamental_a * 100.0)


def per_phase_imbalance_pct(phase_currents: Mapping[str, float]) -> float:
    """S3.2(c): NEMA/IEEE-style current imbalance from a single-phase home mix on a 3-phase bank.
    Named wrapper over the shared `phase_unbalance_pct` formula (S2's definition) so callers reading
    S3.2(c) find it under its own name -- there is still exactly one implementation."""
    return phase_unbalance_pct(phase_currents)


def aggregate_kva_circle(
    member_kva_ratings: Sequence[float],
    member_pf_min_lagging: Sequence[float],
    member_pf_min_leading: Sequence[float],
) -> KvaCircle:
    """S3.2(d): the bank's deliverable kVA circle, approximated -- per S3.2(d)'s stated conservative
    bound, used at admission rather than an exact LP evaluation every cycle -- as the SUM of member
    kVA ratings with an effective PF limit equal to the TIGHTEST (highest) member limit."""
    if not member_kva_ratings:
        raise ValueError("aggregate_kva_circle() requires at least one member")
    return KvaCircle(
        kva_rating=float(np.sum(member_kva_ratings)),
        pf_min_lagging=float(np.max(member_pf_min_lagging)),
        pf_min_leading=float(np.max(member_pf_min_leading)),
    )


def kva_point_feasible(p_kw: float, q_kvar: float, circle: KvaCircle) -> bool:
    """S3.1/S5.2 step 1: is (P, Q) inside the aggregate kVA circle at the applicable direction's PF
    limit? A real inverter cannot deliver rated kW and rated kVAR simultaneously -- the allocator must
    never request a point outside this circle."""
    kva = math.hypot(p_kw, q_kvar)
    if kva > circle.kva_rating + 1e-9:
        return False
    if kva <= 0.0:
        return True
    pf = abs(p_kw) / kva
    pf_limit = circle.pf_min_lagging if q_kvar >= 0.0 else circle.pf_min_leading
    return pf >= pf_limit - 1e-9


def inverter_quality_score(
    freq_offset_hz: float,
    voltage_offset_pct: float,
    thd_current_pct: float,
    phase_angle_error_deg: float,
    *,
    weights: tuple[float, float, float, float] = (
        QUALITY_SCORE_EQUAL_WEIGHT,
        QUALITY_SCORE_EQUAL_WEIGHT,
        QUALITY_SCORE_EQUAL_WEIGHT,
        QUALITY_SCORE_EQUAL_WEIGHT,
    ),
) -> float:
    """S5.1's scalar fast-filtering quality score (a tuning parameter, not a physical constant),
    clipped to [0, 1]. Not a substitute for the constraint checks in `envelope.py`."""
    w1, w2, w3, w4 = weights
    score = (
        1.0
        - w1 * abs(freq_offset_hz) / QUALITY_SCORE_FREQ_REF_HZ
        - w2 * voltage_offset_pct / QUALITY_SCORE_VOLTAGE_REF_PCT
        - w3 * thd_current_pct / QUALITY_SCORE_THD_REF_PCT
        - w4 * abs(phase_angle_error_deg) / QUALITY_SCORE_PHASE_REF_DEG
    )
    return float(min(max(score, 0.0), 1.0))
