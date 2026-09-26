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

from opengrid.core import limits as core_limits
from opengrid.core import reasons
from opengrid.core.physics import BankParams, HubParams
from opengrid.core.timeutil import check_command_freshness, clock_offset_ok
from opengrid.guardian.ports import L2Instruction, ProposedItem


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
    """K4: |P| bound per hub."""
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
      distinguishes that reason; when the upper bound is omitted it is used as-is.

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
        stale = bank_capability_kw is not None and bank_capability_kw < committed_floor_kw - 1e-9
        reason = (
            "COMMIT_LOCK_OVERRIDE_EVIDENCE_STALE" if stale else "COMMIT_LOCK_OVERRIDE_INFEASIBLE_UNVERIFIED"
        )
        return CheckOutcome("G-19", False, reason, obligation_id=obligation_id)
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
