"""WP-D performance requirement (AGENT_BRIEF.md): PQ eligibility filtering and continuous PQ monitoring
must run inside the 2 s allocator cycle; p99 target < 500 ms at 2,000 hubs. Mirrors
`test_performance.py`'s best-of-N wall-clock methodology."""

from __future__ import annotations

import time

from opengrid.allocator.pq_eligibility import (
    EligibilityConfig,
    HubPqCandidate,
    apply_eligibility,
    evaluate_hub_eligibility,
)
from opengrid.allocator.pq_monitor import LadderState, evaluate_obligation_pq, rank_hubs_by_deviation
from opengrid.core.pq import PqEnvelopeLimits, PqMeasurement

_N_HUBS = 2000
_P99_BUDGET_MS = 500.0

_DATA_CENTER_CONFIG = EligibilityConfig(
    pq_aware_selection=True,
    min_quality_score=0.8,
    required_ride_through_class="CATEGORY_III",
    eligible_asset_states=frozenset({"OK", "WATCH"}),
    excluded_asset_states=frozenset({"DEGRADED", "QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"}),
    requested_p_kw=8.0,
    requested_q_kvar=1.0,
)

_LIMITS = PqEnvelopeLimits(
    max_phase_imbalance_pct=1.5,
    voltage_band_pct=2.0,
    freq_tolerance_hz=0.5,
    pf_min=0.95,
    thd_voltage_limit_pct=2.5,
    thd_current_limit_pct=2.5,
)


def _build_candidates(n: int) -> list[HubPqCandidate]:
    candidates = []
    for i in range(n):
        candidates.append(
            HubPqCandidate(
                hub_id=f"hub-{i:05d}",
                phase_connection=("A", "B", "C")[i % 3],
                kva_rating=11.0,
                pf_min_leading=0.95,
                pf_min_lagging=0.95,
                freq_offset_hz=0.001 * (i % 10),
                voltage_offset_pct=0.1 * (i % 5),
                thd_current_pct=0.5 + 0.05 * (i % 20),
                phase_angle_error_deg=0.5,
                quality_score=0.99 - 0.0001 * (i % 50),
                ride_through_class="CATEGORY_III",
                asset_state=("OK", "OK", "OK", "WATCH")[i % 4],
                dominant_harmonics={3: complex(1.0, 0.1 * (i % 7))},
            )
        )
    return candidates


def _best_of(fn: object, repeats: int = 5) -> float:
    fn()  # warm-up
    best_ms = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        fn()
        best_ms = min(best_ms, (time.perf_counter() - start) * 1000.0)
    return best_ms


def test_pq_eligibility_under_500ms_for_2000_hubs(record_property) -> None:
    candidates = _build_candidates(_N_HUBS)
    phase_by_hub = {c.hub_id: c.phase_connection for c in candidates}
    phase_kw = {"A": 4000.0, "B": 4100.0, "C": 3900.0}

    def _run() -> None:
        result = evaluate_hub_eligibility(candidates, _DATA_CENTER_CONFIG)
        apply_eligibility((), result, phase_by_hub_id=phase_by_hub, phase_kw_by_phase=phase_kw)

    elapsed_ms = _best_of(_run)
    record_property("pq_eligibility_ms_2000_hubs", elapsed_ms)
    assert elapsed_ms < _P99_BUDGET_MS


def test_pq_monitor_under_500ms_for_2000_obligations(record_property) -> None:
    measurement = PqMeasurement(
        imbalance_pct=0.5,
        voltage_deviation_pct=0.5,
        freq_deviation_hz=0.05,
        pf=0.98,
        thd_voltage_pct=1.0,
        thd_current_pct=1.0,
    )
    states = {f"ob-{i:05d}": LadderState() for i in range(_N_HUBS)}

    def _run() -> None:
        for obligation_id in states:
            evaluate_obligation_pq(obligation_id, measurement, _LIMITS, states[obligation_id], now_s=0.0)

    elapsed_ms = _best_of(_run)
    record_property("pq_monitor_ms_2000_obligations", elapsed_ms)
    assert elapsed_ms < _P99_BUDGET_MS


def test_rank_hubs_by_deviation_under_500ms_for_2000_hubs(record_property) -> None:
    candidates = _build_candidates(_N_HUBS)

    def _run() -> None:
        rank_hubs_by_deviation(candidates, _LIMITS)

    elapsed_ms = _best_of(_run)
    record_property("pq_rank_hubs_ms_2000_hubs", elapsed_ms)
    assert elapsed_ms < _P99_BUDGET_MS
