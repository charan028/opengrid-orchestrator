"""Continuous PQ monitoring and the corrective-action ladder on COMMITTED obligations (07-delivery/06-
service-profiles-and-power-quality.md S5.4; K14; WP-D new file).

**Scope (owner decision 2026-09-25, K13 unaffected).** Selection-time eligibility
(`opengrid.allocator.pq_eligibility`) only governs which hubs a NEW grant may draw from. This module
watches an already-`COMMITTED`/`DELIVERING` obligation's MEASURED, DELIVERED power quality every cycle
and, on a sustained envelope deviation, proposes the S5.4 corrective-action ladder in order:

    1. REBALANCE_PHASES  -- within the obligation's already-assigned hubs, no new hubs, no committed-kW
                             change (S3.2c minimization).
    2. SUBSTITUTE_HUBS    -- swap a drifting hub for an eligible spare from the SAME obligation's S5.2-
                             eligible pool, preserving committed kW (K13's R-SUBSTITUTION exception).
    3. RECALIBRATE        -- NOT decided or executed by this module. `opengrid.assets.service.
                             AssetHealthService.request_calibration` / guardian G-25 own the remote-
                             recalibration ladder step; this module only emits a marker action so the
                             live-path agent's wiring can hand off to that service in the right ORDER
                             (after step 2 has cleared the hub of any committed PQ-sensitive delivery,
                             S5.5.4's "never on a live PQ-sensitive delivery" rule) and react to the
                             outcome -- "the allocator only invokes it and reacts" (S9.2, Agent D).
    4. ADJUST_PF          -- reactive/PF setpoint correction within the kVA circle (S3.1), before
                             touching real-power delivery.
    5. EXCLUDE_HUB        -- drop the drifting unit(s) from this obligation's active set entirely, then
                             re-run 1/2/4 on the reduced set (the caller's own next-cycle loop, since
                             this module only proposes ONE cycle's ladder, not a multi-step simulation).
    6. ESCALATE_AT_RISK   -- if 1-5 have not restored compliance within `max_breach_cycles` (default 3)
                             cycles, mark the obligation AT_RISK (reason `R-PQ-DRIFT-AT-RISK`) -- never a
                             silent breach.

Every step is proposed, never executed, by this module (K10 "trace before act": the live-path agent
traces each `CorrectionAction` before attempting it, and any resulting command batch still passes
G-21..G-25 independently -- "the ladder generates requests, it never bypasses the independent check").

Pure functions/dataclasses over `opengrid.core.pq`'s hysteresis and envelope math -- no I/O, no
Postgres, no async, no re-derivation of `evaluate_envelope`/`step_hysteresis` (BUILD.md S1/S5a). This
file does not edit `allocator/cycle.py`, `allocator/substitution.py` or `allocator/models.py` -- see
this package's README for the exact call-site wiring.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from opengrid.allocator import reasons
from opengrid.allocator.pq_eligibility import HubPqCandidate
from opengrid.core.pq import (
    ComplianceState,
    HysteresisConfig,
    HysteresisState,
    PqEnvelopeLimits,
    PqMeasurement,
    compliance_ratios,
    step_hysteresis,
)

#: S5.4 step 6's exact reason code (spec-mandated string). Not yet in `opengrid.core.reasons` (ALLOC-04's
#: canonical registry) -- reported in this package's build notes as an action item for the architect to
#: add there and re-export via `opengrid.allocator.reasons`, exactly like `R_SUBSTITUTION` today; defined
#: locally here in the meantime rather than blocking on an edit outside this module's owned paths.
R_PQ_DRIFT_AT_RISK = "R-PQ-DRIFT-AT-RISK"

#: S5.4's WARN-state alert reason (raises an alert, "does not yet act").
PQ_MONITOR_WARN_ALERT = "PQ_MONITOR_WARN_ALERT"
PQ_MONITOR_REBALANCE_PHASES = "PQ_MONITOR_REBALANCE_PHASES"
PQ_MONITOR_RECALIBRATION_HANDOFF = "PQ_MONITOR_RECALIBRATION_HANDOFF"
PQ_MONITOR_ADJUST_PF = "PQ_MONITOR_ADJUST_PF"
PQ_MONITOR_EXCLUDE_HUB = "PQ_MONITOR_EXCLUDE_HUB"

#: S5.4: "if no combination of 1-5 restores compliance within 3 cycles (default, per-contract
#: configurable)".
DEFAULT_MAX_BREACH_CYCLES = 3

#: Module-level singleton default (ruff B008: never call a constructor in a parameter default) --
#: `HysteresisConfig` is frozen/immutable, so sharing one instance across calls is safe.
_DEFAULT_HYSTERESIS_CONFIG = HysteresisConfig()


class CorrectionActionType(StrEnum):
    """The S5.4 ladder's six steps, as a closed enum (step 3 included only as a hand-off marker, see
    this module's docstring)."""

    ALERT = "ALERT"
    REBALANCE_PHASES = "REBALANCE_PHASES"
    SUBSTITUTE_HUBS = "SUBSTITUTE_HUBS"
    RECALIBRATE = "RECALIBRATE"
    ADJUST_PF = "ADJUST_PF"
    EXCLUDE_HUB = "EXCLUDE_HUB"
    ESCALATE_AT_RISK = "ESCALATE_AT_RISK"


@dataclass(frozen=True, slots=True)
class CorrectionAction:
    """One proposed ladder step for one obligation, in the order the live-path agent should attempt
    them this cycle (K10: traced before act)."""

    action: CorrectionActionType
    reason_code: str
    #: The hub this action targets (SUBSTITUTE_HUBS's "from" hub, EXCLUDE_HUB's target, RECALIBRATE's
    #: candidate) -- `None` for actions that are obligation-scoped, not hub-scoped (ALERT,
    #: REBALANCE_PHASES's own targets are the obligation's whole assigned set, ESCALATE_AT_RISK).
    target_hub_id: str | None = None
    detail: str = ""


@dataclass(frozen=True, slots=True)
class LadderState:
    """Per-obligation state the caller carries across cycles (mirrors `allocator.models.PiState`/
    `DwellState`'s "one instance per bank, mutated by the caller" pattern -- kept immutable/frozen here
    since `evaluate_obligation_pq` returns a fresh one each call rather than mutating in place)."""

    hysteresis: HysteresisState = field(default_factory=HysteresisState)
    cycles_in_breach: int = 0
    at_risk: bool = False


@dataclass(frozen=True, slots=True)
class MonitorResult:
    obligation_id: str
    ladder_state: LadderState
    verdict: ComplianceState
    worst_dimension: str | None
    actions: tuple[CorrectionAction, ...] = ()


def evaluate_obligation_pq(
    obligation_id: str,
    measurement: PqMeasurement,
    limits: PqEnvelopeLimits,
    ladder_state: LadderState,
    now_s: float,
    *,
    has_substitution_candidate: bool = False,
    recalibration_eligible: bool = False,
    max_breach_cycles: int = DEFAULT_MAX_BREACH_CYCLES,
    hysteresis_config: HysteresisConfig = _DEFAULT_HYSTERESIS_CONFIG,
) -> MonitorResult:
    """S5.4: advance `obligation_id`'s hysteresis state by one measured observation and propose this
    cycle's ladder actions.

    The scalar fed to `step_hysteresis` is the WORST (maximum) per-dimension ratio from
    `compliance_ratios` -- a BREACH on ANY monitored dimension (imbalance, voltage, frequency, THD_V,
    THD_I, current) must trigger the ladder, so a single hysteresis state machine over "the worst
    dimension this cycle" is the correct scalar (per-dimension detail is still returned via
    `worst_dimension` for the trace/UI, mirroring `core.pq.evaluate_envelope`'s own per-dimension +
    `worst_verdict` split).

    `has_substitution_candidate`/`recalibration_eligible` are pre-resolved by the caller (from
    `opengrid.allocator.pq_eligibility`'s own eligible pool and from `opengrid.assets`'s asset-state/
    sensitive-grant reads respectively) -- this function never re-derives either, it only decides
    whether to INCLUDE the corresponding ladder step this cycle.
    """
    ratios = compliance_ratios(measurement, limits)
    worst_dimension, worst_ratio = _worst_ratio(ratios)
    new_hysteresis = step_hysteresis(ladder_state.hysteresis, worst_ratio, now_s, hysteresis_config)

    if new_hysteresis.verdict == ComplianceState.NOMINAL:
        return MonitorResult(
            obligation_id=obligation_id,
            ladder_state=LadderState(hysteresis=new_hysteresis, cycles_in_breach=0, at_risk=False),
            verdict=ComplianceState.NOMINAL,
            worst_dimension=None,
        )

    if new_hysteresis.verdict == ComplianceState.WARN:
        return MonitorResult(
            obligation_id=obligation_id,
            ladder_state=LadderState(hysteresis=new_hysteresis, cycles_in_breach=0, at_risk=False),
            verdict=ComplianceState.WARN,
            worst_dimension=worst_dimension,
            actions=(CorrectionAction(CorrectionActionType.ALERT, PQ_MONITOR_WARN_ALERT),),
        )

    # BREACH: propose the ladder, in order, and track how many consecutive cycles it has failed to
    # restore compliance (S5.4's "within 3 cycles (default)").
    cycles_in_breach = ladder_state.cycles_in_breach + 1
    escalate = cycles_in_breach >= max_breach_cycles
    actions = list(
        _breach_ladder(
            reason_dimension=worst_dimension,
            has_substitution_candidate=has_substitution_candidate,
            recalibration_eligible=recalibration_eligible,
        )
    )
    if escalate:
        actions.append(CorrectionAction(CorrectionActionType.ESCALATE_AT_RISK, R_PQ_DRIFT_AT_RISK))

    return MonitorResult(
        obligation_id=obligation_id,
        ladder_state=LadderState(
            hysteresis=new_hysteresis, cycles_in_breach=cycles_in_breach, at_risk=escalate
        ),
        verdict=ComplianceState.BREACH,
        worst_dimension=worst_dimension,
        actions=tuple(actions),
    )


def _worst_ratio(ratios: dict[str, float]) -> tuple[str | None, float]:
    if not ratios:
        return None, 0.0
    dimension = max(ratios, key=lambda d: ratios[d])
    return dimension, ratios[dimension]


def _breach_ladder(
    *, reason_dimension: str | None, has_substitution_candidate: bool, recalibration_eligible: bool
) -> Sequence[CorrectionAction]:
    detail = reason_dimension or ""
    actions = [
        CorrectionAction(CorrectionActionType.REBALANCE_PHASES, PQ_MONITOR_REBALANCE_PHASES, detail=detail)
    ]
    if has_substitution_candidate:
        actions.append(
            CorrectionAction(CorrectionActionType.SUBSTITUTE_HUBS, reasons.R_SUBSTITUTION, detail=detail)
        )
    if recalibration_eligible:
        actions.append(
            CorrectionAction(
                CorrectionActionType.RECALIBRATE, PQ_MONITOR_RECALIBRATION_HANDOFF, detail=detail
            )
        )
    actions.append(CorrectionAction(CorrectionActionType.ADJUST_PF, PQ_MONITOR_ADJUST_PF, detail=detail))
    actions.append(CorrectionAction(CorrectionActionType.EXCLUDE_HUB, PQ_MONITOR_EXCLUDE_HUB, detail=detail))
    return actions


def rank_hubs_by_deviation(assigned: Sequence[HubPqCandidate], limits: PqEnvelopeLimits) -> tuple[str, ...]:
    """Ranks an obligation's currently-assigned hubs from most to least likely to be driving an
    aggregate BREACH, most-drifting first -- S5.4's ladder needs a CONCRETE hub for SUBSTITUTE_HUBS'
    "from" side and EXCLUDE_HUB's target, not just the obligation-level verdict `evaluate_obligation_pq`
    produces. Pure heuristic: each hub's own THD/voltage/frequency deviation is normalized against the
    SAME customer envelope `evaluate_obligation_pq` used (not S5.1's generic quality-score reference
    scales, which are fleet-wide defaults, not this customer's contracted limits), and hubs are ranked
    by their own worst normalized dimension. Ties break on `hub_id` for a deterministic result.
    """
    if not assigned:
        return ()

    def _worst_normalized(candidate: HubPqCandidate) -> tuple[float, str]:
        normalized = {
            "thd_current_pct": _ratio(candidate.thd_current_pct, limits.thd_current_limit_pct),
            "voltage_offset_pct": _ratio(candidate.voltage_offset_pct, limits.voltage_band_pct),
            "freq_offset_hz": _ratio(candidate.freq_offset_hz, limits.freq_tolerance_hz),
        }
        worst = max(normalized.values())
        return -worst, candidate.hub_id  # negate: sort ascending puts the WORST (highest ratio) first

    ranked = sorted(assigned, key=_worst_normalized)
    return tuple(c.hub_id for c in ranked)


def _ratio(value: float, limit: float) -> float:
    if limit <= 0.0:
        return 0.0 if value <= 0.0 else float("inf")
    return abs(value) / limit
