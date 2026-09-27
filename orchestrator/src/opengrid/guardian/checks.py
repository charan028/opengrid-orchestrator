"""Guardian G-checks (02a S6, canonical numbering in 00-invariants.md): G-01, G-02, G-03, G-04, G-05,
G-06, G-09, G-13, G-14, G-15, G-19, G-20.

Every check is a pure function over plain values -- no I/O, no knowledge of Postgres/MQTT -- delegating
the actual envelope/limit arithmetic to `opengrid.core.limits`/`opengrid.core.timeutil`/`opengrid.core.
physics` (the SAME functions the allocator calls at planning time, per 02b S12's "one formula, two data
paths"). `opengrid.guardian.service.GuardianService` supplies the guardian's own independently-read
inputs and interprets the resulting `CheckOutcome`s into a `Verdict`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from opengrid.core import limits as core_limits
from opengrid.core import reasons
from opengrid.core.physics import BankParams, HubParams
from opengrid.core.services import ERCOT_AS_SERVICE_TYPE
from opengrid.core.services import HOLD_SERVICE_TYPES as HOLD_SERVICE_TYPES
from opengrid.core.timeutil import check_command_freshness, clock_offset_ok
from opengrid.guardian.ports import L2Instruction, ProposedItem
from opengrid.market.territory import TERRITORY_REASONS


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    rule_id: str
    ok: bool
    reason: str | None = None
    hub_id: str | None = None
    obligation_id: str | None = None

    @staticmethod
    def passed(rule_id: str, *, hub_id: str | None = None) -> CheckOutcome:
        return CheckOutcome(rule_id, True, None, hub_id)


def hub_setpoints(items: list[ProposedItem]) -> list[ProposedItem]:
    """One item per hub carrying the SUM of that hub's setpoints in the batch (a hub executes the sum of its
    items, one per obligation). Every hub-level limit is evaluated on this; the per-item obligation mapping
    stays on the batch for G-19. The first item's reason code is kept for tracing only."""
    totals: dict[str, float] = {}
    first: dict[str, ProposedItem] = {}
    for item in items:
        totals[item.hub_id] = totals.get(item.hub_id, 0.0) + item.p_kw_setpoint
        first.setdefault(item.hub_id, item)
    return [
        first[hub_id]
        if totals[hub_id] == first[hub_id].p_kw_setpoint
        else ProposedItem(hub_id, totals[hub_id], first[hub_id].reason_code)
        for hub_id in totals
    ]


def check_g01_reserve(
    item: ProposedItem, hub: HubParams, soc_kwh: float, *, margin_pct: float = 0.01
) -> CheckOutcome:
    """K1: the hub's reported SoC must clear reserve + margin under the proposed setpoint."""
    result = core_limits.check_reserve_floor(soc_kwh, hub, margin_pct=margin_pct)
    return CheckOutcome("G-01", result.ok, result.reason, item.hub_id)


def check_g01_energy_lease(
    item: ProposedItem, hub: HubParams, soc_kwh: float, lease_ttl_h: float, *, margin_pct: float = 0.01
) -> CheckOutcome:
    """G-01-ENERGY/K1: independent of the instantaneous G-01 check, project the hub's OWN
    guardian-read SoC across the command's full lease duration (`lease_ttl_h`, from the batch's own
    `issued_at`/`expires_at`) and veto if the projected SoC would breach reserve (discharge) or
    overfill above `e_kwh` (charge) before the lease expires -- capacity (kW) headroom alone does not
    guarantee enough energy above reserve to sustain the command for its whole hold duration."""
    result = core_limits.check_reserve_floor_over_lease(
        soc_kwh, item.p_kw_setpoint, lease_ttl_h, hub, margin_pct=margin_pct
    )
    return CheckOutcome("G-01-ENERGY", result.ok, result.reason, item.hub_id)


def check_g02_hub_power(item: ProposedItem, hub: HubParams, *, inverter_cap_kw: float) -> CheckOutcome:
    """K4: |P| bound per hub -- the home's own rated power; `inverter_cap_kw` is per unit (see
    `core.limits.check_hub_power`)."""
    result = core_limits.check_hub_power(item.p_kw_setpoint, hub, inverter_cap_kw=inverter_cap_kw)
    return CheckOutcome("G-02", result.ok, result.reason, item.hub_id)


def check_g03_bank_kva(
    bank_id: str, additional_kw: float, bank_load_kva: float, bank: BankParams, *, loading_pct: float = 0.95
) -> CheckOutcome:
    """K4/K9: bank/feeder loading, calling the same `recharge_headroom` the allocator's PI loop uses."""
    result = core_limits.check_bank_kva(bank_load_kva, additional_kw, bank, loading_pct=loading_pct)
    return CheckOutcome("G-03", result.ok, result.reason, bank_id)


def check_g03_bank_load_fresh(bank_id: str, bank_load_age_s: float, max_age_s: float) -> CheckOutcome:
    """K4: G-03 needs a live SCADA bank-load reading. A missing (`inf`) or stale one is unknown loading
    -- VETO (a hold), never a silent 0 kVA that would let any batch through."""
    if not math.isfinite(bank_load_age_s) or bank_load_age_s > max_age_s:
        return CheckOutcome("G-03", False, "BANK_LOAD_STALE", bank_id)
    return CheckOutcome.passed("G-03", hub_id=bank_id)


def check_g03_bank_kva_magnitude(
    bank_id: str, bank_load_kva: float, net_delta_kw: float, bank: BankParams, *, loading_pct: float = 0.95
) -> CheckOutcome:
    """K4: transformer loading is a MAGNITUDE, so discharge that drives the bank into reverse flow loads
    it exactly as charging does, and so does cutting discharge on an importing bank. Bounds the projected
    |bank load + the batch's net setpoint change| by the same rating-net-of-reserve envelope
    (`core.limits.check_bank_kva` on the magnitude), in both directions.

    SCADA reports apparent power as a magnitude; it is taken as import-positive (the residential bank
    convention the sim and the allocator's PI loop share). A batch that does not increase the magnitude
    (relief on an already-overloaded bank) is never vetoed here: blocking it would block the relief."""
    projected_kw = bank_load_kva + net_delta_kw
    magnitude_kw = abs(projected_kw)
    if magnitude_kw <= abs(bank_load_kva) + 1e-9:
        return CheckOutcome.passed("G-03", hub_id=bank_id)
    result = core_limits.check_bank_kva(0.0, magnitude_kw, bank, loading_pct=loading_pct)
    if result.ok:
        return CheckOutcome.passed("G-03", hub_id=bank_id)
    reason = "BANK_KVA_LIMIT_REVERSE_FLOW" if projected_kw < 0 else result.reason
    return CheckOutcome("G-03", False, reason, bank_id)


def check_g04_hub_ramp(
    item: ProposedItem, prev_p_kw: float, dt_s: float, ramp_kw_per_s: float
) -> CheckOutcome:
    """K4: per-hub ramp bound."""
    result = core_limits.check_hub_ramp(prev_p_kw, item.p_kw_setpoint, dt_s, ramp_kw_per_s)
    return CheckOutcome("G-04", result.ok, result.reason, item.hub_id)


@dataclass(frozen=True, slots=True)
class G04Anchor:
    """Where G-04 measures a hub's step from, and over how long."""

    kw: float
    dt_s: float
    source: Literal["TELEMETRY", "SIGNED"]


def g04_anchor_kw(
    *,
    prev_telemetry_kw: float,
    telemetry_ts: datetime | None,
    last_signed_kw: float | None,
    last_signed_at: datetime | None,
    lease_expires_at: datetime | None,
    now: datetime,
    utility_scale: bool,
    cycle_interval_s: float,
) -> G04Anchor:
    """G-04's anchor (agreed with DISPATCH, r3.4.1). A utility-scale hub (og.asset SUBSTATION / MOBILE_STORAGE)
    reports telemetry only every ~10 s while the engine steps it every 2 s cycle from its last command; measured
    against the stale telemetry, every step after the first looked like several and was vetoed, so the 20 MW
    toll never delivered. For a utility-scale hub G-04 anchors at the last setpoint the GUARDIAN ITSELF SIGNED
    for it whenever that signed lease is still live -- while it is, the hub is following that setpoint, so that
    is where its physical step starts (lead's decision with DISPATCH; a "signed newer than the telemetry sample"
    condition dropped to the stale telemetry on a timestamp tie and vetoed every following step). dt = the time
    since that signature (at least one cycle). Otherwise -- homes always, or no live signed lease -- the telemetry
    `prev_p_kw` over one cycle, as before. The bound itself is unchanged: `hub_ramp_kw_per_s(params) x dt`.
    `telemetry_ts` is accepted for the caller's record and future use; it does not change the anchor."""
    del telemetry_ts  # the live signed lease alone decides (see above)
    if (
        utility_scale
        and last_signed_kw is not None
        and last_signed_at is not None
        and lease_expires_at is not None
        and lease_expires_at > now
    ):
        elapsed_s = max((now - last_signed_at).total_seconds(), 0.0)
        return G04Anchor(last_signed_kw, max(elapsed_s, cycle_interval_s), "SIGNED")
    return G04Anchor(prev_telemetry_kw, cycle_interval_s, "TELEMETRY")


def ramp_step_per_cycle_kw(setpoint_kw: float, anchor: G04Anchor, cycle_interval_s: float) -> float:
    """The step a hub takes this cycle for the RATE checks (G-05 fleet ramp and stagger, G-06/G-32 feeder ramp):
    from the same anchor as G-04, scaled to one cycle when the anchor is older than one (a utility-scale hub whose
    last signed step was two cycles ago spreads that change over both). Homes: setpoint - telemetry, as before."""
    return (setpoint_kw - anchor.kw) * (cycle_interval_s / anchor.dt_s) if anchor.dt_s > 0 else 0.0


def check_g05_fleet_ramp(
    fleet_delta_kw: float,
    dt_s: float,
    *,
    is_firm_event: bool,
    discretionary_cap_kw_per_min: float = 50_000.0,
    non_firm_cap_kw_per_min: float = 10_000.0,
) -> CheckOutcome:
    """K4: fleet-wide ramp cap for synchronized steps (discretionary vs. non-firm)."""
    result = core_limits.check_fleet_ramp_cap(
        fleet_delta_kw,
        dt_s,
        discretionary_cap_kw_per_min=discretionary_cap_kw_per_min,
        non_firm_cap_kw_per_min=non_firm_cap_kw_per_min,
        is_firm_event=is_firm_event,
    )
    return CheckOutcome("G-05", result.ok, result.reason)


def check_g06_feeder_ramp(
    feeder_delta_kw: float, dt_s: float, feeder_ceiling_kw_per_min: float, *, is_firm_event: bool
) -> CheckOutcome:
    """K4: per-feeder/substation ramp ceiling for firm events (review §6 finding #4, ES06-S05)."""
    result = core_limits.check_feeder_ramp_ceiling(
        feeder_delta_kw, dt_s, feeder_ceiling_kw_per_min, is_firm_event=is_firm_event
    )
    return CheckOutcome("G-06", result.ok, result.reason)


def check_g09_ledger_version(batch_ledger_version: int, current_ledger_version: int) -> CheckOutcome:
    """K2: the batch must be built on the ledger version guardian itself reads right now -- a stale
    version means another writer has moved the ledger underneath the proposal (02a S6.1: "additive floor
    still satisfiable")."""
    if batch_ledger_version != current_ledger_version:
        return CheckOutcome("G-09", False, "STALE_LEDGER_VERSION")
    return CheckOutcome.passed("G-09")


def check_g13_freshness(
    *,
    epoch: int,
    seq: int,
    last_accepted_epoch: int,
    last_accepted_seq: int,
    issued_at: datetime,
    expires_at: datetime,
    now: datetime,
) -> CheckOutcome:
    """K6: sequence/epoch/lease freshness, independently re-checked at the guardian."""
    result = check_command_freshness(
        epoch=epoch,
        seq=seq,
        last_accepted_epoch=last_accepted_epoch,
        last_accepted_seq=last_accepted_seq,
        issued_at=issued_at,
        expires_at=expires_at,
        now=now,
    )
    return CheckOutcome("G-13", result.ok, result.reason)


def check_g14_trace_preimage(exists: bool) -> CheckOutcome:
    """K10: no command is signed unless its decision pre-image is already durably traced."""
    if not exists:
        return CheckOutcome("G-14", False, "TRACE_PREIMAGE_MISSING")
    return CheckOutcome.passed("G-14")


def check_g15_l2_boundary(
    instruction: L2Instruction | None, bank_id: str, aggregate_abs_kw: float
) -> CheckOutcome:
    """K5: an active L2 utility/ISO instruction is a hard constraint, never relaxed for commercial
    value. `ESTOP`/`BLOCK` allow nothing beyond 0 kW; `LIMIT` bounds the aggregate magnitude."""
    if instruction is None:
        return CheckOutcome.passed("G-15", hub_id=bank_id)
    if instruction.kind in ("ESTOP", "BLOCK"):
        ok = abs(aggregate_abs_kw) <= 1e-9
    else:  # LIMIT
        limit = instruction.limit_kw if instruction.limit_kw is not None else 0.0
        ok = abs(aggregate_abs_kw) <= limit + 1e-9
    return CheckOutcome("G-15", ok, None if ok else "L2_BOUNDARY_EXCEEDED", bank_id)


def check_g19_commitment_lock(
    obligation_id: str,
    new_kw: float,
    frozen_kw: float,
    prior_kw: float,
    reason_code: str | None,
    *,
    as_release_enabled: bool = False,
) -> CheckOutcome:
    """K13: refuse any reduction of a committed allocation without an allowed reason code -- the
    independent check (00-invariants.md K13, review finding #1, ES06-S02)."""
    result = core_limits.check_commitment_lock(
        new_kw, frozen_kw, prior_kw, reason_code, as_release_enabled=as_release_enabled
    )
    return CheckOutcome("G-19", result.ok, result.reason, obligation_id=obligation_id)


def g19_reduction_below_floor(new_kw: float, frozen_kw: float, prior_kw: float) -> bool:
    """True when `new_kw` is below the commitment floor, i.e. G-19 passes only if an override reason
    excuses it. Asks `core.limits.check_commitment_lock` itself (no reason code), never re-derives it."""
    return not core_limits.check_commitment_lock(new_kw, frozen_kw, prior_kw, None).ok


#: K13 overrides the guardian re-verifies from its own capability read (02a S2.1: L0 device safety and
#: L1 homeowner reserve both surface as hubs that cannot deliver; INFEASIBLE is "no substitute hub").
CAPABILITY_OVERRIDE_REASONS = frozenset(
    {reasons.R_COMMIT_LOCK_OVERRIDE_L0, reasons.R_COMMIT_LOCK_OVERRIDE_L1, reasons.R_COMMIT_LOCK_INFEASIBLE}
)


def check_g19_override_evidence(
    obligation_id: str,
    reason_code: str | None,
    *,
    l2_instruction_active: bool,
    bank_capability_kw: float | None,
    committed_floor_kw: float,
    bank_capability_upper_kw: float | None = None,
    pq_capability_upper_kw: float | None = None,
    pq_floor_kw: float | None = None,
) -> CheckOutcome:
    """K13: an override reason code is a CLAIM by the engine; the guardian signs a reduction below the
    commitment lock only when its own independent reads back that claim up (review S3.3-2).

    - `R-COMMIT-LOCK-OVERRIDE-L2`: the guardian's own L2InstructionPort shows an active utility/ISO
      instruction for the bank.
    - `-L0`/`-L1`/`R-COMMIT-LOCK-INFEASIBLE`: the guardian's own capability read of the bank's hubs is
      below the bank's committed floor, i.e. no substitution within the bank could have kept the
      commitment. The bound used is `bank_capability_upper_kw` (hubs whose telemetry the guardian has not
      seen recently counted at their full rating; `None` means no read at all): a shortfall that exists
      only because hubs are unseen is not corroborated (`..._EVIDENCE_STALE`), so stale telemetry
      fails the claim closed instead of proving it. `bank_capability_kw` (unseen hubs at 0) only
      distinguishes that reason; when the upper bound is omitted it is used as-is. Both are the
      caller's DERATED capability (`core.limits.derated_power_bounds_kw`, the allocator's own bound), so a
      shortfall caused by SoC/temperature derating or a BMS limit is corroborated, not vetoed.
    - PQ eligibility (K14): for a PQ-sensitive obligation the allocator may only use hubs that pass the
      guardian's own G-24 asset conformance. `pq_capability_upper_kw` is that PQ-eligible capability
      (same upper-bound rule) and `pq_floor_kw` the bank's PQ-sensitive commitments: below it, the claim
      is corroborated too. Either None: no PQ evidence (the plain capability rule alone decides).

    Any other reason passes here: `check_g19_commitment_lock` already refuses it (and governs the
    config-gated `R-AS-RELEASE`)."""
    if reason_code == reasons.R_COMMIT_LOCK_OVERRIDE_L2:
        if l2_instruction_active:
            return CheckOutcome.passed("G-19")
        return CheckOutcome("G-19", False, "COMMIT_LOCK_OVERRIDE_L2_UNVERIFIED", obligation_id=obligation_id)
    if reason_code in CAPABILITY_OVERRIDE_REASONS:
        upper_kw = bank_capability_upper_kw if bank_capability_upper_kw is not None else bank_capability_kw
        if upper_kw is not None and upper_kw < committed_floor_kw - 1e-9:
            return CheckOutcome.passed("G-19")
        if (
            pq_capability_upper_kw is not None
            and pq_floor_kw is not None
            and pq_capability_upper_kw < pq_floor_kw - 1e-9
        ):
            return CheckOutcome.passed("G-19")
        stale = bank_capability_kw is not None and bank_capability_kw < committed_floor_kw - 1e-9
        reason = (
            "COMMIT_LOCK_OVERRIDE_EVIDENCE_STALE" if stale else "COMMIT_LOCK_OVERRIDE_INFEASIBLE_UNVERIFIED"
        )
        return CheckOutcome("G-19", False, reason, obligation_id=obligation_id)
    return CheckOutcome.passed("G-19")


#: K15 territory blocks an allocator may carry on a 0 kW grant for an obligation it must not serve from this
#: bank (`market.territory.check_territory`'s codes, plus the guardian's own G-33 code).
#: D-37: plus R-BANK-UNAVAILABLE-REGULATED-NO-CONTRACT (an UNAVAILABLE bank), corroborated the same way by the
#: guardian's own availability read (`GuardianService._territory_block`).
TERRITORY_BLOCK_REASONS = TERRITORY_REASONS | {reasons.R_TERRITORY_INELIGIBLE, reasons.R_BANK_UNAVAILABLE}


def check_g19_territory_block(obligation_id: str, *, guardian_block: str | None) -> CheckOutcome:
    """K13 x K15: a reduction claimed as territory-ineligible is signed only when the guardian's OWN territory
    check (`market.check_territory` on its own contract and zone reads, the G-33 predicate) also refuses
    serving this obligation from this bank. Serving it would be vetoed by G-33 anyway, so the reduction is
    the only admissible outcome; if the guardian finds the bank eligible, the claim is VETOED."""
    if guardian_block is not None:
        return CheckOutcome.passed("G-19")
    return CheckOutcome("G-19", False, "TERRITORY_INELIGIBLE_UNVERIFIED", obligation_id=obligation_id)


def check_g19_operator_override(
    obligation_id: str,
    *,
    manual_target_hubs: frozenset[str],
    capability_without_manual_upper_kw: float | None,
    committed_floor_kw: float,
) -> CheckOutcome:
    """K13 x manual operator setpoints: a reduction because a live operator target (MANUAL_TARGET) took hubs a
    commitment was served from is signed only if the guardian's OWN read shows such a target on this bank AND
    the bank's upper-bound capability without the operator-owned hubs is below the commitment floor (no
    substitute hub could have kept it). Otherwise VETO."""
    if not manual_target_hubs:
        return CheckOutcome("G-19", False, "OPERATOR_OVERRIDE_NO_LIVE_TARGET", obligation_id=obligation_id)
    if (
        capability_without_manual_upper_kw is None
        or capability_without_manual_upper_kw >= committed_floor_kw - 1e-9
    ):
        return CheckOutcome("G-19", False, "OPERATOR_OVERRIDE_UNVERIFIED", obligation_id=obligation_id)
    return CheckOutcome.passed("G-19")


def check_g35_mobile_charge(
    hub_id: str, p_kw_setpoint: float, *, is_mobile: bool, at_home_station: bool | None
) -> CheckOutcome:
    """G-35 (D-31): a MOBILE_STORAGE unit is never charged from the fleet or at a deployment site -- only at
    its home station, from that station's own connection. VETO any charging setpoint (p > 0) on a mobile unit
    unless the guardian's own read says it is at its home station; an unknown location counts as away (fail
    closed). Discharge and 0 kW are not G-35's concern."""
    if not is_mobile or p_kw_setpoint <= 1e-9 or at_home_station is True:
        return CheckOutcome.passed("G-35", hub_id=hub_id)
    return CheckOutcome("G-35", False, reasons.R_MOBILE_CHARGE_AWAY_FROM_HOME_STATION, hub_id)


def g19_lock_reason(reason_code: str | None) -> str | None:
    """The K13 lock exception a batch reason stands for. A best-effort partial grant after a mid-window
    SHORTFALL carries the shortfall reason (`core.reasons.LOCK_REASON_BY_SHORTFALL`); it is judged, and
    corroborated, exactly as the override it maps to. Every other reason is itself."""
    if reason_code is None:
        return None
    return reasons.LOCK_REASON_BY_SHORTFALL.get(reason_code, reason_code)


#: `og.service_profile.setpoint_source` of a measured closed-loop (need-basis) profile.
NEED_BASIS_SETPOINT_SOURCE = "MEASURED_FEEDBACK"


def g19_obligations_over_commitment(
    granted_kw: dict[str, Decimal], committed_kw: dict[str, Decimal], *, exclude: str
) -> list[str]:
    """Obligations (other than `exclude`) granted more on this bank this cycle than their own commitment
    here (0 kW for one with no commitment on the bank). Any such grant could only be using capacity
    reserved for another obligation, e.g. a need-basis obligation's unused reservation."""
    return sorted(
        key
        for key, kw in granted_kw.items()
        if key != exclude and kw > committed_kw.get(key, Decimal(0)) + Decimal("1e-9")
    )


def check_g19_need_basis(
    obligation_id: str, *, setpoint_source: str | None, borrowed_by: list[str]
) -> CheckOutcome:
    """K13 need basis (owner decision 2026-09-26): an `R-GRANT-CLOSED-LOOP` grant below the reserved
    maximum is signed only if (a) the guardian's own read of the obligation's service profile is
    `MEASURED_FEEDBACK` and (b) no other obligation on the bank is granted beyond its own commitment this
    cycle (the unused reservation stays locked, never reassigned). Otherwise VETO, as for any reduction."""
    if setpoint_source != NEED_BASIS_SETPOINT_SOURCE:
        return CheckOutcome(
            "G-19", False, "NEED_BASIS_PROFILE_NOT_MEASURED_FEEDBACK", obligation_id=obligation_id
        )
    if borrowed_by:
        return CheckOutcome("G-19", False, "NEED_BASIS_RESERVATION_REASSIGNED", obligation_id=obligation_id)
    return CheckOutcome.passed("G-19")


#: `og.obligation.service_type`s held at 0 kW with R-GRANT-AS-HOLD until deployed (og.as_deployment): an ERCOT
#: ancillary-service award, and a REGULATED_CAPACITY utility toll (D-29, deployed only per obligation) --
#: `core.services`, the one definition shared with the allocator and the K13 invariant.
AS_SERVICE_TYPE = ERCOT_AS_SERVICE_TYPE


def check_g19_as_hold(
    obligation_id: str, *, service_type: str | None, deployment_active: bool | None, borrowed_by: list[str]
) -> CheckOutcome:
    """K13 AS capacity hold (lead decision 2026-09-26, migration 0020): an `R-GRANT-AS-HOLD` grant below the
    commitment is signed only if the guardian's own reads show (a) the obligation is `ERCOT_AS`, (b) no
    deployment covering now is active for it (or for every AS award) -- while deployed it must deliver, and
    a reduction needs the normal override/shortfall reasons -- and (c) no other obligation on the bank is
    granted beyond its own commitment (the held reservation is not being used). `deployment_active` None
    means no read: VETO."""
    if service_type not in HOLD_SERVICE_TYPES:
        return CheckOutcome("G-19", False, "AS_HOLD_NOT_AN_AS_AWARD", obligation_id=obligation_id)
    if deployment_active is None or deployment_active:
        return CheckOutcome("G-19", False, "AS_HOLD_WHILE_DEPLOYED", obligation_id=obligation_id)
    if borrowed_by:
        return CheckOutcome("G-19", False, "AS_HOLD_RESERVATION_REASSIGNED", obligation_id=obligation_id)
    return CheckOutcome.passed("G-19")


def check_g20_clock_quality(offset_ms: float, max_offset_ms: float) -> CheckOutcome:
    """K12: refuse to sign when the guardian's own clock offset from NTP exceeds its limit. Runs first,
    before every other check (02a S6.2a) -- every freshness/lease check downstream depends on this
    clock."""
    if not clock_offset_ok(offset_ms, max_offset_ms):
        return CheckOutcome("G-20", False, "CLOCK_OFFSET_EXCEEDED")
    return CheckOutcome.passed("G-20")


def obligation_totals(items: list[ProposedItem]) -> dict[str, Decimal]:
    """Sum `obligation_granted_kw` per obligation across a batch's items -- each item carries its hub's
    share of the obligation's grant, so the sum is the batch's claimed grant per obligation, which G-19
    compares against guardian's own independently-read frozen/prior kw."""
    totals: dict[str, Decimal] = {}
    for item in items:
        if item.obligation_id is None or item.obligation_granted_kw is None:
            continue
        key = str(item.obligation_id)
        totals[key] = totals.get(key, Decimal(0)) + item.obligation_granted_kw
    return totals
