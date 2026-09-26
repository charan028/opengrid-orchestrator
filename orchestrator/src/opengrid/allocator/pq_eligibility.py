"""Allocator PQ eligibility filter and phase/diversity weighting (07-delivery/06-service-profiles-and-
power-quality.md S5.2 steps 1-2, S5.5.3's eligibility table; K14; WP-D new file).

**Scope (owner decision 2026-09-25, K13 unaffected).** PQ-aware selection acts on UNCOMMITTED headroom
only: this module filters/weights the CANDIDATE hub pool a NEW or re-planned obligation may draw from
(S5.2's "eligible hub set H_o passed into realize_obligation"). It never touches an already-committed
obligation's frozen floor or forces a change to its currently-assigned hubs -- continuous correction of
an in-flight delivery is `opengrid.allocator.pq_monitor` (S5.4), not this module.

Pure functions over plain dataclasses -- no I/O, no Postgres, no async (BUILD.md S5a "pure logic
separated from I/O"), mirroring `allocator/waterfill.py` and `allocator/substitution.py`. This file does
not edit `allocator/waterfill.py`, `allocator/substitution.py`, `allocator/cycle.py` or
`allocator/models.py` -- see this package's README for the exact call-site wiring the live-path agent
applies before `water_fill`/`realize_obligation`/`cycle()` run.

Every envelope/aggregation formula is imported from `opengrid.core.pq` (dupcheck-enforced single
implementation, BUILD.md S1) -- this module adds only the S5.2 filter predicates and the S5.2-step-2
diversity/phase-balance weighting on top of them, plus the S5.5.3 asset-state table.
"""

from __future__ import annotations

import cmath
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from dataclasses import replace as dataclasses_replace

from opengrid.allocator.models import HubSnapshot
from opengrid.core.pq import RIDE_THROUGH_RANK, KvaCircle, kva_point_feasible
from opengrid.core.pq.constants import ELIGIBILITY_K_SIGMA_DEFAULT
from opengrid.guardian.pq_ports import AssetState

_EPS = 1e-9

#: S5.5.3's table: excluded from ALL grid-facing dispatch, for every profile except HOME (HOME is out of
#: this filter's scope entirely -- callers never route a HOME obligation through this module, since HOME
#: is "always serves the homeowner" regardless of asset state). Enforced unconditionally as a defense-in-
#: depth floor, unioned with the profile's own `EligibilityConfig.excluded_asset_states` -- mirrors
#: `opengrid.guardian.pq_checks._ALWAYS_EXCLUDED_STATES` exactly (guardian's independent re-check of the
#: SAME floor, K2/K14 primary+independent shape, not a duplicated implementation of anything beyond a
#: 3-element constant set).
ALWAYS_EXCLUDED_ASSET_STATES: frozenset[AssetState] = frozenset(
    {"QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"}
)

# --- S5.2 step 1 reason codes (this module's own hard-filter verdicts; distinct from the guardian's
# G-2x reason strings in `guardian/pq_checks.py`, since this is the allocator's PRIMARY check and the
# guardian's is the INDEPENDENT one -- K2/K14 pattern, two separate reason namespaces by design). ---
PQ_ELIGIBILITY_ASSET_STATE_EXCLUDED = "PQ_ELIGIBILITY_ASSET_STATE_EXCLUDED"
PQ_ELIGIBILITY_RIDE_THROUGH_INSUFFICIENT = "PQ_ELIGIBILITY_RIDE_THROUGH_INSUFFICIENT"
PQ_ELIGIBILITY_RIDE_THROUGH_UNKNOWN = "PQ_ELIGIBILITY_RIDE_THROUGH_UNKNOWN"
PQ_ELIGIBILITY_QUALITY_SCORE_BELOW_MINIMUM = "PQ_ELIGIBILITY_QUALITY_SCORE_BELOW_MINIMUM"
PQ_ELIGIBILITY_OUTLIER_THD = "PQ_ELIGIBILITY_OUTLIER_THD"
PQ_ELIGIBILITY_OUTLIER_VOLTAGE_OFFSET = "PQ_ELIGIBILITY_OUTLIER_VOLTAGE_OFFSET"
PQ_ELIGIBILITY_KVA_INFEASIBLE = "PQ_ELIGIBILITY_KVA_INFEASIBLE"

#: S5.2 step 2's diversity/phase-balance weight is clipped to this range so a single outlier candidate
#: can never dominate (or be starved out of) `water_fill`'s weighted split -- `hub_weight()`'s own
#: stickiness bonus is a similarly bounded multiplicative nudge (+20%), so a PQ weight of the same order
#: of magnitude keeps the two composable without one swamping the other.
_MIN_DIVERSITY_WEIGHT = 0.25
_MAX_DIVERSITY_WEIGHT = 4.0
#: S5.5.3: "WATCH is eligible but deprioritized" -- a fixed penalty multiplier, not zero (WATCH is still
#: usable, just less preferred than OK).
_WATCH_WEIGHT_PENALTY = 0.5
#: S3.2(b)'s dominant harmonic orders (3rd, 5th, 7th -- non-triplen odd orders from PWM switching); the
#: 3rd is used as the single representative order for the diversity weight (a cheap, stable proxy -- a
#: full per-order weighted diversity score is not needed for a WATER-FILL WEIGHT, only for G-22's actual
#: compliance verdict, which already re-derives the full spectrum independently).
_DIVERSITY_HARMONIC_ORDER = 3


@dataclass(frozen=True, slots=True)
class HubPqCandidate:
    """One hub's PQ characterization for the S5.2 eligibility filter (`og.hub_inverter_pq` fields, S1.5/
    S3.1) plus its asset-health state (S5.5.2). The live-path agent builds these from its own fleet/
    asset-health gateway reads; this module never fetches them."""

    hub_id: str
    phase_connection: str  # "A" | "B" | "C" | "AB" | "BC" | "CA" | "ABC" (S1.5)
    kva_rating: float
    pf_min_leading: float
    pf_min_lagging: float
    freq_offset_hz: float
    voltage_offset_pct: float
    thd_current_pct: float
    phase_angle_error_deg: float
    quality_score: float
    ride_through_class: str
    asset_state: AssetState
    dominant_harmonics: Mapping[int, complex] | None = None


@dataclass(frozen=True, slots=True)
class EligibilityConfig:
    """The profile's own S5.2/S5.5.3 parameters -- mirrors `config/service_profiles/data_center.toml`'s
    `[eligibility]` section field-for-field. `pq_aware_selection=False` (S4.c's `ERCOT_ENERGY` arbitrage
    contrast case) skips the tight per-unit thresholds, the k-sigma outlier filter, the kVA-circle check
    and all diversity/phase weighting -- only the asset-state floor and ride-through conformance still
    apply (grid-code minimum, never fully unconstrained -- S5.5.3's table still excludes QUARANTINED/
    AWAITING_REPLACEMENT/RECOMMISSIONING and still requires the fleet-default ride-through class)."""

    pq_aware_selection: bool
    min_quality_score: float
    required_ride_through_class: str
    eligible_asset_states: frozenset[AssetState]
    excluded_asset_states: frozenset[AssetState]
    k_sigma: float = ELIGIBILITY_K_SIGMA_DEFAULT
    #: The candidate dispatch point checked against each hub's kVA circle (S3.1/S5.2 step 1's "hub's
    #: kVA-circle PF at the requested dispatch point is infeasible"). `None` skips the kVA check (no
    #: specific point requested yet, e.g. a coarse pre-filter ahead of water-filling).
    requested_p_kw: float | None = None
    requested_q_kvar: float = 0.0


@dataclass(frozen=True, slots=True)
class HubEligibilityVerdict:
    """One candidate's S5.2 step-1/step-2 outcome."""

    hub_id: str
    eligible: bool
    reason_code: str = ""
    #: S5.2 step 2's diversity/phase-balance multiplier for this hub's `HubSnapshot.tau` (1.0 = neutral).
    #: Meaningless when `eligible=False` (left at 1.0, never computed for an excluded hub).
    diversity_weight: float = 1.0


@dataclass(frozen=True, slots=True)
class EligibilityResult:
    verdicts: tuple[HubEligibilityVerdict, ...]

    @property
    def eligible_hub_ids(self) -> tuple[str, ...]:
        return tuple(v.hub_id for v in self.verdicts if v.eligible)

    @property
    def excluded(self) -> tuple[HubEligibilityVerdict, ...]:
        return tuple(v for v in self.verdicts if not v.eligible)


def evaluate_hub_eligibility(
    candidates: Sequence[HubPqCandidate], config: EligibilityConfig
) -> EligibilityResult:
    """S5.2 step 1 (hard filter) + step 2 (diversity/phase weighting), S5.5.3's asset-state table.

    Deterministic: candidates are processed in `hub_id` order and the k-sigma outlier threshold (step
    1's last hard filter) is computed once over the whole candidate set before any hub is excluded by
    it, so the result never depends on iteration order or on which hubs step 1's earlier filters already
    dropped.
    """
    ordered = sorted(candidates, key=lambda c: c.hub_id)
    if not ordered:
        return EligibilityResult(())

    thd_mean, thd_std = _mean_std([c.thd_current_pct for c in ordered])
    voltage_mean, voltage_std = _mean_std([c.voltage_offset_pct for c in ordered])

    verdicts: list[HubEligibilityVerdict] = []
    survivors: list[HubPqCandidate] = []
    for candidate in ordered:
        reason = _hard_filter_reason(candidate, config, thd_mean, thd_std, voltage_mean, voltage_std)
        if reason:
            verdicts.append(HubEligibilityVerdict(candidate.hub_id, eligible=False, reason_code=reason))
        else:
            survivors.append(candidate)

    weights = (
        _diversity_weights(survivors)
        if config.pq_aware_selection
        else dict.fromkeys((c.hub_id for c in survivors), 1.0)
    )
    for candidate in survivors:
        verdicts.append(
            HubEligibilityVerdict(candidate.hub_id, eligible=True, diversity_weight=weights[candidate.hub_id])
        )

    verdicts.sort(key=lambda v: v.hub_id)
    return EligibilityResult(tuple(verdicts))


def _hard_filter_reason(
    candidate: HubPqCandidate,
    config: EligibilityConfig,
    thd_mean: float,
    thd_std: float,
    voltage_mean: float,
    voltage_std: float,
) -> str:
    """S5.2 step 1's ordered predicate chain; the first applicable reason wins (deterministic, one
    reason per excluded hub). Returns `""` when the candidate is eligible."""
    excluded_states = ALWAYS_EXCLUDED_ASSET_STATES | config.excluded_asset_states
    if candidate.asset_state in excluded_states:
        return PQ_ELIGIBILITY_ASSET_STATE_EXCLUDED

    hub_rank = RIDE_THROUGH_RANK.get(candidate.ride_through_class)
    required_rank = RIDE_THROUGH_RANK.get(config.required_ride_through_class)
    if hub_rank is None or required_rank is None:
        return PQ_ELIGIBILITY_RIDE_THROUGH_UNKNOWN
    if hub_rank < required_rank:
        return PQ_ELIGIBILITY_RIDE_THROUGH_INSUFFICIENT

    if not config.pq_aware_selection:
        return ""

    if candidate.asset_state not in config.eligible_asset_states:
        return PQ_ELIGIBILITY_ASSET_STATE_EXCLUDED
    if candidate.quality_score < config.min_quality_score - _EPS:
        return PQ_ELIGIBILITY_QUALITY_SCORE_BELOW_MINIMUM
    if _is_outlier(candidate.thd_current_pct, thd_mean, thd_std, config.k_sigma):
        return PQ_ELIGIBILITY_OUTLIER_THD
    if _is_outlier(candidate.voltage_offset_pct, voltage_mean, voltage_std, config.k_sigma):
        return PQ_ELIGIBILITY_OUTLIER_VOLTAGE_OFFSET
    if config.requested_p_kw is not None:
        circle = KvaCircle(
            kva_rating=candidate.kva_rating,
            pf_min_lagging=candidate.pf_min_lagging,
            pf_min_leading=candidate.pf_min_leading,
        )
        if not kva_point_feasible(config.requested_p_kw, config.requested_q_kvar, circle):
            return PQ_ELIGIBILITY_KVA_INFEASIBLE
    return ""


def _mean_std(values: Sequence[float]) -> tuple[float, float]:
    if not values:
        return 0.0, 0.0
    mean = sum(values) / len(values)
    if len(values) < 2:
        return mean, 0.0
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    return mean, math.sqrt(variance)


def _is_outlier(value: float, mean: float, std: float, k_sigma: float) -> bool:
    """S5.2 step 1: "more than k-sigma above the bank's mean" -- one-sided (only an EXCESS of THD/
    voltage offset over the mean is a quality risk; a below-mean reading is never penalized). A
    degenerate `std=0` (a near-identical candidate set, e.g. one candidate, or a firmware-homogeneous
    batch with zero measured spread) never flags an outlier -- there is no "above the mean" without
    spread to measure it against."""
    if std <= _EPS:
        return False
    return (value - mean) / std > k_sigma


def _diversity_weights(survivors: Sequence[HubPqCandidate]) -> dict[str, float]:
    """S5.2 step 2: diversity (S3.2b harmonic phase spread) x phase-balance is folded into phase
    weighting by the caller via `phase_balance_weights` (this function only handles the harmonic-
    diversity term and the WATCH deprioritization, S5.5.3) -- phase-balance needs the bank's current
    per-phase loading, which this function does not have; see `apply_eligibility`'s docstring for how
    the two compose."""
    if not survivors:
        return {}
    mean_angle_deg = _mean_harmonic_angle_deg(survivors, _DIVERSITY_HARMONIC_ORDER)
    weights: dict[str, float] = {}
    for candidate in survivors:
        weight = _harmonic_diversity_weight(candidate, mean_angle_deg, _DIVERSITY_HARMONIC_ORDER)
        if candidate.asset_state == "WATCH":
            weight *= _WATCH_WEIGHT_PENALTY
        weights[candidate.hub_id] = _clip(weight, _MIN_DIVERSITY_WEIGHT, _MAX_DIVERSITY_WEIGHT)
    return weights


def _harmonic_phasor(candidate: HubPqCandidate, order: int) -> complex | None:
    if candidate.dominant_harmonics is None:
        return None
    return candidate.dominant_harmonics.get(order)


def _mean_harmonic_angle_deg(candidates: Sequence[HubPqCandidate], order: int) -> float | None:
    """The magnitude-weighted mean phasor angle of `order` across every candidate that reports one
    (S3.2b's vector-sum direction) -- the reference a single candidate's own angle is compared against
    to decide whether adding it would STACK (near the mean) or CANCEL (far from the mean)."""
    phasors = [p for c in candidates if (p := _harmonic_phasor(c, order)) is not None]
    if not phasors:
        return None
    resultant = complex(sum(phasors, start=0j))
    if abs(resultant) <= _EPS:
        return None
    return math.degrees(cmath.phase(resultant))


def _harmonic_diversity_weight(candidate: HubPqCandidate, mean_angle_deg: float | None, order: int) -> float:
    """S3.2(b)'s "prefer assigning inverters with different dominant_harmonics phase angles ... because
    it is the only lever that turns stacking into the sqrt(N) [cancellation] regime": a candidate whose
    own angle is close to the bank's current resultant angle would STACK (worst case, weight below
    neutral); one far from it (near 180 degrees away) would CANCEL (weight above neutral). No harmonic
    data on this candidate, or no established bank resultant yet, is neutral (1.0) -- this is a WATER-
    FILL preference weight, not a compliance verdict, so an unknown never excludes, it just doesn't bias.
    """
    phasor = _harmonic_phasor(candidate, order)
    if phasor is None or mean_angle_deg is None:
        return 1.0
    own_angle_deg = math.degrees(cmath.phase(phasor))
    delta = abs(_angle_diff_deg(own_angle_deg, mean_angle_deg))
    # delta in [0, 180]: 0 -> stacking (weight 0.5), 180 -> cancelling (weight 1.5).
    return 0.5 + delta / 180.0


def _angle_diff_deg(a: float, b: float) -> float:
    diff = (a - b) % 360.0
    return diff - 360.0 if diff > 180.0 else diff


def phase_balance_weights(
    survivors: Sequence[HubPqCandidate], phase_kw_by_phase: Mapping[str, float]
) -> dict[str, float]:
    """S3.2(c)/S5.2 step 2's per-phase minimization, expressed as a `HubSnapshot.tau` multiplier: a
    single-phase hub on an under-loaded phase gets a weight > 1 (preferred), one on an over-loaded phase
    gets a weight < 1 (deprioritized) -- "the allocator's only real-time lever is which already-
    connected hubs to dispatch and how much, not which phase a home is on" (S3.2c). Multi-phase hubs
    (`ABC`) and single-phase hubs when `phase_kw_by_phase` has fewer than 2 entries are neutral (1.0):
    there is nothing to balance against with only one phase reported this cycle.
    """
    if len(phase_kw_by_phase) < 2:
        return dict.fromkeys((c.hub_id for c in survivors), 1.0)
    total_kw = sum(phase_kw_by_phase.values())
    average_kw = total_kw / len(phase_kw_by_phase)
    weights: dict[str, float] = {}
    for candidate in survivors:
        if candidate.phase_connection not in phase_kw_by_phase:
            weights[candidate.hub_id] = 1.0
            continue
        this_phase_kw = phase_kw_by_phase[candidate.phase_connection]
        if this_phase_kw <= _EPS:
            weights[candidate.hub_id] = _MAX_DIVERSITY_WEIGHT if average_kw > _EPS else 1.0
            continue
        weights[candidate.hub_id] = _clip(
            average_kw / this_phase_kw, _MIN_DIVERSITY_WEIGHT, _MAX_DIVERSITY_WEIGHT
        )
    return weights


def _clip(value: float, low: float, high: float) -> float:
    return min(max(value, low), high)


def apply_eligibility(
    hubs: Sequence[HubSnapshot],
    result: EligibilityResult,
    *,
    phase_by_hub_id: Mapping[str, str] | None = None,
    phase_kw_by_phase: Mapping[str, float] | None = None,
) -> tuple[HubSnapshot, ...]:
    """Turns an `EligibilityResult` into the `HubSnapshot` set the live-path agent hands to
    `water_fill`/`realize_obligation`/`cycle()`: drops ineligible hubs, and scales each survivor's
    `tau` by its S5.2-step-2 diversity weight (composed with the phase-balance weight when
    `phase_by_hub_id`/`phase_kw_by_phase` are supplied -- both optional, since phase-balance needs the
    bank's current per-phase loading, which not every caller has to hand).

    This is the ONLY place this module touches `HubSnapshot` -- everything else works on
    `HubPqCandidate`/`HubEligibilityVerdict`, so wiring this in ahead of `cycle()` requires no change to
    `allocator/waterfill.py`, `allocator/substitution.py` or `allocator/cycle.py` (BUILD.md S4
    ownership): `hub.tau` is an existing, already-consumed `HubSnapshot` field.
    """
    weight_by_id = {v.hub_id: v.diversity_weight for v in result.verdicts if v.eligible}
    phase_weight_by_id: dict[str, float] = {}
    if phase_by_hub_id is not None and phase_kw_by_phase is not None:
        eligible_ids = set(weight_by_id)
        candidates_for_phase = [
            HubPqCandidate(
                hub_id=hub_id,
                phase_connection=phase,
                kva_rating=0.0,
                pf_min_leading=0.0,
                pf_min_lagging=0.0,
                freq_offset_hz=0.0,
                voltage_offset_pct=0.0,
                thd_current_pct=0.0,
                phase_angle_error_deg=0.0,
                quality_score=0.0,
                ride_through_class="",
                asset_state="OK",
            )
            for hub_id, phase in phase_by_hub_id.items()
            if hub_id in eligible_ids
        ]
        phase_weight_by_id = phase_balance_weights(candidates_for_phase, phase_kw_by_phase)

    eligible_ids = set(weight_by_id)
    survivors = [h for h in hubs if h.hub_id in eligible_ids]
    return tuple(
        dataclasses_replace(
            hub, tau=hub.tau * weight_by_id[hub.hub_id] * phase_weight_by_id.get(hub.hub_id, 1.0)
        )
        for hub in survivors
    )
