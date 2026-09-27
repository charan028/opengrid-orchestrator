# ruff: noqa
# mypy: ignore-errors
"""ORACLE -- do not edit. A verbatim copy of `opengrid/allocator/cycle.py` as of main b3e01fe (before the
r3.4.3 PERF-OPT work), the reference the perf-optimized cycle must match byte for byte
(`test_alloc_perf_equivalence.py`). Only this docstring differs from the original file."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import replace as dataclasses_replace
from datetime import datetime

from opengrid.allocator import reasons
from opengrid.allocator.dist_deferral_pi import DistDeferralPI
from opengrid.allocator.energy_hold import headroom_energy_cap_kw
from opengrid.allocator.flow_limits import (
    apply_group_caps,
    bank_budget_caps_kw,
    cap_hub,
    consume_group_budget,
    with_topology,
    xfmr_budgets_kw,
)
from opengrid.allocator.lexicographic import allocate_tiers
from opengrid.allocator.models import (
    BankSnapshot,
    CycleResult,
    DwellState,
    FleetState,
    FlowLimits,
    HubAllocation,
    HubSnapshot,
    Instruction,
    LedgerView,
    ObligationCall,
    PiState,
    PqCapabilityReduction,
    PqDispatchContext,
    ProposedGrant,
    ScadaSample,
    Schedule,
    ShortfallReport,
    SubstitutionEvent,
    TerritoryBlock,
)
from opengrid.allocator.pq_eligibility import EligibilityResult, HubEligibilityVerdict, apply_eligibility
from opengrid.allocator.price_response import price_responsive_schedule
from opengrid.allocator.substitution import realize_obligation
from opengrid.core.physics import hub_sustainable_discharge_kw
from opengrid.core.services import DIST_DEFERRAL_SERVICE_TYPE
from opengrid.market.territory import FREE, check_territory

_EPS = 1e-9
_NO_FLOW_LIMITS = FlowLimits()
#: Single-phase connections the phase-balance weight balances across (S3.2c).
_SINGLE_PHASES = ("A", "B", "C")
# 02a S5's "hold horizon (the lease TTL, not 2 s)": how long a discharge grant must be SUSTAINABLE for,
# not merely instantaneously safe -- K7's hold-the-last-setpoint-until-lease-expiry duration (00-
# invariants.md K7: "30 s during events, 60 s otherwise"). The conservative (shorter) default is used
# unless the caller knows the actual per-cycle lease TTL.
DEFAULT_LEASE_TTL_S = 30.0


def cycle(
    t: datetime,
    fleet_state: FleetState,
    ledger_view: LedgerView,
    schedule: Schedule,
    scada: Mapping[str, ScadaSample],
    instructions: Sequence[Instruction],
    *,
    cycle_id: str | None = None,
    pi_states: MutableMapping[str, PiState] | None = None,
    dwell_states: MutableMapping[str, DwellState] | None = None,
    price_threshold_usd_per_mwh: float | None = None,
    dt_c_s: float = 2.0,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
    stickiness: float = 0.2,
    closed_loop_caps: Mapping[tuple[str, str], float] | None = None,
    pq: PqDispatchContext | None = None,
    enforce_territory: bool = False,
    flow_limits: FlowLimits = _NO_FLOW_LIMITS,
    excluded_hub_ids: frozenset[str] = frozenset(),
    operator_hub_ids: frozenset[str] = frozenset(),
    device_excluded_hub_ids: frozenset[str] = frozenset(),
) -> CycleResult:
    """Run one S1-S7 cycle across every bank in `fleet_state`.

    `pi_states`/`dwell_states` are mutated in place (one entry per bank) so the caller keeps exactly
    one `DistDeferralPI` integrator and one dwell tracker alive per bank across cycles (K9). Both
    default to fresh empty dicts when omitted, for one-shot/test use.

    - `price_threshold_usd_per_mwh`: a threshold for banks whose `PriceSignal` carries none. `None`
      (default): such a bank takes no headroom discharge -- the threshold is the value of the stored
      energy (09 D7), never a fixed number.
    - `closed_loop_caps`: per `(obligation_id, bank_id)`, the closed-loop controller's kW this cycle
      (`closed_loop_common.closed_loop_caps`; need basis, S4.a/S4.b). The grant follows it inside
      `[0, committed]` with R-GRANT-CLOSED-LOOP; the unused commitment stays idle (K13).
    - `pq`: S5.2 eligibility (filter + diversity/phase weights) for PQ-sensitive obligations, and the
      S5.4 ladder's hub exclusions.
    - `enforce_territory`: K15 -- each obligation only on banks its market may use (`market.
      check_territory`), FREE headroom only where the territory allows it; unknown fails closed.
    - `flow_limits`: 09 S1.9 F1-F3 caps.
    - `excluded_hub_ids`: K4 fail-safe after a guardian item veto: these hubs are unavailable this cycle
      (their kW moves to other hubs of the same obligation, recorded as a substitution) and their free kW
      comes off their bank's capability.
    - `operator_hub_ids`: the excluded hubs held by a live operator target (`engine.manual`); a shortfall on
      their bank carries R-OPERATOR-OVERRIDE.
    """
    cycle_id = cycle_id or t.isoformat()
    pi_states = {} if pi_states is None else pi_states
    dwell_states = {} if dwell_states is None else dwell_states
    closed_loop_caps = closed_loop_caps or {}

    effective_cap, l2_banks = _apply_instructions(fleet_state, instructions)
    for bank_id, budget_cap in bank_budget_caps_kw(fleet_state.banks, flow_limits).items():
        effective_cap[bank_id] = min(effective_cap.get(bank_id, budget_cap), budget_cap)

    hubs_by_bank: dict[str, list[HubSnapshot]] = {}
    # 09 S1.9: what the per-hub flow caps (F1 derating, F2 export) take off each healthy hub also comes off
    # its bank's capability, so the tiers never allocate kW the hubs cannot deliver.
    flow_cut_by_bank: dict[str, float] = {}
    for hub in fleet_state.hubs:
        if hub.hub_id in device_excluded_hub_ids and hub.is_healthy:
            # Device work (firmware update): out like a FAULT hub -- L0 attribution (`classify_hub_loss`).
            flow_cut_by_bank[hub.bank_id] = flow_cut_by_bank.get(hub.bank_id, 0.0) + max(
                hub.free_discharge_kw, 0.0
            )
            hub = hub.evolve(health="FAULT", free_discharge_kw=0.0)
        if hub.hub_id in excluded_hub_ids and hub.is_healthy:
            flow_cut_by_bank[hub.bank_id] = flow_cut_by_bank.get(hub.bank_id, 0.0) + max(
                hub.free_discharge_kw, 0.0
            )
            hub = hub.evolve(health="LAGGING", free_discharge_kw=0.0)
        if flow_limits.enabled:
            hub = with_topology(hub, flow_limits)
        sustainable = _cap_sustainable_discharge(hub, lease_ttl_s)
        capped = cap_hub(sustainable, flow_limits)
        if capped is not sustainable and sustainable.is_healthy:
            cut = sustainable.free_discharge_kw - capped.free_discharge_kw
            flow_cut_by_bank[hub.bank_id] = flow_cut_by_bank.get(hub.bank_id, 0.0) + cut
        hubs_by_bank.setdefault(hub.bank_id, []).append(capped)
    for bank in fleet_state.banks:
        cut = flow_cut_by_bank.get(bank.bank_id, 0.0)
        if cut > _EPS:
            effective_cap[bank.bank_id] = max(effective_cap.get(bank.bank_id, bank.capability_kw) - cut, 0.0)

    calls_by_bank: dict[str, list[ObligationCall]] = {}
    for call in ledger_view.calls:
        calls_by_bank.setdefault(call.bank_id, []).append(call)

    prices_by_bank = {p.bank_id: p.price_usd_per_mwh for p in schedule.prices}
    thresholds_by_bank = {
        p.bank_id: p.threshold_usd_per_mwh for p in schedule.prices if p.threshold_usd_per_mwh is not None
    }
    # S5.2 verdicts indexed once per cycle, so each PQ-sensitive obligation looks up only its own hubs.
    verdicts_by_service = (
        {svc: {v.hub_id: v for v in result.verdicts} for svc, result in pq.results_by_service.items()}
        if pq is not None
        else {}
    )

    grants: list[ProposedGrant] = []
    shortfalls: list[ShortfallReport] = []
    substitutions: list[SubstitutionEvent] = []
    held: list[str] = []
    hub_allocations: list[HubAllocation] = []
    pq_reductions: list[PqCapabilityReduction] = []
    territory_blocks: list[TerritoryBlock] = []

    for bank in sorted(fleet_state.banks, key=lambda b: b.bank_id):
        bank_id = bank.bank_id
        cap = effective_cap.get(bank_id, bank.capability_kw)
        calls = tuple(calls_by_bank.get(bank_id, ()))
        bank_hubs = hubs_by_bank.get(bank_id, [])
        hubs_by_obligation = _index_hubs_by_obligation(bank_hubs, calls)

        # K13 exception behind any shortfall on this bank: an L2 instruction binds first; otherwise the
        # dominant cause among device faults (L0), the reserve floor (L1) and unknown-state hubs.
        hub_loss_reason = classify_hub_loss(bank_hubs)
        if operator_hub_ids and any(h.hub_id in operator_hub_ids for h in bank_hubs):
            # A live operator target took hubs of this bank: any short obligation here carries the operator
            # override (G-19 corroborates it against the live MANUAL_TARGET on the bank).
            hub_loss_reason = reasons.R_OPERATOR_OVERRIDE
        shortfall_reason = reasons.R_COMMIT_LOCK_OVERRIDE_L2 if bank_id in l2_banks else hub_loss_reason
        tier_result = allocate_tiers(bank_id, calls, cap, shortfall_reason=shortfall_reason)
        bank_shortfalls = list(tier_result.shortfalls)
        short_obligations = {s.obligation_id: s.reason_code for s in tier_result.shortfalls}
        tier_short = set(short_obligations)  # short at the bank-capability step (vs. no hub substitute)
        # Obligations whose tier shortfall is not a real one this cycle: the closed-loop controller asked
        # for less than the bank could give, or K15 kept the obligation off this bank (its own report).
        not_tier_short: set[str] = set()

        remaining_headroom = tier_result.remaining_capability_kw

        # ALLOC-02/K9: exactly one DistDeferralPI step per bank per cycle -- hoisted out of the
        # per-obligation loop below, which previously re-stepped (and re-integrated) the SAME PI once
        # per DIST_DEFERRAL call on this bank. Its relief is applied to at most one obligation (the
        # first DIST_DEFERRAL call in deterministic order) rather than compounded across several.
        pi_extra_kw = 0.0
        dist_deferral_calls = [c for c in calls if c.service_type == DIST_DEFERRAL_SERVICE_TYPE]
        if dist_deferral_calls and bank_id in scada:
            pi_state = pi_states.get(bank_id, PiState())
            pi = DistDeferralPI(bank)
            pi_output_kw, new_pi_state = pi.step(scada[bank_id], pi_state, dt_c_s)
            pi_states[bank_id] = new_pi_state
            dist_deferral_tier_kw = sum(
                tier_result.granted_kw.get(c.obligation_id, 0.0) for c in dist_deferral_calls
            )
            relief_kw = max(pi_output_kw - dist_deferral_tier_kw, 0.0)
            pi_extra_kw = min(relief_kw, remaining_headroom)
            if pi_extra_kw > _EPS:
                remaining_headroom -= pi_extra_kw

        pi_extra_applied = False

        # 09 F3: the service transformers' discharge budgets for this cycle, consumed obligation by
        # obligation (nested group caps inside each water-fill).
        xfmr_remaining = xfmr_budgets_kw(bank_hubs, flow_limits)
        xfmr_of = {h.hub_id: h.xfmr_id for h in bank_hubs if h.xfmr_id is not None}
        phase_kw = _phase_discharge_kw(bank_hubs, pq) if pq is not None else {}

        # ALLOC-01/K2: a hub can appear in more than one obligation's eligible set at the same bank
        # (e.g. a HOME obligation and a DIST_DEFERRAL obligation sharing hubs). Track what this cycle
        # has already granted each hub and subtract it before the NEXT obligation's water-fill, so no
        # hub is ever granted beyond its own capability across obligations.
        granted_kw_by_hub: dict[str, float] = {}
        # Hubs delivering a PQ-sensitive obligation this cycle serve only it (its hub items are built from
        # the allocation below; a second obligation's item on the same hub would override it).
        pq_hubs: set[str] = set()
        for call in sorted(calls, key=lambda c: (_pq_result(pq, c) is None, c.obligation_id)):
            oid = call.obligation_id
            tier_granted = tier_result.granted_kw.get(oid, 0.0)
            reason_code = reasons.R_GRANT_COMMITTED

            # D-37: nothing is dispatched on an UNAVAILABLE bank (regulated, no contract) except a call
            # grandfathered under K13, which is also exempt from K15 (committed while the zone was ERCOT).
            unavailable_block = None if bank.available or call.grandfathered else reasons.R_BANK_UNAVAILABLE
            if unavailable_block is not None or (enforce_territory and not call.grandfathered):
                block = unavailable_block or check_territory(
                    call.market_ref, bank.territory, free_access=bank.free_access
                )
                if block is not None:
                    # K15 fail-safe: never served here; the shortfall is recorded against the same
                    # obligation. Its tier capacity stays locked (never exported as headroom).
                    territory_blocks.append(TerritoryBlock(bank_id, oid, block))
                    not_tier_short.add(oid)
                    bank_shortfalls.append(ShortfallReport(oid, bank_id, max(call.committed_kw, 0.0), block))
                    # An explicit 0 kW grant carrying the K15 reason: an omitted obligation reads to G-19 as
                    # an unexplained reduction to 0 (commitment-lock violation).
                    grants.append(_zero_grant(bank_id, oid, block))
                    continue

            if call.is_as_hold:
                # ERCOT_AS is a capacity hold (NPRR1282): undeployed it discharges nothing. Its tier
                # allocation stays out of `remaining_headroom` (K13: the capacity stays locked, never
                # exported), and no shortfall is reported -- holding IS delivering the service.
                held.append(oid)
                # An explicit 0 kW grant carrying R-GRANT-AS-HOLD: omitting the award from the batch would
                # read to the guardian's G-19 as an unexplained reduction to 0 (it enumerates every active
                # obligation itself); the guardian signs the hold on its own reads.
                grants.append(_zero_grant(bank_id, oid, reasons.R_GRANT_AS_HOLD))
                continue

            if (
                call.service_type == DIST_DEFERRAL_SERVICE_TYPE
                and not pi_extra_applied
                and pi_extra_kw > _EPS
            ):
                tier_granted += pi_extra_kw
                reason_code = reasons.R_GRANT_DIST_DEFERRAL_PI
                pi_extra_applied = True

            # Need basis (S4.a/S4.b): the closed-loop controller's kW, inside [0, committed]. What it
            # leaves unused stays idle (it is already out of `remaining_headroom`, K13).
            closed_loop_cap = closed_loop_caps.get((oid, bank_id))
            closed_loop_bound = closed_loop_cap is not None and closed_loop_cap < tier_granted - _EPS
            if closed_loop_cap is not None and closed_loop_bound:
                tier_granted = max(closed_loop_cap, 0.0)
                reason_code = reasons.R_GRANT_CLOSED_LOOP
                not_tier_short.add(oid)

            if tier_granted <= _EPS:
                if closed_loop_bound:
                    grants.append(_zero_grant(bank_id, oid, reasons.R_GRANT_CLOSED_LOOP))
                continue

            eligible = hubs_by_obligation.get(oid, ())
            pq_result = _pq_result(pq, call)
            reduced_by_pq = False
            if pq is not None and pq_result is not None:
                eligible, reduction = _pq_eligible(
                    call,
                    _apply_granted_so_far(eligible, granted_kw_by_hub),
                    verdicts_by_service[call.service_type],
                    pq,
                    phase_kw,
                    tier_granted,
                )
                if reduction is not None:
                    pq_reductions.append(reduction)
                    reduced_by_pq = True
            else:
                if pq_hubs:
                    eligible = tuple(h for h in eligible if h.hub_id not in pq_hubs)
                eligible = _apply_granted_so_far(eligible, granted_kw_by_hub)
            eligible = apply_group_caps(eligible, xfmr_remaining)

            result = realize_obligation(oid, bank_id, eligible, tier_granted, stickiness=stickiness)
            consume_group_budget(result.per_hub_kw, xfmr_of, xfmr_remaining)
            if result.shortfall is not None:
                # PQ eligibility is the cause when it removed the capability: no substitute exists.
                cause = reasons.R_COMMIT_LOCK_INFEASIBLE if reduced_by_pq else hub_loss_reason
                realized_short = dataclasses_replace(result.shortfall, reason_code=cause)
                bank_shortfalls.append(realized_short)
                short_obligations.setdefault(oid, realized_short.reason_code)
                not_tier_short.discard(oid)
            if oid in short_obligations and oid not in not_tier_short:
                # A grant below the committed kW carries its K13 exception (G-19 accepts only these); an
                # obligation already in SHORTFALL carries the best-effort shortfall code G-19 corroborates.
                reason_code = short_obligations[oid]
                if call.in_shortfall:
                    reason_code = best_effort_reason(reason_code, at_bank_capacity=oid in tier_short)
            if result.event is not None:
                substitutions.append(result.event)

            for hub_id, kw in result.per_hub_kw.items():
                granted_kw_by_hub[hub_id] = granted_kw_by_hub.get(hub_id, 0.0) + kw
            if pq_result is not None:
                used = tuple(sorted((h, kw) for h, kw in result.per_hub_kw.items() if kw > _EPS))
                pq_hubs.update(h for h, _ in used)
                hub_allocations.append(HubAllocation(oid, bank_id, used))

            delivered = sum(result.per_hub_kw.values())
            if delivered > _EPS:
                grants.append(
                    ProposedGrant(
                        bank_id=bank_id,
                        granted_kw=delivered,
                        obligation_id=oid,
                        is_headroom=False,
                        reason_code=reason_code,
                    )
                )
            elif closed_loop_bound:
                grants.append(_zero_grant(bank_id, oid, reasons.R_GRANT_CLOSED_LOOP))

        shortfalls.extend(
            s
            for s in bank_shortfalls
            if not (s.obligation_id in not_tier_short and s in tier_result.shortfalls)
        )

        if bank_id in schedule.conservative_bank_ids:
            # A CONSERVATIVE scope (K7 escalation) takes no new uncommitted/market dispatch.
            continue
        spot_kw = _headroom_kw(
            t,
            bank,
            remaining_headroom,
            calls=calls,
            bank_hubs=bank_hubs,
            granted_kw_by_hub=granted_kw_by_hub,
            pq_hubs=pq_hubs,
            price=prices_by_bank.get(bank_id, 0.0),
            threshold=thresholds_by_bank.get(bank_id, price_threshold_usd_per_mwh),
            dwell_states=dwell_states,
            lease_ttl_s=lease_ttl_s,
            enforce_territory=enforce_territory,
            flow_limits=flow_limits,
            territory_blocks=territory_blocks,
            plan_floor_kwh=schedule.hold_floor_kwh.get(bank_id),
        )
        if spot_kw > _EPS:
            grants.append(
                ProposedGrant(
                    bank_id=bank_id,
                    granted_kw=spot_kw,
                    obligation_id=None,
                    is_headroom=True,
                    reason_code=reasons.R_GRANT_HEADROOM,
                )
            )

    shortfalls.sort(key=lambda s: (s.obligation_id, s.bank_id))
    grants.sort(key=lambda g: (g.bank_id, g.obligation_id or ""))
    substitutions.sort(key=lambda s: (s.obligation_id, s.bank_id))

    return CycleResult(
        cycle_id=cycle_id,
        grants=tuple(grants),
        shortfalls=tuple(shortfalls),
        substitutions=tuple(substitutions),
        held=tuple(sorted(set(held))),
        hub_allocations=tuple(hub_allocations),
        pq_reductions=tuple(pq_reductions),
        territory_blocks=tuple(territory_blocks),
    )


def _zero_grant(bank_id: str, obligation_id: str, reason_code: str) -> ProposedGrant:
    return ProposedGrant(
        bank_id=bank_id,
        granted_kw=0.0,
        obligation_id=obligation_id,
        is_headroom=False,
        reason_code=reason_code,
    )


def _headroom_kw(
    t: datetime,
    bank: BankSnapshot,
    remaining_headroom: float,
    *,
    calls: Sequence[ObligationCall],
    bank_hubs: Sequence[HubSnapshot],
    granted_kw_by_hub: Mapping[str, float],
    pq_hubs: set[str],
    price: float,
    threshold: float | None,
    dwell_states: MutableMapping[str, DwellState],
    lease_ttl_s: float,
    enforce_territory: bool,
    flow_limits: FlowLimits,
    territory_blocks: list[TerritoryBlock],
    plan_floor_kwh: float | None = None,
) -> float:
    """S6: the bank's FREE headroom discharge this cycle.

    - K15: none where the bank's territory does not allow FREE dispatch (or is unknown).
    - F7 / Frank #6: never below the AS energy hold (`energy_hold.headroom_energy_cap_kw`: reserve + 1 %
      x capacity + kW x duration / eta_d for every ERCOT_AS award on the bank).
    - 09 S1.9: with flow limits on, no more than the hubs' remaining capped capability.
    - 09 D7: only when the price clears the stored-energy value (`threshold`); unknown value: none.
    """
    bank_id = bank.bank_id
    if not bank.available:
        # D-37: an UNAVAILABLE bank (regulated, no contract) takes no headroom, whatever the territory says.
        territory_blocks.append(TerritoryBlock(bank_id, None, reasons.R_BANK_UNAVAILABLE))
        return 0.0
    if enforce_territory:
        block = check_territory(FREE, bank.territory, free_access=bank.free_access)
        if block is not None:
            territory_blocks.append(TerritoryBlock(bank_id, None, block))
            return 0.0
    as_awards = [c for c in calls if c.is_capacity_hold]
    if as_awards or plan_floor_kwh is not None:
        remaining_headroom = min(
            remaining_headroom,
            headroom_energy_cap_kw(bank_hubs, as_awards, lease_ttl_s, plan_floor_kwh=plan_floor_kwh),
        )
    if flow_limits.enabled:
        hub_room = sum(
            max(h.free_discharge_kw - granted_kw_by_hub.get(h.hub_id, 0.0), 0.0)
            for h in bank_hubs
            if h.is_healthy and h.hub_id not in pq_hubs
        )
        remaining_headroom = min(remaining_headroom, hub_room)
    if threshold is None:
        return 0.0
    spot_kw, new_dwell = price_responsive_schedule(
        max(remaining_headroom, 0.0), price, threshold, dwell_states.get(bank_id, DwellState()), t
    )
    dwell_states[bank_id] = new_dwell
    return spot_kw


def _pq_result(pq: PqDispatchContext | None, call: ObligationCall) -> EligibilityResult | None:
    return pq.results_by_service.get(call.service_type) if pq is not None else None


def _phase_discharge_kw(hubs: Sequence[HubSnapshot], pq: PqDispatchContext) -> dict[str, float]:
    """S3.2(c): the bank's measured discharge per single phase (kW), the phase-balance weight's input."""
    per_phase = dict.fromkeys(_SINGLE_PHASES, 0.0)
    for hub in hubs:
        phase = pq.phase_by_hub_id.get(hub.hub_id)
        if phase in per_phase and hub.p_kw is not None:
            per_phase[phase] += max(-hub.p_kw, 0.0)
    return per_phase


def _pq_eligible(
    call: ObligationCall,
    hubs: tuple[HubSnapshot, ...],
    verdicts: Mapping[str, HubEligibilityVerdict],
    pq: PqDispatchContext,
    phase_kw: Mapping[str, float],
    needed_kw: float,
) -> tuple[tuple[HubSnapshot, ...], PqCapabilityReduction | None]:
    """S5.2 steps 1-2 for one PQ-sensitive obligation: keep its PQ-eligible hubs, weighted for harmonic
    diversity and phase balance (`pq_eligibility.apply_eligibility`). Hubs the S5.4 ladder excluded from
    it go to substitution as unhealthy, so the swap is recorded with R-SUBSTITUTION. Reports a capability
    reduction when eligibility leaves less than the obligation needs."""
    excluded = pq.excluded_by_obligation.get(call.obligation_id, frozenset())
    before_kw = sum(h.free_discharge_kw for h in hubs if h.is_healthy)
    candidates = tuple(h for h in hubs if h.hub_id not in excluded)
    phases = {h.hub_id: pq.phase_by_hub_id[h.hub_id] for h in candidates if h.hub_id in pq.phase_by_hub_id}
    # This obligation's own hubs' verdicts only (a hub with no verdict is not eligible).
    result = EligibilityResult(tuple(verdicts[h.hub_id] for h in candidates if h.hub_id in verdicts))
    kept = apply_eligibility(candidates, result, phase_by_hub_id=phases, phase_kw_by_phase=phase_kw)
    ladder_out = tuple(h.evolve(health="LAGGING") for h in hubs if h.hub_id in excluded and h.is_healthy)
    after_kw = sum(h.free_discharge_kw for h in kept if h.is_healthy)
    reduction = None
    if after_kw < before_kw - _EPS and after_kw < needed_kw - _EPS:
        kept_ids = {h.hub_id for h in kept}
        reduction = PqCapabilityReduction(
            obligation_id=call.obligation_id,
            bank_id=call.bank_id,
            needed_kw=needed_kw,
            capability_before_kw=before_kw,
            capability_after_kw=after_kw,
            excluded_hub_ids=tuple(sorted(h.hub_id for h in hubs if h.hub_id not in kept_ids)),
        )
    return kept + ladder_out, reduction


def best_effort_reason(lock_reason: str, *, at_bank_capacity: bool) -> str:
    """Owner decision 2026-09-26: a SHORTFALL obligation keeps receiving its maximum feasible kW; that
    partial grant carries the shortfall code (`core.reasons.LOCK_REASON_BY_SHORTFALL`'s keys) that the
    guardian's G-19 corroborates: an L2 instruction, the bank's capability, or no substitute hub. L0/L1
    keep their own K13 code (corroborated from the guardian's capability read)."""
    if lock_reason == reasons.R_COMMIT_LOCK_OVERRIDE_L2:
        return reasons.R_SHORTFALL_L2_INSTRUCTION
    if lock_reason == reasons.R_COMMIT_LOCK_INFEASIBLE:
        return reasons.R_SHORTFALL_BANK_CAPACITY if at_bank_capacity else reasons.R_SHORTFALL_NO_SUBSTITUTE
    return lock_reason


def classify_hub_loss(hubs: Sequence[HubSnapshot]) -> str:
    """The K13 exception behind a bank's lost hub capacity this cycle (02a S2.1 lock paths, ES05-S03),
    from each hub's nameplate `rated_kw` against what it can deliver now:

    - L0 (`R-COMMIT-LOCK-OVERRIDE-L0`, device safety): capacity of hubs excluded for a FAULT;
    - L1 (`R-COMMIT-LOCK-OVERRIDE-L1`, homeowner reserve): capacity healthy hubs cannot deliver because
      their SoC is near the reserve floor (the K1 caps: `hub_capability` and the lease-horizon cap);
    - otherwise `R-COMMIT-LOCK-INFEASIBLE` (stale/offline hubs of unknown state, no substitute).

    The largest of the three wins; ties go L0 > L1 > infeasible. A hub without `rated_kw` counts
    nothing, so missing data can never manufacture an override."""
    l0 = l1 = unknown = 0.0
    for hub in hubs:
        if hub.rated_kw is None:
            continue
        if hub.health == "FAULT":
            l0 += hub.rated_kw
        elif hub.is_healthy and hub.soc_kwh is not None:
            l1 += max(hub.rated_kw - hub.free_discharge_kw, 0.0)
        elif not hub.is_healthy:
            unknown += hub.rated_kw
    ranked = (
        (l0, reasons.R_COMMIT_LOCK_OVERRIDE_L0),
        (l1, reasons.R_COMMIT_LOCK_OVERRIDE_L1),
        (unknown, reasons.R_COMMIT_LOCK_INFEASIBLE),
    )
    best_kw, best_reason = max(ranked, key=lambda r: r[0])  # max keeps the first of equal values
    return best_reason if best_kw > _EPS else reasons.R_COMMIT_LOCK_INFEASIBLE


def _cap_sustainable_discharge(hub: HubSnapshot, lease_ttl_s: float) -> HubSnapshot:
    """CORE-003/K1: cap `free_discharge_kw` by the ENERGY the hub can sustain for the command's full
    HOLD HORIZON -- the lease TTL (`lease_ttl_s`), not merely the 2 s tick -- via
    `hub_sustainable_discharge_kw`. A sliver of energy just above reserve must not be offered to
    water-filling/PI at the hub's full power rating for the whole lease duration; capacity (kW) alone
    is not sufficient (user requirement: energy above reserve must be checked continuously).

    K7/dispatch-live pass: when the fleet twin has NOT supplied a live `soc_kwh`/`reserve_kwh` reading
    for this hub this cycle (`None` -- always the case for a stale/offline/fault hub,
    `opengrid.fleet.hub_capabilities`), this is never trusted as "assume full power is safe". The
    conservative fallback is 0 kW discharge capability, exactly like a hub the fleet twin already
    excluded -- a missing or stale SoC reading must NEVER silently imply `free_discharge_kw` at face
    value is safe to promise for the whole lease (this replaces a prior "trust it as-is" no-op that was
    the reported silent full-power fallback bug).
    """
    if hub.soc_kwh is None or hub.reserve_kwh is None:
        if hub.free_discharge_kw <= 0.0:
            return hub
        return hub.evolve(free_discharge_kw=0.0)
    dt_h = lease_ttl_s / 3600.0
    sustainable_kw = hub_sustainable_discharge_kw(
        hub.soc_kwh, hub.reserve_kwh, hub.free_discharge_kw, dt_h, hub.eta_d
    )
    if sustainable_kw >= hub.free_discharge_kw:
        return hub
    return hub.evolve(free_discharge_kw=sustainable_kw)


def _apply_granted_so_far(
    hubs: tuple[HubSnapshot, ...], granted_kw_by_hub: Mapping[str, float]
) -> tuple[HubSnapshot, ...]:
    """ALLOC-01/K2: shrink each hub's free capability by whatever this cycle already granted it (for a
    different obligation sharing eligibility) before the next obligation's water-fill sees it -- the
    same hub can never be committed beyond `hub.free_discharge_kw` in total across obligations."""
    if not granted_kw_by_hub:
        return hubs
    adjusted: list[HubSnapshot] = []
    for hub in hubs:
        used = granted_kw_by_hub.get(hub.hub_id, 0.0)
        if used <= _EPS:
            adjusted.append(hub)
        else:
            adjusted.append(hub.evolve(free_discharge_kw=max(hub.free_discharge_kw - used, 0.0)))
    return tuple(adjusted)


def _index_hubs_by_obligation(
    hubs: Sequence[HubSnapshot], calls: Sequence[ObligationCall]
) -> dict[str, tuple[HubSnapshot, ...]]:
    by_id = {h.hub_id: h for h in hubs}
    result: dict[str, tuple[HubSnapshot, ...]] = {}
    for call in calls:
        result[call.obligation_id] = tuple(by_id[hid] for hid in call.eligible_hub_ids if hid in by_id)
    return result


def _apply_instructions(
    fleet_state: FleetState, instructions: Sequence[Instruction]
) -> tuple[dict[str, float], set[str]]:
    """K5: L2 instructions are hard constraints, applied before any economic decision. `BLOCK`/
    `ESTOP` zero a scope's capability; `LIMIT` caps it. `FLEET` and `ZONE` scopes apply to every bank
    in scope. Returns (effective_capability_by_bank, banks_touched_by_an_L2_instruction).
    """
    effective = {b.bank_id: b.capability_kw for b in fleet_state.banks}
    touched: set[str] = set()
    zones = {b.bank_id: b.zone for b in fleet_state.banks}

    for instr in instructions:
        if instr.scope == "FLEET":
            targets = list(effective)
        elif instr.scope == "ZONE":
            targets = [bid for bid, zone in zones.items() if zone == instr.scope_ref]
        else:
            targets = [instr.scope_ref] if instr.scope_ref in effective else []

        for bank_id in targets:
            touched.add(bank_id)
            if instr.kind in ("BLOCK", "ESTOP"):
                effective[bank_id] = 0.0
            elif instr.kind == "LIMIT" and instr.limit_kw is not None:
                effective[bank_id] = min(effective[bank_id], max(instr.limit_kw, 0.0))

    return effective, touched
