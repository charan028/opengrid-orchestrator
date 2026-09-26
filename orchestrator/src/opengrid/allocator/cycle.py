"""The 2-second S1-S7 allocation cycle (02a S5), as a pure function of its inputs.

`cycle()` never touches a database or the network -- the thin adapter in
`opengrid.allocator.__init__.run_cycle` gathers `FleetState`/`LedgerView`/`Schedule`/SCADA/instruction
inputs from `opengrid.fleet`/`opengrid.ledger`/`opengrid.feeds` and converts this function's
`CycleResult` into `og.grant` rows for the guardian. Keeping the logic pure makes it possible to
hypothesis-test K1/K4/K5/K9/K13 without a DB, and keeps the hot path numpy-friendly for 2,000-10,000
hubs across ~40 banks within the 200 ms budget (BUILD.md S5's performance target).

The allocator NEVER selects new opportunities and NEVER reallocates a committed obligation's capacity
to a different obligation (K13) -- it only re-derives, every 2 s, how much of each already-committed
obligation's frozen floor is physically deliverable right now, substitutes hubs within the same
obligation on health loss (S5), and schedules any leftover headroom (S6).
"""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import replace as dataclasses_replace
from datetime import datetime

from opengrid.allocator import reasons
from opengrid.allocator.dist_deferral_pi import DistDeferralPI
from opengrid.allocator.lexicographic import allocate_tiers
from opengrid.allocator.models import (
    CycleResult,
    DwellState,
    FleetState,
    HubSnapshot,
    Instruction,
    LedgerView,
    ObligationCall,
    PiState,
    ProposedGrant,
    ScadaSample,
    Schedule,
    ShortfallReport,
    SubstitutionEvent,
)
from opengrid.allocator.price_response import price_responsive_schedule
from opengrid.allocator.substitution import realize_obligation
from opengrid.core.physics import hub_sustainable_discharge_kw

_EPS = 1e-9
_DEFAULT_PRICE_THRESHOLD_USD_PER_MWH = 30.0
# 02a S5's "hold horizon (the lease TTL, not 2 s)": how long a discharge grant must be SUSTAINABLE for,
# not merely instantaneously safe -- K7's hold-the-last-setpoint-until-lease-expiry duration (00-
# invariants.md K7: "30 s during events, 60 s otherwise"). The conservative (shorter) default is used
# unless the caller knows the actual per-cycle lease TTL.
_DEFAULT_LEASE_TTL_S = 30.0


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
    price_threshold_usd_per_mwh: float = _DEFAULT_PRICE_THRESHOLD_USD_PER_MWH,
    dt_c_s: float = 2.0,
    lease_ttl_s: float = _DEFAULT_LEASE_TTL_S,
    stickiness: float = 0.2,
) -> CycleResult:
    """Run one S1-S7 cycle across every bank in `fleet_state`.

    `pi_states`/`dwell_states` are mutated in place (one entry per bank) so the caller keeps exactly
    one `DistDeferralPI` integrator and one dwell tracker alive per bank across cycles (K9). Both
    default to fresh empty dicts when omitted, for one-shot/test use.
    """
    cycle_id = cycle_id or t.isoformat()
    pi_states = {} if pi_states is None else pi_states
    dwell_states = {} if dwell_states is None else dwell_states

    effective_cap, l2_banks = _apply_instructions(fleet_state, instructions)

    hubs_by_bank: dict[str, list[HubSnapshot]] = {}
    for hub in fleet_state.hubs:
        hubs_by_bank.setdefault(hub.bank_id, []).append(_cap_sustainable_discharge(hub, lease_ttl_s))

    calls_by_bank: dict[str, list[ObligationCall]] = {}
    for call in ledger_view.calls:
        calls_by_bank.setdefault(call.bank_id, []).append(call)

    prices_by_bank = {p.bank_id: p.price_usd_per_mwh for p in schedule.prices}

    grants: list[ProposedGrant] = []
    shortfalls: list[ShortfallReport] = []
    substitutions: list[SubstitutionEvent] = []

    for bank in sorted(fleet_state.banks, key=lambda b: b.bank_id):
        bank_id = bank.bank_id
        cap = effective_cap.get(bank_id, bank.capability_kw)
        calls = tuple(calls_by_bank.get(bank_id, ()))
        hubs_by_obligation = _index_hubs_by_obligation(hubs_by_bank.get(bank_id, ()), calls)

        shortfall_reason = (
            reasons.R_COMMIT_LOCK_OVERRIDE_L2 if bank_id in l2_banks else reasons.R_COMMIT_LOCK_INFEASIBLE
        )
        tier_result = allocate_tiers(bank_id, calls, cap, shortfall_reason=shortfall_reason)
        shortfalls.extend(tier_result.shortfalls)

        remaining_headroom = tier_result.remaining_capability_kw

        # ALLOC-02/K9: exactly one DistDeferralPI step per bank per cycle -- hoisted out of the
        # per-obligation loop below, which previously re-stepped (and re-integrated) the SAME PI once
        # per DIST_DEFERRAL call on this bank. Its relief is applied to at most one obligation (the
        # first DIST_DEFERRAL call in deterministic order) rather than compounded across several.
        pi_extra_kw = 0.0
        dist_deferral_calls = [c for c in calls if c.service_type == "DIST_DEFERRAL"]
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

        # ALLOC-01/K2: a hub can appear in more than one obligation's eligible set at the same bank
        # (e.g. a HOME obligation and a DIST_DEFERRAL obligation sharing hubs). Track what this cycle
        # has already granted each hub and subtract it before the NEXT obligation's water-fill, so no
        # hub is ever granted beyond its own capability across obligations.
        granted_kw_by_hub: dict[str, float] = {}
        for call in sorted(calls, key=lambda c: c.obligation_id):
            tier_granted = tier_result.granted_kw.get(call.obligation_id, 0.0)
            reason_code = reasons.R_GRANT_COMMITTED

            if call.service_type == "DIST_DEFERRAL" and not pi_extra_applied and pi_extra_kw > _EPS:
                tier_granted += pi_extra_kw
                reason_code = reasons.R_GRANT_DIST_DEFERRAL_PI
                pi_extra_applied = True

            if tier_granted <= _EPS:
                continue

            eligible = _apply_granted_so_far(
                hubs_by_obligation.get(call.obligation_id, ()), granted_kw_by_hub
            )
            result = realize_obligation(
                call.obligation_id, bank_id, eligible, tier_granted, stickiness=stickiness
            )
            if result.shortfall is not None:
                shortfalls.append(result.shortfall)
            if result.event is not None:
                substitutions.append(result.event)

            for hub_id, kw in result.per_hub_kw.items():
                granted_kw_by_hub[hub_id] = granted_kw_by_hub.get(hub_id, 0.0) + kw

            delivered = sum(result.per_hub_kw.values())
            if delivered > _EPS:
                grants.append(
                    ProposedGrant(
                        bank_id=bank_id,
                        granted_kw=delivered,
                        obligation_id=call.obligation_id,
                        is_headroom=False,
                        reason_code=reason_code,
                    )
                )

        spot_kw, new_dwell = price_responsive_schedule(
            remaining_headroom,
            prices_by_bank.get(bank_id, 0.0),
            price_threshold_usd_per_mwh,
            dwell_states.get(bank_id, DwellState()),
            t,
        )
        dwell_states[bank_id] = new_dwell
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
    )


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
        return dataclasses_replace(hub, free_discharge_kw=0.0)
    dt_h = lease_ttl_s / 3600.0
    sustainable_kw = hub_sustainable_discharge_kw(
        hub.soc_kwh, hub.reserve_kwh, hub.free_discharge_kw, dt_h, hub.eta_d
    )
    if sustainable_kw >= hub.free_discharge_kw:
        return hub
    return dataclasses_replace(hub, free_discharge_kw=sustainable_kw)


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
            adjusted.append(
                dataclasses_replace(hub, free_discharge_kw=max(hub.free_discharge_kw - used, 0.0))
            )
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
