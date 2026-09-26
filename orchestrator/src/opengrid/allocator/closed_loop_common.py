"""Shared pieces of the per-customer closed-loop controllers (`closed_loop_data_center`,
`closed_loop_pipeline_ac`; 06-service-profiles-and-power-quality.md S4.a/S4.b, S5.4; K9, K13, K14).

- Need basis (owner decision 2026-09-26): for a `MEASURED_FEEDBACK` profile the commitment is a reserved
  maximum over the period and delivery follows the measured need. A controller's output for an obligation
  is always in `[0, min(committed_kw, available_kw)]` -- never above the reserved maximum (K13) or what the
  bank/hubs can deliver (K4). Delivering less because the measured need is less is not a reduction of the
  allocation; the guardian's G-19 accepts `R_GRANT_CLOSED_LOOP` after its own checks.
- Best effort after a mid-window SHORTFALL: the controllers do not look at obligation state. A shortfall
  shows up only as a lower `available_kw`, so the output is the maximum feasible (never forced to 0) and
  comes back as soon as capacity returns, within the profile's ramp. Only signal loss changes behaviour
  (DATA_CENTER holds then follows the schedule; PIPELINE_AC goes neutral per its profile).
- The allocator-cycle cap: the controller's per-obligation kW is split across that obligation's banks
  in proportion to each bank's committed share, as a cap on the obligation's grant there. Capacity the
  controller leaves unused this cycle is held idle, never handed to another obligation (K13).
- The PQ check at the point of common coupling feeds `opengrid.allocator.pq_monitor`'s S5.4 ladder
  unchanged; a missing or stale measurement is recorded as missing, never as compliant (S5.4).

Pure functions and frozen dataclasses: no I/O, injected clock.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from opengrid.allocator.models import ObligationCall
from opengrid.allocator.pq_monitor import (
    DEFAULT_MAX_BREACH_CYCLES,
    LadderState,
    MonitorResult,
    evaluate_obligation_pq,
)
from opengrid.allocator.reasons import R_GRANT_CLOSED_LOOP as R_GRANT_CLOSED_LOOP  # re-exported
from opengrid.core.pq import HysteresisConfig, PqEnvelopeLimits, PqMeasurement

_EPS = 1e-9
_DEFAULT_HYSTERESIS_CONFIG = HysteresisConfig()

#: S5.4: "a summary older than the freshness gate is treated as missing, not as compliant".
PQ_MEASUREMENT_MISSING = "PQ_MEASUREMENT_MISSING"
#: The contract's minimum power factor is not met at the point of common coupling. `core.pq`'s
#: compliance ratios carry no PF dimension, so this is reported beside the ladder verdict.
PQ_PF_BELOW_MIN = "PQ_PF_BELOW_MIN"


class ControlMode(StrEnum):
    TRACKING = "TRACKING"  # closed loop on a usable measured signal
    HOLD = "HOLD"  # signal lost: holding the delivered level (HOLD_THEN_SCHEDULE)
    SCHEDULE = "SCHEDULE"  # signal lost past the hold time, or no loop target: the schedule
    NEUTRAL = "NEUTRAL"  # signal lost: band to 0 kW (NEUTRAL_ON_SIGNAL_LOSS)
    OPEN_LOOP = "OPEN_LOOP"  # PIPELINE_AC with an unknown shift factor: the customer's kW schedule


def envelope_ceiling_kw(committed_kw: float, available_kw: float, *extra_caps_kw: float) -> float:
    """The highest kW a controller may ask for this cycle: the commitment (K13), the deliverable
    capacity (K4) and any further cap (e.g. PIPELINE_AC's band B), never negative."""
    return max(min(committed_kw, available_kw, *extra_caps_kw), 0.0)


def clamp(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def closed_loop_caps(
    calls: Iterable[ObligationCall], setpoint_kw_by_obligation: dict[str, float]
) -> dict[tuple[str, str], float]:
    """Per (obligation_id, bank_id) caps for `allocator.cycle`: each obligation's controller setpoint
    split across its banks in proportion to the committed kW there (a bank with nothing committed gets
    nothing). Obligations without a setpoint are not capped."""
    committed_by_obligation: dict[str, float] = {}
    relevant = [c for c in calls if c.obligation_id in setpoint_kw_by_obligation]
    for call in relevant:
        committed_by_obligation[call.obligation_id] = committed_by_obligation.get(
            call.obligation_id, 0.0
        ) + max(call.committed_kw, 0.0)
    caps: dict[tuple[str, str], float] = {}
    for call in relevant:
        total = committed_by_obligation[call.obligation_id]
        share = max(call.committed_kw, 0.0) / total if total > _EPS else 0.0
        caps[(call.obligation_id, call.bank_id)] = (
            max(setpoint_kw_by_obligation[call.obligation_id], 0.0) * share
        )
    return caps


@dataclass(frozen=True, slots=True)
class PqCheckResult:
    """One cycle's PCC power-quality check for one obligation. `monitor` is `pq_monitor`'s verdict and
    ladder proposal, `None` when the measurement was missing/stale (then `ladder_state` is carried over
    unchanged and `flags` holds `PQ_MEASUREMENT_MISSING`)."""

    obligation_id: str
    ladder_state: LadderState
    monitor: MonitorResult | None
    flags: tuple[str, ...] = ()


def monitor_pq(
    obligation_id: str,
    measurement: PqMeasurement | None,
    limits: PqEnvelopeLimits,
    ladder_state: LadderState,
    now_s: float,
    *,
    has_substitution_candidate: bool = False,
    recalibration_eligible: bool = False,
    max_breach_cycles: int = DEFAULT_MAX_BREACH_CYCLES,
    hysteresis_config: HysteresisConfig = _DEFAULT_HYSTERESIS_CONFIG,
) -> PqCheckResult:
    """Feed one PCC measurement to `pq_monitor.evaluate_obligation_pq` (the S5.4 ladder, reused as is).
    PF below the envelope's `pf_min` is flagged beside it."""
    if measurement is None:
        return PqCheckResult(obligation_id, ladder_state, None, (PQ_MEASUREMENT_MISSING,))
    result = evaluate_obligation_pq(
        obligation_id,
        measurement,
        limits,
        ladder_state,
        now_s,
        has_substitution_candidate=has_substitution_candidate,
        recalibration_eligible=recalibration_eligible,
        max_breach_cycles=max_breach_cycles,
        hysteresis_config=hysteresis_config,
    )
    flags = (PQ_PF_BELOW_MIN,) if abs(measurement.pf) < limits.pf_min else ()
    return PqCheckResult(obligation_id, result.ladder_state, result, flags)
