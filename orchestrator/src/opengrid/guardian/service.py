"""`GuardianService`: the sole-signer decision engine (02a S6). Pure orchestration over the ports in
`opengrid.guardian.ports` and the pure checks in `opengrid.guardian.checks` -- no direct Postgres/MQTT
import here (BUILD.md S5a "pure logic separated from I/O"); `opengrid.guardian.repo`/`mqtt_io` supply
real port implementations for `main.py`, and tests supply in-memory fakes.

Ordering (02a S6.2a): G-20 (clock quality) runs first and alone -- every other check's freshness/lease
arithmetic depends on the guardian's own clock. Everything else accumulates independently so a single
veto never hides another (the trace records every reason, not just the first).
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import OrderedDict
from collections.abc import Iterable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from prometheus_client import Counter

from opengrid.core import limits as core_limits
from opengrid.core import reasons
from opengrid.core.crypto import sha256_hex_of_json, sign_payload
from opengrid.core.models.engine import CommandBatchRow, Verdict, VerdictOutcome
from opengrid.core.models.market import ERCOT_COMPETITIVE
from opengrid.core.models.mqtt import CommandBatch, StopEvent
from opengrid.core.models.pq import CalibrationBounds as WireCalibrationBounds
from opengrid.core.models.pq import CalibrationCommand, CalibrationCorrection
from opengrid.core.physics import bank_capability, hub_ramp_kw_per_s, hub_sustainable_discharge_kw
from opengrid.core.pq.constants import CALIBRATION_MIN_INTERVAL_S_DEFAULT
from opengrid.guardian import checks, flow_checks, pq_checks, stop_release
from opengrid.guardian.checks import CheckOutcome
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.escalation import BatchOutcome, vetoed_command_count
from opengrid.guardian.ports import (
    ActiveObligation,
    AggregateFlow,
    BankSnapshot,
    GuardianPorts,
    HubSite,
    HubSnapshot,
    ProposedBatch,
    ProposedItem,
    ReleaseRequest,
    SafeStopScope,
    ServiceTransformer,
)
from opengrid.guardian.pq_ports import (
    CalibrationFleetUsage,
    CalibrationLedgerPort,
    ProposedCalibrationCommand,
)
from opengrid.market.territory import MarketRef, check_territory, territory_of_zone
from opengrid.platform.metrics import guardian_clock_offset_ms, guardian_verdicts_total

logger = logging.getLogger(__name__)

_MAX_CYCLE_HISTORY = 64  # bound the in-memory ramp accumulators; MVP-S runs a 2s cycle, never GC-free
_Staged = list[tuple[OrderedDict[Any, float], Any, float]]
#: The accumulator deltas (store, key, delta) of the batch being evaluated; None outside `evaluate_and_sign`.
_STAGED: ContextVar[_Staged | None] = ContextVar("guardian_staged_accumulators", default=None)
_MAX_EVALUATED_PROPOSALS = 256
#: K12: raised while G-20 holds every signature.
CLOCK_ALERT_RULE = "ALR-CLOCK-QUALITY"  # > one tick's pending batches (main.py fetches at most 50 per tick)
_ITEM_LEVEL_RULES = frozenset(
    {"G-01", "G-01-ENERGY", "G-02", "G-04", "G-24", "G-26", "G-27", "G-31", "G-33", "G-35"}
)
#: G-27: hubs checked as a group of one because they have no service-transformer mapping.
XFMR_UNMAPPED_ALERT_RULE = "ALR-XFMR-UNMAPPED"
#: A bank with no feeder mapping: G-06/G-28/G-32 are skipped for it (vetoed with fail_closed_missing_topology).
BANK_UNMAPPED_ALERT_RULE = "ALR-BANK-UNMAPPED-TOPOLOGY"
#: A bank with no substation topology (og.asset mapping or og.substation_limit row): G-29 is skipped for it
#: (vetoed with fail_closed_missing_topology).
SUBSTATION_UNMAPPED_ALERT_RULE = "ALR-SUBSTATION-UNMAPPED-TOPOLOGY"
#: K11: a verdict's GUARDIAN_VERDICT trace row could not be written (the verdict itself stands).
TRACE_VERDICT_ALERT_RULE = "ALR-TRACE-VERDICT-WRITE-FAILED"
guardian_trace_verdict_failures_total = Counter(
    "og_guardian_trace_verdict_failures_total",
    "Guardian verdicts whose GUARDIAN_VERDICT audit trace row could not be written (K11).",
)


@dataclass(frozen=True, slots=True)
class _OverrideEvidence:
    """What the guardian itself reads when a batch claims a K13 override (see `_override_evidence`)."""

    l2_instruction_active: bool
    bank_capability_kw: float | None  # from member hubs the guardian sees live; stale ones count 0
    bank_capability_upper_kw: float | None  # the same with every stale hub at its full rating
    #: the upper bound over only the hubs passing G-24 for a PQ-sensitive obligation; None when the bank has
    #: no active non-default PQ envelope (or no PQ reads)
    pq_capability_upper_kw: float | None = None
    #: hubs a live operator target (MANUAL_TARGET) owns on this bank, and the bank's upper-bound capability
    #: without them (None: no target, or no read)
    manual_target_hubs: frozenset[str] = frozenset()
    capability_without_manual_upper_kw: float | None = None


#: G-24's required ride-through class for a PQ-sensitive (non-default-envelope) obligation: the only such
#: profile defined so far, DATA_CENTER (config/service_profiles/data_center.toml, S4.b), accepts Category III
#: only. `PqEnvelopeLimits` does not carry the class; revisit when a second sensitive profile exists.
SENSITIVE_RIDE_THROUGH_CLASS = "CATEGORY_III"


#: Cap on violations recorded per verdict trace row (a 50-hub batch can fail one item rule per hub).
_MAX_TRACED_VIOLATIONS = 20


def _violation_summary(violations: list[CheckOutcome]) -> list[dict[str, str | None]]:
    """Distinct `(rule, reason)` violations, first example hub/obligation for each, capped."""
    seen: dict[tuple[str, str | None], dict[str, str | None]] = {}
    for v in violations:
        key = (v.rule_id, v.reason)
        if key not in seen:
            seen[key] = {
                "rule_id": v.rule_id,
                "reason": v.reason,
                "hub_id": v.hub_id,
                "obligation_id": v.obligation_id,
            }
    return list(seen.values())[:_MAX_TRACED_VIOLATIONS]


def manual_charge_territory_exempt(
    proposal: ProposedBatch, outcomes: list[CheckOutcome]
) -> list[CheckOutcome]:
    """D-29c: in a regulated/toll territory the utility makes the DISCHARGE calls, so an operator's manual
    discharge there stays vetoed by G-33 (headroom market) unless tied to that utility's obligation -- but a
    manual CHARGE or a 0 kW hold (owner schedule, maintenance) is allowed. Drops the G-33 outcome of a hub
    whose no-obligation items are all R-MANUAL-RAMP at setpoint >= 0; every other outcome (G-34, obligation
    items, manual discharge, headroom) is kept as the territory check made it."""
    unobligated: dict[str, list[ProposedItem]] = {}
    for item in proposal.items:
        if item.obligation_id is None:
            unobligated.setdefault(item.hub_id, []).append(item)
    exempt = {
        hub_id
        for hub_id, items in unobligated.items()
        if all(i.reason_code == reasons.R_MANUAL_RAMP and i.p_kw_setpoint >= 0 for i in items)
    }
    return [
        o
        for o in outcomes
        if not (
            o.rule_id == "G-33"
            and o.obligation_id is None
            and o.hub_id in exempt
            # D-37: an UNAVAILABLE bank (regulated, no contract) is an idle hold -- no manual charge either.
            and o.reason != reasons.R_BANK_UNAVAILABLE
        )
    ]


def vetoed_hub_ids(violations: list[CheckOutcome]) -> list[str]:
    """Every hub with an item-level veto (`_ITEM_LEVEL_RULES`), sorted and uncapped -- the full list
    DISPATCH's K4 re-solve excludes (`R-HUB-VETO-EXCLUDED`). `_violation_summary` keeps only one example hub
    per (rule, reason); this is the complete set."""
    return sorted({v.hub_id for v in violations if v.rule_id in _ITEM_LEVEL_RULES and v.hub_id is not None})


def _iso_z(dt: datetime) -> str:
    """RFC 3339 UTC with a literal 'Z' suffix, per interfaces/crypto.md S1."""
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class GuardianService:
    ports: GuardianPorts
    config: GuardianConfig
    signing_seed: bytes
    now_fn: Any = field(default=lambda: datetime.now(UTC))
    monotonic_fn: Any = field(default=time.monotonic)

    _fleet_delta_by_cycle: OrderedDict[str, float] = field(default_factory=OrderedDict, init=False)
    _feeder_delta_by_cycle: OrderedDict[tuple[str, str], float] = field(
        default_factory=OrderedDict, init=False
    )
    _gross_step_by_cycle: OrderedDict[str, float] = field(default_factory=OrderedDict, init=False)
    _flow_delta_by_cycle: OrderedDict[tuple[str, str], float] = field(default_factory=OrderedDict, init=False)

    _evaluated: OrderedDict[UUID, ProposedBatch] = field(default_factory=OrderedDict, init=False)
    _clock_alert_open: bool = field(default=False, init=False)
    _violations: OrderedDict[UUID, list[CheckOutcome]] = field(default_factory=OrderedDict, init=False)
    #: G-04 anchor: per hub, the last net setpoint this guardian SIGNED, when, and its lease expiry
    _last_signed: dict[str, tuple[float, datetime, datetime]] = field(default_factory=dict, init=False)

    def _record_signed_setpoints(self, command_batch_id: UUID, signed_at: datetime) -> None:
        """Remember each hub's signed net setpoint (G-04's utility-scale anchor, `checks.g04_anchor_kw`)."""
        proposal = self._evaluated.get(command_batch_id)
        if proposal is None:
            return
        for item in checks.hub_setpoints(proposal.items):
            self._last_signed[item.hub_id] = (item.p_kw_setpoint, signed_at, proposal.expires_at)

    def seed_signed_anchors(self, anchors: dict[str, tuple[float, datetime, datetime]]) -> None:
        """r3.4.3 HIGH-A: after a restart, reload the last signed setpoint per hub whose lease is still live
        (`repo.load_signed_anchors`, from og.verdict PASS + the batch's RT_ALLOCATION trace), so G-04 keeps the
        same anchor as the engine instead of falling back to stale telemetry. Never overwrites a newer entry."""
        for hub_id, entry in anchors.items():
            current = self._last_signed.get(hub_id)
            if current is None or current[1] < entry[1]:
                self._last_signed[hub_id] = entry

    def _drop_signed_anchors(self, hub_ids: Iterable[str]) -> None:
        """Contract with DISPATCH (r3.4.3): a veto naming a hub, or a stop engaged over it since the signature,
        ends that hub's signed anchor on both sides -- both then anchor on telemetry."""
        for hub_id in hub_ids:
            self._last_signed.pop(hub_id, None)

    async def _drop_anchors_stopped_since_signing(self, bank_id: str, hub_ids: Iterable[str]) -> None:
        """A FLEET/ZONE/BANK stop ENGAGE at or after a hub's signature means the hub ramped to 0 kW: its signed
        setpoint is no longer where it is (a release inside the lease would otherwise let a step from the
        pre-stop setpoint pass G-04 while the hub sits at 0)."""
        signed = {h: self._last_signed[h][1] for h in hub_ids if h in self._last_signed}
        if not signed:
            return
        port = self.ports.safe_stop
        scopes: list[tuple[SafeStopScope, str]] = [("FLEET", "FLEET"), ("BANK", bank_id)]
        zone = self.ports.zones_by_bank.get(bank_id)
        if zone:
            scopes.append(("ZONE", zone))
        engaged: list[datetime] = []
        for kind, ref in scopes:
            try:
                at = await port.last_engaged_at(kind, ref)
            except Exception:
                logger.exception("stop read failed: signed anchors dropped", extra={"bank_id": bank_id})
                self._drop_signed_anchors(signed)
                return
            if at is not None:
                engaged.append(at)
        if engaged:
            latest = max(engaged)
            self._drop_signed_anchors([h for h, signed_at in signed.items() if latest >= signed_at])

    def _g04_anchor(self, hub_id: str, hub: HubSnapshot) -> checks.G04Anchor:
        signed = self._last_signed.get(hub_id)
        return checks.g04_anchor_kw(
            prev_telemetry_kw=hub.prev_p_kw,
            telemetry_ts=hub.telemetry_at,
            last_signed_kw=signed[0] if signed else None,
            last_signed_at=signed[1] if signed else None,
            lease_expires_at=signed[2] if signed else None,
            now=self.now_fn(),
            utility_scale=hub.params.utility_scale,
            cycle_interval_s=self.config.cycle_interval_s,
        )

    async def evaluate_and_sign(self, batch: CommandBatchRow) -> Verdict:
        """Run every applicable G-check against independently-read state; PASS signs, any veto returns
        VETOED/PARTLY_VETOED, and a processing timeout (or a G-20 clock-quality failure) returns
        TIMEOUT -- all three are holds, never a stop (K7).

        The per-cycle accumulators (G-05 fleet and gross step, G-06/G-32 feeder, G-28/29/30 flows) only
        ever count batches that were signed: this batch's deltas are staged while it is checked and
        committed on PASS, dropped on VETOED, PARTLY_VETOED or TIMEOUT."""
        token = _STAGED.set([])
        try:
            verdict = await self._evaluate(batch)
            staged = _STAGED.get()
            if verdict.outcome == "PASS" and staged:
                self._commit_accumulators(staged)
            return verdict
        finally:
            _STAGED.reset(token)

    async def _evaluate(self, batch: CommandBatchRow) -> Verdict:
        started = self.monotonic_fn()
        verdict_id = uuid4()

        if not await self._clock_ok():
            return await self._finalize(
                batch, verdict_id, started, outcome="TIMEOUT", vetoed_rule_ids=["G-20"]
            )

        timeout_s = 2 * self.config.verdict_timeout_ms / 1000.0
        try:
            violations = await asyncio.wait_for(self._run_checks(batch), timeout=timeout_s)
        except TimeoutError:
            return await self._finalize(batch, verdict_id, started, outcome="TIMEOUT", vetoed_rule_ids=[])

        if not violations:
            return await self._finalize(batch, verdict_id, started, outcome="PASS", vetoed_rule_ids=[])

        outcome = self._classify(violations)
        rule_ids = sorted({v.rule_id for v in violations})
        return await self._finalize(
            batch, verdict_id, started, outcome=outcome, vetoed_rule_ids=rule_ids, violations=violations
        )

    def batch_outcome(self, verdict: Verdict) -> BatchOutcome | None:
        """ES06-S04: this verdict as the escalation counter sees it (commands, and how many an explicit
        invariant veto refused). None when the batch's proposal was never read (nothing to count)."""
        proposal = self._evaluated.get(verdict.command_batch_id)
        if proposal is None:
            return None
        hubs = [item.hub_id for item in proposal.items]
        violations = self._violations.get(verdict.command_batch_id, [])
        vetoed = vetoed_command_count(
            verdict.outcome, verdict.vetoed_rule_ids, hubs, (v.hub_id for v in violations)
        )
        return BatchOutcome(bank_id=proposal.bank_id, commands=len(hubs), vetoed_commands=vetoed)

    def evaluated_proposal(self, command_batch_id: UUID) -> ProposedBatch | None:
        """The exact proposal `evaluate_and_sign` checked for this batch. The caller publishes THIS,
        never a second read of the pre-image: a re-read could return different items than were
        verified (a later RT_ALLOCATION row with the same `command_batch_id`)."""
        return self._evaluated.get(command_batch_id)

    async def _clock_ok(self) -> bool:
        """G-20/K12, fail-closed: a clock port that raises, or reports a non-finite offset, is out of
        limit (the adapters report `inf` for every read failure or unsynchronised clock)."""
        try:
            offset_ms = float(await self.ports.clock.offset_from_ntp_ms())
        except Exception:
            logger.exception("guardian clock-quality read failed; treating as out of limit (K12)")
            offset_ms = math.inf
        if math.isnan(offset_ms):
            offset_ms = math.inf
        guardian_clock_offset_ms.set(offset_ms)
        ok = checks.check_g20_clock_quality(offset_ms, self.config.clock_offset_max_ms).ok
        await self._clock_alert(ok, offset_ms)
        return ok

    async def _clock_alert(self, ok: bool, offset_ms: float) -> None:
        """ALR-CLOCK-QUALITY: raised when G-20 starts holding (the guardian signs nothing), cleared when the
        clock is back in limit. Only on transitions, and best-effort: the hold stands either way."""
        alerts = self.ports.alerts
        if alerts is None or ok != self._clock_alert_open:
            return
        try:
            if ok:
                await alerts.clear_alert(CLOCK_ALERT_RULE, CLOCK_ALERT_RULE)
            else:
                await alerts.raise_alert(
                    CLOCK_ALERT_RULE,
                    "critical",
                    "guardian clock quality out of limit: signing held (G-20, K12)",
                    CLOCK_ALERT_RULE,
                    {
                        "offset_ms": offset_ms if math.isfinite(offset_ms) else None,
                        "limit_ms": self.config.clock_offset_max_ms,
                    },
                )
            self._clock_alert_open = not ok
        except Exception:
            logger.exception("failed to update %s", CLOCK_ALERT_RULE)

    async def _run_checks(self, batch: CommandBatchRow) -> list[CheckOutcome]:
        violations: list[CheckOutcome] = []

        proposal = await self.ports.proposals.fetch(batch.command_batch_id)
        if proposal is None:
            return [CheckOutcome("G-14", False, "PROPOSAL_NOT_FOUND")]
        if proposal.command_batch_id != batch.command_batch_id:
            return [CheckOutcome("G-14", False, "PROPOSAL_BATCH_ID_MISMATCH")]
        self._evaluated[batch.command_batch_id] = proposal
        self._evaluated.move_to_end(batch.command_batch_id)
        while len(self._evaluated) > _MAX_EVALUATED_PROPOSALS:
            self._evaluated.popitem(last=False)

        if await self._is_stop_engaged(proposal.bank_id):
            violations.append(CheckOutcome("SAFE_STOP", False, "SAFE_STOP_ENGAGED", hub_id=proposal.bank_id))

        # K10/G-14: the durable pre-image is `og.trace.trace_id == batch.trace_pre_image_id` (the DDL's
        # own FK, 02a S1.11) -- NOT `batch.command_batch_id` itself, which is a different id entirely
        # (root cause of the "100% of verdicts VETO on G-14" incident, qa/merge-notes.md S17: this used
        # to check `exists_preimage(batch.command_batch_id)`, which can only ever coincidentally match a
        # trace row's own randomly-generated `trace_id`). A batch with no `trace_pre_image_id` at all
        # was never traced and fails closed without a wasted DB round-trip.
        preimage_exists = batch.trace_pre_image_id is not None and await self.ports.trace.exists_preimage(
            batch.trace_pre_image_id
        )
        g14 = checks.check_g14_trace_preimage(preimage_exists)
        if not g14.ok:
            violations.append(g14)
            return violations  # nothing downstream is trustworthy without the pre-image (K10)

        ledger_version = await self.ports.ledger.ledger_version()
        g09 = checks.check_g09_ledger_version(batch.ledger_version, ledger_version)
        if not g09.ok:
            violations.append(g09)

        last_epoch, last_seq = await self.ports.leases.last_accepted(proposal.bank_id)
        g13 = checks.check_g13_freshness(
            epoch=proposal.epoch,
            seq=proposal.seq,
            last_accepted_epoch=last_epoch,
            last_accepted_seq=last_seq,
            issued_at=proposal.issued_at,
            expires_at=proposal.expires_at,
            now=self.now_fn(),
        )
        if not g13.ok:
            violations.append(g13)

        violations.extend(await self._check_hubs_and_bank(proposal))
        violations.extend(manual_charge_territory_exempt(proposal, await self._check_territory(proposal)))
        violations.extend(await self._check_mobile_units(proposal))
        violations.extend(await self._check_power_quality(proposal))
        violations.extend(await self._check_l2_boundary(proposal))
        violations.extend(await self._check_commitment_lock(proposal))
        return violations

    async def _is_stop_engaged(self, bank_id: str) -> bool:
        if await self.ports.safe_stop.is_stopped("FLEET", "FLEET"):
            return True
        zone = self.ports.zones_by_bank.get(bank_id)
        if zone and await self.ports.safe_stop.is_stopped("ZONE", zone):
            return True
        return await self.ports.safe_stop.is_stopped("BANK", bank_id)

    async def _check_hubs_and_bank(self, proposal: ProposedBatch) -> list[CheckOutcome]:
        violations: list[CheckOutcome] = []
        fleet_delta_kw = 0.0  # measured change vs telemetry: bank loading (G-03) and the flow checks
        ramp_delta_kw = 0.0  # the step for the RATE checks (G-05, G-06, G-32), on G-04's anchor
        gross_step_kw = 0.0
        additional_charge_kw = 0.0
        snapshots: dict[str, HubSnapshot] = {}
        sites: dict[str, HubSite | None] = {}
        topology = self.ports.topology
        policy = self._flow_policy()

        lease_ttl_s = max((proposal.expires_at - self.now_fn()).total_seconds(), 0.0)
        lease_ttl_h = lease_ttl_s / 3600.0

        # A hub may carry several items (one per obligation) and executes their SUM: every hub-level limit
        # (reserve, energy over the lease, power, meter, ramp) is checked on that sum, never per item.
        hub_items = checks.hub_setpoints(proposal.items)
        await self._drop_anchors_stopped_since_signing(proposal.bank_id, [i.hub_id for i in hub_items])
        for item in hub_items:
            hub = await self.ports.hubs.snapshot(item.hub_id)
            if hub is None:
                violations.append(CheckOutcome("G-01", False, "HUB_UNKNOWN", item.hub_id))
                continue
            snapshots[item.hub_id] = hub
            site = await topology.hub_site(item.hub_id) if topology is not None else None
            sites[item.hub_id] = site
            if hub.health != "online" and item.p_kw_setpoint < 0:
                # K1 (NOTICES #1): a stale or faulted SoC means zero discharge -- never sign on the
                # last reading the guardian happened to see.
                violations.append(CheckOutcome("G-01", False, "HUB_SOC_NOT_LIVE", item.hub_id))
                continue

            g01 = checks.check_g01_reserve(
                item, hub.params, hub.soc_kwh, margin_pct=self.config.reserve_margin_pct
            )
            if not g01.ok:
                violations.append(g01)
            g01_energy = checks.check_g01_energy_lease(
                item, hub.params, hub.soc_kwh, lease_ttl_h, margin_pct=self.config.reserve_margin_pct
            )
            if not g01_energy.ok:
                violations.append(g01_energy)
            violations.extend(self._check_hub_power(item, hub, site, lease_ttl_s, policy))
            if topology is not None:
                g26 = flow_checks.check_g26_home_meter(item, hub, site, policy)
                if not g26.ok:
                    violations.append(g26)

            # telemetry, or a utility-scale hub's last signed setpoint
            anchor = self._g04_anchor(item.hub_id, hub)
            g04 = checks.check_g04_hub_ramp(item, anchor.kw, anchor.dt_s, hub_ramp_kw_per_s(hub.params))
            if not g04.ok:
                violations.append(g04)

            fleet_delta_kw += item.p_kw_setpoint - hub.prev_p_kw
            ramp_step_kw = checks.ramp_step_kw(item.p_kw_setpoint, anchor)
            ramp_delta_kw += ramp_step_kw
            gross_step_kw += abs(ramp_step_kw)
            additional_charge_kw += max(item.p_kw_setpoint, 0.0) - max(hub.prev_p_kw, 0.0)

        bank = await self.ports.banks.snapshot(proposal.bank_id)
        if bank is None:
            violations.append(CheckOutcome("G-03", False, "BANK_UNKNOWN", proposal.bank_id))
        else:
            violations.extend(
                self._check_bank_loading(proposal.bank_id, bank, additional_charge_kw, fleet_delta_kw)
            )

        cumulative_fleet = self._accumulate(self._fleet_delta_by_cycle, proposal.cycle_id, ramp_delta_kw)
        g05 = checks.check_g05_fleet_ramp(
            cumulative_fleet,
            self.config.cycle_interval_s,
            is_firm_event=proposal.is_firm_event,
            discretionary_cap_kw_per_min=self.config.discretionary_ramp_cap_kw_per_min,
            non_firm_cap_kw_per_min=self.config.non_firm_ramp_cap_kw_per_min,
        )
        if not g05.ok:
            violations.append(g05)
        # K4 stagger: the GROSS step of every hub moved in this tick, across every bank's batch.
        cumulative_gross = self._accumulate(self._gross_step_by_cycle, proposal.cycle_id, gross_step_kw)
        sync = core_limits.check_synchronized_step(
            cumulative_gross,
            self.config.cycle_interval_s,
            discretionary_cap_kw_per_min=self.config.discretionary_ramp_cap_kw_per_min,
        )
        if not sync.ok and gross_step_kw > 0:
            violations.append(CheckOutcome("G-05", False, sync.reason))

        if bank is not None and bank.feeder_id is not None:
            feeder_key = (proposal.cycle_id, bank.feeder_id)
            cumulative_feeder = self._accumulate(self._feeder_delta_by_cycle, feeder_key, ramp_delta_kw)
            ceiling = bank.feeder_ceiling_kw_per_min or self.config.feeder_ramp_ceiling_kw_per_min.get(
                bank.feeder_id, self.config.default_feeder_ramp_ceiling_kw_per_min
            )
            g06 = checks.check_g06_feeder_ramp(
                cumulative_feeder, self.config.cycle_interval_s, ceiling, is_firm_event=proposal.is_firm_event
            )
            if not g06.ok:
                violations.append(g06)
            if not proposal.is_firm_event and topology is not None:
                # F6/G-32: the feeder ramp binds non-firm steps too.
                g32 = core_limits.check_feeder_ramp(
                    cumulative_feeder,
                    self.config.cycle_interval_s,
                    ceiling,
                    reason=reasons.R_FEEDER_RAMP_NON_FIRM,
                )
                if not g32.ok:
                    violations.append(CheckOutcome("G-32", False, g32.reason, bank.feeder_id))

        if topology is not None and bank is not None and bank.feeder_id is None:
            violations.extend(await self._check_unmapped_bank(proposal.bank_id, hub_items, snapshots))

        if topology is not None:
            violations.extend(
                await self._check_flows(proposal, hub_items, bank, snapshots, sites, fleet_delta_kw, policy)
            )
        return violations

    async def _check_unmapped_bank(
        self,
        bank_id: str,
        hub_items: list[ProposedItem],
        snapshots: dict[str, HubSnapshot],
        *,
        rule_id: str = flow_checks.G28,
        reason: str = flow_checks.BANK_TOPOLOGY_UNMAPPED,
        alert_rule: str = BANK_UNMAPPED_ALERT_RULE,
        summary: str = "bank has no feeder mapping: G-06/G-28/G-32 are not evaluated for it (09 S2.6)",
    ) -> list[CheckOutcome]:
        """A bank whose feeder (G-06/G-28/G-32) or substation (G-29) topology is missing: always the alert (a
        warning, deduped per bank by the alert port), and with `fail_closed_missing_topology` only relief is
        signed -- |net setpoint| no larger and on the same side of zero (09 S2.6)."""
        await self._alert_missing_topology(alert_rule, summary, bank_id)
        if not self.config.flow_fail_closed_missing_topology:
            return []
        seen = [item for item in hub_items if item.hub_id in snapshots]
        prev = sum(snapshots[item.hub_id].prev_p_kw for item in seen)
        new = sum(item.p_kw_setpoint for item in seen)
        outcome = flow_checks.check_unmapped_bank(bank_id, prev, new, rule_id=rule_id, reason=reason)
        return [] if outcome.ok else [outcome]

    async def _alert_missing_topology(self, alert_rule: str, summary: str, bank_id: str) -> None:
        alerts = self.ports.alerts
        if alerts is None:
            return
        try:
            await alerts.raise_alert(
                alert_rule,
                "warning",
                summary,
                bank_id,
                {
                    "bank_id": bank_id,
                    "fail_closed_missing_topology": self.config.flow_fail_closed_missing_topology,
                },
            )
        except Exception:
            logger.exception("failed to raise %s", alert_rule)

    def _flow_policy(self) -> flow_checks.FlowPolicy:
        c = self.config
        return flow_checks.FlowPolicy(
            telemetry_required=c.flow_telemetry_required,
            max_age_s=c.flow_max_age_s,
            unknown_temp_factor=c.unknown_temp_factor,
            load_drop_kw=c.load_drop_kw,
            inverter_cap_kw=c.inverter_cap_kw,
            default_pv_rated_kw=c.default_pv_rated_kw,
            default_service_kw=c.default_service_kw,
            xfmr_forward_pct=c.xfmr_forward_pct,
            xfmr_reverse_pct=c.xfmr_reverse_pct,
            xfmr_max_stale_fraction=c.xfmr_max_stale_fraction,
            unmapped_xfmr_kva_per_home=c.unmapped_xfmr_kva_per_home,
        )

    def _check_hub_power(
        self,
        item: ProposedItem,
        hub: HubSnapshot,
        site: HubSite | None,
        lease_ttl_s: float,
        policy: flow_checks.FlowPolicy,
    ) -> list[CheckOutcome]:
        """G-02 (derated P_max(SoC, T), continuous) and G-31 (above continuous only within the peak allowance,
        F5). An above-continuous setpoint is G-31's alone: G-02 then bounds it by the derated PEAK."""
        continuous = core_limits.continuous_power_kw(hub.params, inverter_cap_kw=self.config.inverter_cap_kw)
        peak_kw: float | None = None
        if self.ports.topology is not None and abs(item.p_kw_setpoint) > continuous + 1e-9:
            g31 = flow_checks.check_g31_peak(item, hub, site, lease_ttl_s, policy)
            if not g31.ok:
                return [g31]
            peak_kw = site.peak_kw if site is not None else None
        g02 = flow_checks.check_g02_derated(item, hub, policy, peak_kw=peak_kw)
        return [] if g02.ok else [g02]

    async def _check_flows(
        self,
        proposal: ProposedBatch,
        hub_items: list[ProposedItem],
        bank: BankSnapshot | None,
        snapshots: dict[str, HubSnapshot],
        sites: dict[str, HubSite | None],
        bank_delta_kw: float,
        policy: flow_checks.FlowPolicy,
    ) -> list[CheckOutcome]:
        """G-27 (group), G-28, G-29, G-30 on the guardian's own reads (09 S2.6)."""
        topology = self.ports.topology
        if topology is None:
            return []
        violations = await self._check_transformers(proposal.bank_id, hub_items, snapshots, sites, policy)
        age = self.config.flow_max_age_s
        feeder_id = bank.feeder_id if bank is not None else None
        aggregates: list[tuple[str, AggregateFlow | None, str, str]] = [
            (
                "G-28",
                await topology.feeder_flow(feeder_id) if feeder_id is not None else None,
                reasons.R_FEEDER_REVERSE_FLOW,
                reasons.R_FEEDER_THERMAL_LIMIT,
            ),
            (
                "G-29",
                await topology.substation_flow(proposal.bank_id),
                reasons.R_SUBSTATION_LIMIT,
                reasons.R_SUBSTATION_LIMIT,
            ),
            (
                "G-30",
                await topology.territory_flow(proposal.bank_id),
                reasons.R_TERRITORY_EXPORT,
                reasons.R_TERRITORY_EXPORT,
            ),
        ]
        if aggregates[1][1] is None:
            violations.extend(
                await self._check_unmapped_bank(
                    proposal.bank_id,
                    hub_items,
                    snapshots,
                    rule_id="G-29",
                    reason=flow_checks.SUBSTATION_TOPOLOGY_UNMAPPED,
                    alert_rule=SUBSTATION_UNMAPPED_ALERT_RULE,
                    summary="bank has no substation mapping or limits: G-29 is not evaluated for it (09 S2.6)",
                )
            )
        for rule_id, flow, reverse_reason, forward_reason in aggregates:
            if flow is None:
                continue
            key = (proposal.cycle_id, f"{rule_id}:{flow.ref}")
            prior = self._flow_delta_by_cycle.get(key, 0.0)
            cumulative = self._accumulate(self._flow_delta_by_cycle, key, bank_delta_kw)
            outcome = flow_checks.check_aggregate_flow(
                rule_id,
                flow,
                prior,
                cumulative,
                max_age_s=age,
                reverse_reason=reverse_reason,
                forward_reason=forward_reason,
                ref=flow.ref,
            )
            if not outcome.ok:
                violations.append(outcome)
        poi = await topology.poi_limit(proposal.bank_id)
        if poi is not None:
            g29 = flow_checks.check_g29_poi(poi, sum(item.p_kw_setpoint for item in proposal.items))
            if not g29.ok:
                violations.append(g29)
        return violations

    async def _check_transformers(
        self,
        bank_id: str,
        hub_items: list[ProposedItem],
        snapshots: dict[str, HubSnapshot],
        sites: dict[str, HubSite | None],
        policy: flow_checks.FlowPolicy,
    ) -> list[CheckOutcome]:
        """G-27: every service transformer the batch touches, over ALL its members' meters. A violation vetoes
        every item under that transformer (group-level); an unmapped hub is a group of one at the default
        per-home rating (and ALR-XFMR-UNMAPPED)."""
        topology = self.ports.topology
        if topology is None:
            return []
        groups: dict[str, tuple[ServiceTransformer, list[ProposedItem]]] = {}
        unmapped = False
        for item in hub_items:
            hub = snapshots.get(item.hub_id)
            if hub is None:
                continue
            site = sites.get(item.hub_id)
            transformer = (
                await topology.transformer(site.transformer_id)
                if site is not None and site.transformer_id is not None
                else None
            )
            if transformer is None:
                unmapped = True
                transformer = ServiceTransformer(
                    f"unmapped:{item.hub_id}", policy.unmapped_xfmr_kva_per_home, (item.hub_id,)
                )
            groups.setdefault(transformer.transformer_id, (transformer, []))[1].append(item)
        if unmapped:
            await self._alert_unmapped_transformer(bank_id)

        violations: list[CheckOutcome] = []
        for transformer, items in groups.values():
            members: dict[str, flow_checks.TransformerMember] = {}
            for hub_id in transformer.members:
                hub = snapshots.get(hub_id) or await self.ports.hubs.snapshot(hub_id)
                site = sites[hub_id] if hub_id in sites else await topology.hub_site(hub_id)
                members[hub_id] = flow_checks.TransformerMember(hub, site)
            delta = sum(item.p_kw_setpoint - snapshots[item.hub_id].prev_p_kw for item in items)
            ok, reason = flow_checks.check_g27_transformer(transformer, members, delta, policy)
            if not ok:
                violations.extend(CheckOutcome("G-27", False, reason, item.hub_id) for item in items)
        return violations

    async def _alert_unmapped_transformer(self, bank_id: str) -> None:
        alerts = self.ports.alerts
        if alerts is None:
            return
        try:
            await alerts.raise_alert(
                XFMR_UNMAPPED_ALERT_RULE,
                "warning",
                "hubs without a service-transformer mapping: G-27 checks each as a group of one (09 S2.6)",
                bank_id,
                {"bank_id": bank_id, "default_kva_per_home": self.config.unmapped_xfmr_kva_per_home},
            )
        except Exception:
            logger.exception("failed to raise %s", XFMR_UNMAPPED_ALERT_RULE)

    async def _check_territory(self, proposal: ProposedBatch) -> list[CheckOutcome]:
        """G-34: every item's hub is on the proposal's bank (the guardian's own og.hub read, via the topology).
        G-33/K15: every non-idle item's hub territory against its obligation's market (headroom is FREE), via
        `market.check_territory` on the guardian's own contract and zone reads. A 0 kW item (the engine's
        own territory-block grant) serves no market and is not looked up."""
        violations: list[CheckOutcome] = []
        topology = self.ports.topology
        if topology is not None:
            seen: set[str] = set()
            for item in proposal.items:
                if item.hub_id in seen:
                    continue
                seen.add(item.hub_id)
                g34 = flow_checks.check_hub_in_bank(
                    item, proposal.bank_id, await topology.hub_bank(item.hub_id)
                )
                if not g34.ok:
                    violations.append(g34)
        port = self.ports.territory
        if port is None:
            return violations
        zone_territory = port.zone_territory()
        markets: dict[UUID, MarketRef | None] = {}
        availability = await port.bank_availability(proposal.bank_id)
        for item in proposal.items:
            if flow_checks.is_idle(item):
                continue
            # D-37 / K13: an obligation grandfathered on this (now unavailable, regulated) bank completes
            # untouched -- exempt from the availability veto and from K15 (the guardian's own read).
            if item.obligation_id is not None and await port.grandfathered(
                item.obligation_id, proposal.bank_id
            ):
                continue
            unavailable = flow_checks.check_g33_available(item, availability)
            if not unavailable.ok:
                violations.append(unavailable)
                continue
            if item.obligation_id is None:
                ref: MarketRef | None = flow_checks.HEADROOM_MARKET
            else:
                if item.obligation_id not in markets:
                    markets[item.obligation_id] = flow_checks.obligation_market_ref(
                        await port.obligation_market(item.obligation_id)
                    )
                ref = markets[item.obligation_id]
            zone = await port.hub_zone(item.hub_id)
            territory = territory_of_zone(zone, zone_territory)
            free_access = (
                await port.free_access(territory)
                if territory is not None and territory != ERCOT_COMPETITIVE
                else False
            )
            outcome = flow_checks.check_g33_territory(
                item, ref=ref, zone=zone, zone_territory=zone_territory, free_access=free_access
            )
            if not outcome.ok:
                violations.append(outcome)
        return violations

    def _check_bank_loading(
        self, bank_id: str, bank: BankSnapshot, additional_charge_kw: float, net_delta_kw: float
    ) -> list[CheckOutcome]:
        """G-03 (K4; the independent check on K9's single PI integrator, which it never regulates):
        a fresh SCADA reading is required; charging the batch adds is bounded by the allocator's own
        recharge-headroom formula; and the projected |bank load| is bounded in BOTH directions, so
        discharge into reverse flow (or cutting discharge on an importing bank) is checked too. A batch
        that does not raise the magnitude is relief and passes even on an overloaded bank (live
        2026-09-26: SCADA 3,000 kVA on 600 kVA banks)."""
        fresh = checks.check_g03_bank_load_fresh(
            bank_id, bank.bank_load_age_s, self.config.bank_load_max_age_s
        )
        if not fresh.ok:
            return [fresh]
        outcomes = [
            checks.check_g03_bank_kva_magnitude(
                bank_id,
                bank.bank_load_kva,
                net_delta_kw,
                bank.params,
                loading_pct=self.config.bank_loading_pct,
            )
        ]
        if additional_charge_kw > 0:
            outcomes.append(
                checks.check_g03_bank_kva(
                    bank_id,
                    additional_charge_kw,
                    bank.bank_load_kva,
                    bank.params,
                    loading_pct=self.config.bank_loading_pct,
                )
            )
        return [o for o in outcomes if not o.ok]

    async def _check_power_quality(self, proposal: ProposedBatch) -> list[CheckOutcome]:
        """K14 (G-21..G-24): only when an obligation behind this bank carries a non-default PQ envelope
        (`tightest_active_limits` is None otherwise -- grid-code-minimum dispatch is not gated, S4.c)."""
        pq = self.ports.pq
        if pq is None:
            return []
        limits = await pq.envelopes.tightest_active_limits(proposal.bank_id)
        if limits is None:
            return []
        measurement, is_stale = await pq.measurements.aggregate_measurement(proposal.bank_id)
        outcomes = [
            pq_checks.check_g21_phase_imbalance(proposal.bank_id, measurement, limits, is_stale=is_stale),
            pq_checks.check_g22_thd(proposal.bank_id, measurement, limits, is_stale=is_stale),
            pq_checks.check_g23_freq_voltage_deviation(
                proposal.bank_id, measurement, limits, is_stale=is_stale
            ),
        ]
        for item in proposal.items:
            asset = await pq.hub_assets.snapshot(item.hub_id)
            if asset is None:
                outcomes.append(CheckOutcome("G-24", False, "PQ_ASSET_STATE_UNKNOWN", item.hub_id))
                continue
            outcomes.append(
                pq_checks.check_g24_asset_conformance(
                    item.hub_id,
                    asset_state=asset.asset_state,
                    hub_ride_through_class=asset.ride_through_class,
                    envelope_ride_through_class=SENSITIVE_RIDE_THROUGH_CLASS,
                    pq_sensitive=True,
                )
            )
        return [o for o in outcomes if not o.ok]

    async def evaluate_and_sign_calibration(
        self, proposed: ProposedCalibrationCommand
    ) -> CalibrationCommand | None:
        """S6.7/K14: the sole signing path for a remote-recalibration command. Runs G-20 (clock quality)
        first, then G-25 (lease sanity; bounds within the firmware family's maximum and the correction
        within bounds; per-hub rate limit on the guardian's own signed record; no active PQ-sensitive
        grant on the hub; the fleet-wide budget, concurrency and systemic-drift caps, which also raise
        ALR-CALIBRATION-BUDGET). On PASS it atomically claims the attempt in the durable command ledger,
        which assigns the hub's next `(epoch, seq)` (strictly increasing per hub, persisted in
        `og.calibration_command`), signs the full wire envelope (`CalibrationCommand.signing_payload()`,
        the same fields the hub verifies; crypto.md S2.5) and returns it once the signed verdict and the
        ledger row are durable (K10). An attempt already claimed is never signed again. On any refusal it
        returns `None` (a hold: the ladder step is skipped, never forced through), claims the attempt as
        refused and traces the refusal best-effort."""
        pq = self.ports.pq
        ledger = pq.calibration_ledger if pq is not None else None
        if pq is None or ledger is None:
            await self._refuse_calibration(proposed, CheckOutcome("G-25", False, "PQ_CALIBRATION_NOT_WIRED"))
            return None
        if not await self._clock_ok():
            await self._refuse_calibration(proposed, CheckOutcome("G-20", False, "CLOCK_OFFSET_EXCEEDED"))
            return None

        now = self.now_fn()
        g25 = pq_checks.check_g25_calibration_lease(
            proposed,
            now=now,
            max_lease_s=self.config.calibration_max_lease_s,
            max_issue_skew_s=self.config.calibration_max_issue_skew_s,
        )
        if g25.ok:
            g25 = pq_checks.check_g25_calibration_safety(
                proposed,
                firmware_max_bounds=await pq.firmware_bounds.max_bounds_for_hub(proposed.hub_id),
                last_attempt_epoch_s=await pq.calibration_history.last_attempt_epoch_s(proposed.hub_id),
                now_epoch_s=now.timestamp(),
                min_interval_s=CALIBRATION_MIN_INTERVAL_S_DEFAULT,
                hub_has_active_sensitive_grant=await pq.sensitive_grants.has_active_non_default_envelope_grant(
                    proposed.hub_id
                ),
            )
        if g25.ok:
            usage = await ledger.fleet_usage(window_s=self.config.calibration_budget_window_s)
            g25 = pq_checks.check_g25_fleet_budget(
                fleet_hubs=usage.fleet_hubs,
                signed_in_window=usage.signed_in_window,
                in_flight=usage.in_flight,
                flagged_hubs=usage.flagged_hubs,
                budget_pct_per_window=self.config.calibration_budget_pct,
                max_concurrent=self.config.calibration_max_concurrent,
                systemic_drift_pct=self.config.calibration_systemic_drift_pct,
            )
            if not g25.ok:
                await self._alert_calibration_budget(ledger, g25, usage)
        if not g25.ok:
            await self._refuse_calibration(proposed, g25)
            return None

        sequence = await ledger.reserve(proposed.calibration_id, proposed.hub_id)
        if sequence is None:
            logger.info(
                "calibration attempt already claimed; not signing it again",
                extra={"calibration_id": str(proposed.calibration_id)},
            )
            return None
        epoch, seq = sequence
        command = self.sign_calibration_command(
            CalibrationCommand(
                calibration_id=proposed.calibration_id,
                hub_id=proposed.hub_id,
                epoch=epoch,
                seq=seq,
                issued_at=proposed.issued_at,
                expires_at=proposed.expires_at,
                reference=proposed.reference,
                correction=CalibrationCorrection(
                    freq_hz=proposed.correction.freq_hz,
                    voltage_pct=proposed.correction.voltage_pct,
                    phase_deg=proposed.correction.phase_deg,
                ),
                bounds=WireCalibrationBounds(
                    max_freq_hz=proposed.bounds.max_freq_hz,
                    max_voltage_pct=proposed.bounds.max_voltage_pct,
                    max_phase_deg=proposed.bounds.max_phase_deg,
                ),
                key_id=self.config.key_id,
                signature="",
            )
        )
        try:
            await self.ports.trace.append_calibration_verdict(
                proposed.calibration_id,
                {
                    "kind": "CALIBRATION",
                    "outcome": "SIGNED",
                    "calibration_id": str(proposed.calibration_id),
                    "hub_id": proposed.hub_id,
                    "epoch": epoch,
                    "seq": seq,
                    "signed_at": _iso_z(now),
                    "signature": command.signature,
                },
            )
            await ledger.mark_signed(proposed.calibration_id)
        except Exception:
            # K10: a signed command is released only once its verdict and its ledger row are durable.
            # Holding is safe: the reserved (epoch, seq) is simply never used, the attempt stays claimed,
            # and the drift escalates on its own timeline.
            logger.exception(
                "failed to record signed calibration command; withholding it",
                extra={"calibration_id": str(proposed.calibration_id), "hub_id": proposed.hub_id},
            )
            return None
        guardian_verdicts_total.labels(outcome="signed").inc()
        return command

    async def evaluate_and_sign_stop_release(self, request: ReleaseRequest) -> list[StopEvent] | None:
        """K8 / crypto.md S2.3: the only path by which a stop is released. Runs G-20, then every
        `stop_release.check_stop_release` precondition on the guardian's own reads (outstanding ENGAGEs
        from `og.stop_event`, active L2 instructions from its own MQTT subscription for every bank in the
        scope, og-api's trace row for the request). On PASS returns one guardian-signed RELEASE per
        outstanding ENGAGE, once the signed events are durably traced (K10) -- that trace row is what
        `main.py` hands to og-safestop to publish. On any refusal returns `None` (the stop stays engaged)
        and traces the refusal best-effort."""
        port = self.ports.stop_release
        if port is None:
            await self._trace_release_verdict(request, "REFUSED", reason="STOP_RELEASE_NOT_WIRED")
            return None
        if not await self._clock_ok():
            await self._trace_release_verdict(
                request, "REFUSED", reason="CLOCK_OFFSET_EXCEEDED", rule_id="G-20"
            )
            return None

        engaged = await port.outstanding_engages(request.scope_kind, request.scope_ref)
        instruction_kinds: list[str] = []
        for bank_id in await port.banks_in_scope(request.scope_kind, request.scope_ref):
            instruction = await self.ports.l2_instructions.active_instruction(bank_id)
            if instruction is not None:
                instruction_kinds.append(instruction.kind)
        traced = request.trace_id is not None and await self.ports.trace.exists_preimage(request.trace_id)
        now = self.now_fn()
        outcome = stop_release.check_stop_release(
            request,
            now=now,
            engaged=engaged,
            active_instruction_kinds=instruction_kinds,
            authorised_operators=self.config.stop_release_authorised_operators,
            approval_max_age_s=self.config.stop_release_max_age_s,
            max_clock_skew_s=self.config.stop_release_max_clock_skew_s,
            request_traced=traced,
        )
        if not outcome.ok:
            await self._trace_release_verdict(request, "REFUSED", reason=outcome.reason)
            return None

        events = stop_release.build_release_events(
            request, engaged, seed=self.signing_seed, key_id=self.config.key_id, issued_at=now
        )
        try:
            await self.ports.trace.append_stop_release_verdict(
                request.operator_action_id,
                {
                    "kind": "STOP_RELEASE",
                    "outcome": "SIGNED",
                    "operator_action_id": str(request.operator_action_id),
                    "scope_kind": request.scope_kind,
                    "scope_ref": request.scope_ref,
                    "events": [event.model_dump(mode="json") for event in events],
                },
            )
        except Exception:
            logger.exception(
                "failed to trace signed stop RELEASE; withholding it",
                extra={"operator_action_id": str(request.operator_action_id)},
            )
            return None
        logger.info(
            "stop RELEASE signed",
            extra={"scope": f"{request.scope_kind}:{request.scope_ref}", "stops": len(events)},
        )
        return events

    async def _trace_release_verdict(
        self,
        request: ReleaseRequest,
        outcome: str,
        *,
        reason: str | None,
        rule_id: str = stop_release.RULE_ID,
    ) -> None:
        """Best-effort trace of a refused release. The row also marks the request as decided, so it is not
        re-evaluated every cycle: a refused request must be re-requested and re-approved (fail closed)."""
        logger.warning(
            "stop RELEASE refused",
            extra={"scope": f"{request.scope_kind}:{request.scope_ref}", "reason": reason},
        )
        try:
            await self.ports.trace.append_stop_release_verdict(
                request.operator_action_id,
                {
                    "kind": "STOP_RELEASE",
                    "outcome": outcome,
                    "operator_action_id": str(request.operator_action_id),
                    "scope_kind": request.scope_kind,
                    "scope_ref": request.scope_ref,
                    "rule_id": rule_id,
                    "reason": reason,
                },
            )
        except Exception:
            logger.exception("failed to trace refused stop RELEASE")

    def sign_firmware_payload(self, payload: dict[str, Any]) -> str:
        """K3: Ed25519 over FirmwareCommand.signing_payload(), exactly what the hub verifies (R3.1). Called only
        by irmware.guardian_flow.process_pending_firmware, after G-36 has passed on the guardian's own reads."""
        return sign_payload(self.signing_seed, payload)

    def sign_calibration_command(self, command: CalibrationCommand) -> CalibrationCommand:
        """Sign a `CalibrationCommand` envelope over `signing_payload()` (every field but key_id/signature),
        exactly what the hub verifies -- mirrors `sign_command_batch` (interfaces/crypto.md S2.1)."""
        return command.model_copy(
            update={"signature": sign_payload(self.signing_seed, command.signing_payload())}
        )

    async def _alert_calibration_budget(
        self, ledger: CalibrationLedgerPort, outcome: CheckOutcome, usage: CalibrationFleetUsage
    ) -> None:
        """ALR-CALIBRATION-BUDGET (raised once while open): the fleet-wide calibration cap or the
        systemic-drift hold refused a command. Best-effort -- the refusal stands either way."""
        try:
            await ledger.raise_alert(
                "ALR-CALIBRATION-BUDGET",
                f"remote calibration held: {outcome.reason}",
                {
                    "reason": outcome.reason,
                    "fleet_hubs": usage.fleet_hubs,
                    "signed_in_window": usage.signed_in_window,
                    "in_flight": usage.in_flight,
                    "flagged_hubs": usage.flagged_hubs,
                },
            )
        except Exception:
            logger.exception("failed to raise ALR-CALIBRATION-BUDGET")

    async def _refuse_calibration(self, proposed: ProposedCalibrationCommand, outcome: CheckOutcome) -> None:
        """Claim the attempt as refused in the ledger (so it is never re-evaluated or signed later) and
        trace the refusal. Both best-effort: a refusal signs nothing either way (S6.7: the ladder step is
        skipped and the drift escalates on its own timeline)."""
        ledger = self.ports.pq.calibration_ledger if self.ports.pq is not None else None
        if ledger is not None:
            try:
                await ledger.refuse(
                    proposed.calibration_id, proposed.hub_id, outcome.reason or outcome.rule_id
                )
            except Exception:
                logger.exception("failed to record refused calibration attempt")
        await self._trace_calibration_refusal(proposed, outcome)

    async def _trace_calibration_refusal(
        self, proposed: ProposedCalibrationCommand, outcome: CheckOutcome
    ) -> None:
        """Best-effort trace of a refused calibration command."""
        guardian_verdicts_total.labels(outcome="vetoed").inc()
        logger.warning(
            "calibration command refused",
            extra={"hub_id": proposed.hub_id, "rule_id": outcome.rule_id, "reason": outcome.reason},
        )
        try:
            await self.ports.trace.append_calibration_verdict(
                proposed.calibration_id,
                {
                    "kind": "CALIBRATION",
                    "outcome": "REFUSED",
                    "calibration_id": str(proposed.calibration_id),
                    "hub_id": proposed.hub_id,
                    "rule_id": outcome.rule_id,
                    "reason": outcome.reason,
                },
            )
        except Exception:
            logger.exception(
                "failed to trace refused calibration verdict",
                extra={"calibration_id": str(proposed.calibration_id)},
            )

    async def _check_mobile_units(self, proposal: ProposedBatch) -> list[CheckOutcome]:
        """G-35 (D-31): no charging setpoint on a MOBILE_STORAGE unit away from its home station. Judged on
        each hub's net setpoint (the sum of its items, what the hub executes). A hub is mobile when it, or the
        single-hub bank it is on, is in the registry."""
        port = self.ports.mobile_units
        if port is None:
            return []
        bank_mobile = port.is_mobile(proposal.bank_id)
        violations: list[CheckOutcome] = []
        for item in checks.hub_setpoints(proposal.items):
            mobile = bank_mobile or port.is_mobile(item.hub_id)
            at_home = await port.at_home_station(item.hub_id) if mobile and item.p_kw_setpoint > 0 else None
            outcome = checks.check_g35_mobile_charge(
                item.hub_id, item.p_kw_setpoint, is_mobile=mobile, at_home_station=at_home
            )
            if not outcome.ok:
                violations.append(outcome)
        return violations

    async def _check_l2_boundary(self, proposal: ProposedBatch) -> list[CheckOutcome]:
        instruction = await self.ports.l2_instructions.active_instruction(proposal.bank_id)
        aggregate_abs_kw = sum(abs(item.p_kw_setpoint) for item in proposal.items)
        g15 = checks.check_g15_l2_boundary(instruction, proposal.bank_id, aggregate_abs_kw)
        return [] if g15.ok else [g15]

    async def _check_commitment_lock(self, proposal: ProposedBatch) -> list[CheckOutcome]:
        """GUARD-01/K13: enumerate ACTIVE obligations independently (never from the batch's own item
        list) so omitting an obligation, or relabelling it `obligation_id=None`, cannot evade G-19. A
        missing obligation contributes new_kw=0 to the check below, which vetoes it unless an allowed
        reason code was given for it -- and an allowed override reason is itself only a claim: a
        reduction below the lock is signed only when the guardian's own reads corroborate it
        (`checks.check_g19_override_evidence`)."""
        violations: list[CheckOutcome] = []
        totals = checks.obligation_totals(proposal.items)
        reason_by_obligation: dict[str, str | None] = {}
        for item in proposal.items:
            if item.obligation_id is not None:
                reason_by_obligation.setdefault(str(item.obligation_id), item.reason_code)

        active_obligations = await self.ports.commitments.active_obligations_for_bank(
            proposal.bank_id, proposal.cycle_id
        )
        committed_floor_kw = float(sum((o.frozen_kw for o in active_obligations), Decimal(0)))
        committed_by_obligation = {str(o.obligation_id): o.frozen_kw for o in active_obligations}
        evidence: _OverrideEvidence | None = None
        for obligation in active_obligations:
            obligation_key = str(obligation.obligation_id)
            new_kw = float(totals.get(obligation_key, Decimal(0)))  # omitted from the batch -> 0 kw
            frozen_kw = float(obligation.frozen_kw)
            prior = await self.ports.prior_grants.prior_granted_kw(obligation.obligation_id)
            prior_kw = float(prior) if prior is not None else frozen_kw
            reason_code = reason_by_obligation.get(obligation_key)
            if reason_code == reasons.R_GRANT_AS_HOLD and checks.g19_reduction_below_floor(
                new_kw, frozen_kw, prior_kw
            ):
                # AS capacity hold (migration 0020): held at 0 kW until ERCOT deploys it, on the guardian's
                # own reads of the award and its deployment, with the held reservation left unused.
                as_port = self.ports.as_awards
                held = checks.check_g19_as_hold(
                    obligation_key,
                    service_type=await as_port.service_type(obligation.obligation_id) if as_port else None,
                    deployment_active=await as_port.deployment_active(obligation.obligation_id)
                    if as_port
                    else None,
                    borrowed_by=checks.g19_obligations_over_commitment(
                        totals, committed_by_obligation, exclude=obligation_key
                    ),
                )
                if not held.ok:
                    violations.append(held)
                continue
            if reason_code == reasons.R_GRANT_CLOSED_LOOP and checks.g19_reduction_below_floor(
                new_kw, frozen_kw, prior_kw
            ):
                # Need basis (owner decision 2026-09-26): below the reserved maximum is signed only on the
                # guardian's own reads -- the profile is measured closed-loop, and nothing on this bank is
                # using the unused reservation.
                need = checks.check_g19_need_basis(
                    obligation_key,
                    setpoint_source=await self._setpoint_source(obligation.obligation_id),
                    borrowed_by=checks.g19_obligations_over_commitment(
                        totals, committed_by_obligation, exclude=obligation_key
                    ),
                )
                if not need.ok:
                    violations.append(need)
                continue
            if reason_code in checks.TERRITORY_BLOCK_REASONS and checks.g19_reduction_below_floor(
                new_kw, frozen_kw, prior_kw
            ):
                # K15: an obligation this bank may not serve (G-33 would veto serving it) -- signed only when
                # the guardian's own territory check agrees.
                territory = checks.check_g19_territory_block(
                    obligation_key,
                    guardian_block=await self._territory_block(obligation.obligation_id, proposal),
                )
                if not territory.ok:
                    violations.append(territory)
                continue
            if reason_code == reasons.R_OPERATOR_OVERRIDE and checks.g19_reduction_below_floor(
                new_kw, frozen_kw, prior_kw
            ):
                # An operator's live manual target took hubs this commitment was served from: signed only on
                # the guardian's own MANUAL_TARGET read and capability without those hubs.
                if evidence is None:
                    evidence = await self._override_evidence(proposal)
                overridden = checks.check_g19_operator_override(
                    obligation_key,
                    manual_target_hubs=evidence.manual_target_hubs,
                    capability_without_manual_upper_kw=evidence.capability_without_manual_upper_kw,
                    committed_floor_kw=committed_floor_kw,
                )
                if not overridden.ok:
                    violations.append(overridden)
                continue
            # A best-effort partial grant after a mid-window SHORTFALL carries the shortfall reason; it is
            # the override it maps to, and is corroborated below exactly like one.
            reason_code = checks.g19_lock_reason(reason_code)
            g19 = checks.check_g19_commitment_lock(
                obligation_key,
                new_kw,
                frozen_kw,
                prior_kw,
                reason_code,
                as_release_enabled=self.config.as_release_enabled,
            )
            if not g19.ok:
                violations.append(g19)
                continue
            if not checks.g19_reduction_below_floor(new_kw, frozen_kw, prior_kw):
                continue
            if evidence is None:
                evidence = await self._override_evidence(proposal)
            pq_floor_kw: float | None = None
            if evidence.pq_capability_upper_kw is not None and await self._is_pq_sensitive(
                obligation.obligation_id
            ):
                pq_floor_kw = await self._pq_floor_kw(active_obligations)
            corroborated = checks.check_g19_override_evidence(
                obligation_key,
                reason_code,
                l2_instruction_active=evidence.l2_instruction_active,
                bank_capability_kw=evidence.bank_capability_kw,
                bank_capability_upper_kw=evidence.bank_capability_upper_kw,
                committed_floor_kw=committed_floor_kw,
                pq_capability_upper_kw=evidence.pq_capability_upper_kw if pq_floor_kw is not None else None,
                pq_floor_kw=pq_floor_kw,
            )
            if not corroborated.ok and reason_code in checks.CAPABILITY_OVERRIDE_REASONS:
                # An INFEASIBLE claim made while an operator target owns some of the bank's hubs (the
                # allocator treats them as unavailable): corroborated on the capability without them.
                operator = checks.check_g19_operator_override(
                    obligation_key,
                    manual_target_hubs=evidence.manual_target_hubs,
                    capability_without_manual_upper_kw=evidence.capability_without_manual_upper_kw,
                    committed_floor_kw=committed_floor_kw,
                )
                if operator.ok:
                    continue
            if not corroborated.ok:
                violations.append(corroborated)
        return violations

    async def _is_pq_sensitive(self, obligation_id: UUID) -> bool:
        from opengrid.assets.repo import PQ_SENSITIVE_SERVICE_TYPES  # the one definition of the set

        as_port = self.ports.as_awards
        return as_port is not None and await as_port.service_type(obligation_id) in PQ_SENSITIVE_SERVICE_TYPES

    async def _territory_block(self, obligation_id: UUID, proposal: ProposedBatch) -> str | None:
        """The guardian's own K15 verdict on serving `obligation_id` from this batch's bank
        (`market.check_territory`, the G-33 predicate): the block reason, or None when it may be served.
        None too when there is nothing to read with (no territory port, no hub in the batch)."""
        port = self.ports.territory
        if port is None or not proposal.items:
            return None
        if await port.grandfathered(obligation_id, proposal.bank_id):
            return None  # D-37/K13: served here untouched, never a territory reduction
        availability = await port.bank_availability(proposal.bank_id)
        if availability is not None and not availability.available:
            return reasons.R_BANK_UNAVAILABLE
        ref = flow_checks.obligation_market_ref(await port.obligation_market(obligation_id))
        zone = await port.hub_zone(proposal.items[0].hub_id)
        territory = territory_of_zone(zone, port.zone_territory())
        free_access = (
            await port.free_access(territory)
            if territory is not None and territory != ERCOT_COMPETITIVE
            else False
        )
        return check_territory(ref, territory, free_access=free_access)

    async def _setpoint_source(self, obligation_id: UUID) -> str | None:
        port = self.ports.service_profiles
        return await port.setpoint_source(obligation_id) if port is not None else None

    async def _override_evidence(self, proposal: ProposedBatch) -> _OverrideEvidence:
        """The guardian's own reads a K13 override must agree with: an active L2 instruction on the bank,
        and the bank's deliverable discharge capability from its own telemetry of EVERY member hub
        capped by the bank rating (`core.physics.bank_capability`). Hubs that freshly report offline/fault
        contribute 0. A hub the guardian cannot currently see (stale) contributes 0 to the seen figure
        but its full rating to the upper bound, which is what corroboration uses: stale telemetry can
        never make a shortfall claim look true. Both are `None` -- never 0 -- with no read at all."""
        instruction = await self.ports.l2_instructions.active_instruction(proposal.bank_id)
        members_port = self.ports.bank_members
        bank = await self.ports.banks.snapshot(proposal.bank_id)
        members = await members_port.member_snapshots(proposal.bank_id) if members_port is not None else []
        updating = await self._firmware_updating_hubs(proposal.bank_id)
        if updating and members_port is not None:
            # A hub a firmware campaign has taken out of service cannot deliver (the engine excludes it, and a
            # commitment it served becomes R-COMMIT-LOCK-OVERRIDE-L0): unavailable here, seen or not.
            members = [
                snap
                for hub_id in await members_port.member_hub_ids(proposal.bank_id)
                if hub_id not in updating and (snap := await self.ports.hubs.snapshot(hub_id)) is not None
            ]
        if bank is None or not members:
            return _OverrideEvidence(
                instruction is not None, bank_capability_kw=None, bank_capability_upper_kw=None
            )
        lease_h = (
            max((proposal.expires_at - self.now_fn()).total_seconds(), self.config.cycle_interval_s) / 3600.0
        )
        policy = self._flow_policy()
        seen: list[float] = []
        unseen_upper: list[float] = []
        for hub in members:
            kw, is_seen = self._evidence_kw(hub, policy, lease_h)
            if kw is not None:
                (seen if is_seen else unseen_upper).append(kw)
        manual_hubs, without_manual_kw = await self._manual_target_evidence(
            proposal.bank_id, bank, policy, lease_h
        )
        return _OverrideEvidence(
            l2_instruction_active=instruction is not None,
            bank_capability_kw=bank_capability(seen, bank.params),
            bank_capability_upper_kw=bank_capability(seen + unseen_upper, bank.params),
            pq_capability_upper_kw=await self._pq_capability_upper_kw(
                proposal.bank_id, bank, policy, lease_h
            ),
            manual_target_hubs=manual_hubs,
            capability_without_manual_upper_kw=without_manual_kw,
        )

    async def _firmware_updating_hubs(self, bank_id: str) -> frozenset[str]:
        """Hubs on `bank_id` a firmware job has in flight (the guardian's own `og.firmware_job` read). A failed or
        missing read is empty: the hubs then count as available, which can only make an override claim look
        LESS true (never corroborates one)."""
        port = self.ports.firmware_updating
        if port is None:
            return frozenset()
        try:
            return frozenset(await port.updating_hub_ids(bank_id))
        except Exception:
            logger.exception(
                "firmware-updating read failed: counted as available", extra={"bank_id": bank_id}
            )
            return frozenset()

    async def _manual_target_evidence(
        self, bank_id: str, bank: BankSnapshot, policy: flow_checks.FlowPolicy, lease_h: float
    ) -> tuple[frozenset[str], float | None]:
        """Live operator targets on this bank (the guardian's own read of MANUAL_TARGET trace events) and the
        bank's upper-bound capability WITHOUT those operator-owned hubs -- what the allocator could still
        serve a commitment from. `(empty, None)`: no targets, or no read (a failed read is never evidence)."""
        port, members_port = self.ports.manual_targets, self.ports.bank_members
        if port is None or members_port is None:
            return frozenset(), None
        hub_ids = await members_port.member_hub_ids(bank_id)
        try:
            manual = frozenset(await port.manual_target_hubs(hub_ids))
        except Exception:
            logger.exception(
                "manual-target read failed: no operator-override evidence", extra={"bank_id": bank_id}
            )
            return frozenset(), None
        if not manual:
            return frozenset(), None
        remaining: list[float] = []
        for hub_id in hub_ids:
            if hub_id in manual:
                continue
            hub = await self.ports.hubs.snapshot(hub_id)
            if hub is None:
                continue
            kw, _seen = self._evidence_kw(hub, policy, lease_h)
            if kw is not None:
                remaining.append(kw)
        return manual, bank_capability(remaining, bank.params)

    def _evidence_kw(
        self, hub: HubSnapshot, policy: flow_checks.FlowPolicy, lease_h: float
    ) -> tuple[float | None, bool]:
        """One member hub's contribution to G-19's capability evidence: `(kW, seen live)`. Online: the
        allocator's own bound (F1 derating by SoC and temperature, the BMS limit, the unit rating --
        `derated_bounds`, any unknown temperature at the configured factor), energy-limited over the lease,
        so a derating shortfall is corroborated. Stale: its full rating (upper bound only). Offline or fault:
        nothing."""
        if hub.health == "online":
            derated_kw = flow_checks.derated_bounds(hub, policy, static_temp_unknown=True).discharge_kw
            return hub_sustainable_discharge_kw(
                hub.soc_kwh, hub.params.r_kwh, derated_kw, lease_h, hub.params.eta_d
            ), True
        if hub.health == "stale":
            return max(hub.params.p_kw, 0.0), False
        return None, False

    async def _pq_capability_upper_kw(
        self, bank_id: str, bank: BankSnapshot, policy: flow_checks.FlowPolicy, lease_h: float
    ) -> float | None:
        """K14: the upper-bound capability of only the member hubs that pass the guardian's own G-24 asset
        conformance for a PQ-sensitive obligation. None -- no PQ evidence -- when the bank carries no active
        non-default envelope, or its membership cannot be read."""
        pq, members_port = self.ports.pq, self.ports.bank_members
        if pq is None or members_port is None or await pq.envelopes.tightest_active_limits(bank_id) is None:
            return None
        hub_ids = await members_port.member_hub_ids(bank_id)
        if not hub_ids:
            return None
        eligible: list[float] = []
        for hub_id in hub_ids:
            hub = await self.ports.hubs.snapshot(hub_id)
            if hub is None or not await self._pq_eligible(hub_id):
                continue
            kw, _seen = self._evidence_kw(hub, policy, lease_h)
            if kw is not None:
                eligible.append(kw)
        return bank_capability(eligible, bank.params)

    async def _pq_floor_kw(self, active_obligations: list[ActiveObligation]) -> float | None:
        """The bank's committed kW on PQ-sensitive service types, on the guardian's own obligation reads.
        None when any obligation's service type cannot be read (no PQ evidence then)."""
        from opengrid.assets.repo import PQ_SENSITIVE_SERVICE_TYPES  # the one definition of the set

        as_port = self.ports.as_awards
        if as_port is None:
            return None
        floor = 0.0
        for obligation in active_obligations:
            service_type = await as_port.service_type(obligation.obligation_id)
            if service_type is None:
                return None
            if service_type in PQ_SENSITIVE_SERVICE_TYPES:
                floor += float(obligation.frozen_kw)
        return floor

    async def _pq_eligible(self, hub_id: str) -> bool:
        """G-24 for a PQ-sensitive obligation on the guardian's own asset read (unknown asset: ineligible)."""
        pq = self.ports.pq
        if pq is None:
            return False
        asset = await pq.hub_assets.snapshot(hub_id)
        if asset is None:
            return False
        return pq_checks.check_g24_asset_conformance(
            hub_id,
            asset_state=asset.asset_state,
            hub_ride_through_class=asset.ride_through_class,
            envelope_ride_through_class=SENSITIVE_RIDE_THROUGH_CLASS,
            pq_sensitive=True,
        ).ok

    @staticmethod
    def _accumulate(store: OrderedDict[Any, float], key: Any, delta: float) -> float:
        """The cycle's cumulative value for `key` including this batch's `delta`. Inside `evaluate_and_sign`
        the delta is only staged (committed on PASS, see there); outside it, it is committed at once."""
        staged = _STAGED.get()
        if staged is None:
            return GuardianService._commit(store, key, delta)
        pending = sum(d for s, k, d in staged if s is store and k == key)
        staged.append((store, key, delta))
        return store.get(key, 0.0) + pending + delta

    @staticmethod
    def _commit(store: OrderedDict[Any, float], key: Any, delta: float) -> float:
        total = store.get(key, 0.0) + delta
        store[key] = total
        store.move_to_end(key)
        while len(store) > _MAX_CYCLE_HISTORY:
            store.popitem(last=False)
        return total

    @staticmethod
    def _commit_accumulators(staged: _Staged) -> None:
        """A signed batch's staged deltas become part of its cycle's accumulators."""
        for store, key, delta in staged:
            GuardianService._commit(store, key, delta)
        staged.clear()

    @staticmethod
    def _classify(violations: list[CheckOutcome]) -> VerdictOutcome:
        """VETOED if a batch-wide rule failed or every item-level violation shares no untouched
        sibling; PARTLY_VETOED when only a strict subset of hubs is affected by item-level rules."""
        if any(v.rule_id not in _ITEM_LEVEL_RULES for v in violations):
            return "VETOED"
        return "PARTLY_VETOED"

    async def _finalize(
        self,
        batch: CommandBatchRow,
        verdict_id: UUID,
        started: float,
        *,
        outcome: VerdictOutcome,
        vetoed_rule_ids: list[str],
        violations: list[CheckOutcome] | None = None,
    ) -> Verdict:
        self._violations[batch.command_batch_id] = list(violations or [])
        self._violations.move_to_end(batch.command_batch_id)
        while len(self._violations) > _MAX_EVALUATED_PROPOSALS:
            self._violations.popitem(last=False)
        latency_ms = max(int((self.monotonic_fn() - started) * 1000), 0)
        inputs_hash = self._inputs_hash(batch)
        signature: str | None = None
        signed_at: datetime | None = None

        if outcome == "PASS":
            signed_at = self.now_fn()
            payload = {
                "command_batch_id": str(batch.command_batch_id),
                "outcome": outcome,
                "vetoed_rule_ids": vetoed_rule_ids,
                "inputs_hash": inputs_hash,
                "signed_at": _iso_z(signed_at),
            }
            signature = sign_payload(self.signing_seed, payload)
            self._record_signed_setpoints(batch.command_batch_id, signed_at)
        else:
            # contract with DISPATCH: a hub a veto names re-anchors on telemetry on both sides
            self._drop_signed_anchors(vetoed_hub_ids(violations or []))

        verdict = Verdict(
            verdict_id=verdict_id,
            command_batch_id=batch.command_batch_id,
            outcome=outcome,
            vetoed_rule_ids=vetoed_rule_ids,
            latency_ms=latency_ms,
            inputs_hash=inputs_hash,
            signature=signature,
            signed_at=signed_at,
        )
        guardian_verdicts_total.labels(
            outcome={"PASS": "signed", "TIMEOUT": "timeout"}.get(outcome, "vetoed")
        ).inc()
        # Deliberately NOT withheld on a failed verdict trace (unlike the calibration and stop-release
        # paths, where the trace row IS the durable record): here K10's pre-image is already proven by
        # G-14 before signing, and `main.py` durably inserts this verdict (with its signature) into
        # og.verdict before anything is published -- a failed insert publishes nothing. The GUARDIAN_VERDICT
        # trace is K11 audit on top; losing it must not change the decision (K7, TS property K07).
        if not await self._trace_verdict(verdict, violations or []):
            await self._alert_trace_verdict_failure(verdict)
        return verdict

    async def _alert_trace_verdict_failure(self, verdict: Verdict) -> None:
        """A GUARDIAN_VERDICT trace row could not be written: counted and raised as a warning (once while
        open, `PgAlertPort`), never a change to the verdict itself (K7 -- see `_finalize`). Best-effort."""
        guardian_trace_verdict_failures_total.inc()
        alerts = self.ports.alerts
        if alerts is None:
            return
        try:
            await alerts.raise_alert(
                TRACE_VERDICT_ALERT_RULE,
                "warning",
                "guardian verdict trace write failed: the K11 audit row for a verdict is missing",
                TRACE_VERDICT_ALERT_RULE,
                {"command_batch_id": str(verdict.command_batch_id), "outcome": verdict.outcome},
            )
        except Exception:
            logger.exception("failed to raise %s", TRACE_VERDICT_ALERT_RULE)

    def sign_command_batch(self, batch: CommandBatch) -> CommandBatch:
        """Sign the command-batch envelope a hub will verify (interfaces/crypto.md S2.1). This is a
        separate signature from the verdict's (S2.2): the verdict signature stays in `og.verdict` for
        the audit trail, and is never what goes on the wire."""
        return batch.model_copy(
            update={"signature": sign_payload(self.signing_seed, batch.signing_payload())}
        )

    def _inputs_hash(self, batch: CommandBatchRow) -> str:
        """sha256, hex -- domain-separated over (version_vector, ledger_version, batch_hash), per
        interfaces/crypto.md S2.2."""
        return sha256_hex_of_json(
            {
                "version_vector": {"ledger_version": batch.ledger_version},
                "ledger_version": batch.ledger_version,
                "batch_hash": batch.merkle_root,
            }
        )

    async def _trace_verdict(self, verdict: Verdict, violations: list[CheckOutcome]) -> bool:
        """Audit trace of the verdict itself (GUARDIAN_VERDICT, 02a S8.1), including each distinct
        violation (rule, reason, hub/obligation) so a veto is explainable from the trace alone. Never
        raises; returns False on failure (see `_finalize` for why a PASS still stands)."""
        payload = verdict.model_dump(mode="json")
        payload["violations"] = _violation_summary(violations)
        payload["vetoed_hub_ids"] = vetoed_hub_ids(violations)
        try:
            await self.ports.trace.append_verdict(verdict.command_batch_id, payload)
        except Exception:
            logger.exception(
                "failed to trace guardian verdict", extra={"command_batch_id": str(verdict.command_batch_id)}
            )
            return False
        return True
