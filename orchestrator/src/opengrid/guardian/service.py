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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from opengrid.core.crypto import sha256_hex_of_json, sign_payload
from opengrid.core.models.engine import CommandBatchRow, Verdict, VerdictOutcome
from opengrid.core.models.mqtt import CommandBatch, StopEvent
from opengrid.core.models.pq import CalibrationBounds as WireCalibrationBounds
from opengrid.core.models.pq import CalibrationCommand, CalibrationCorrection
from opengrid.core.physics import bank_capability, hub_ramp_kw_per_s, hub_sustainable_discharge_kw
from opengrid.core.pq.constants import CALIBRATION_MIN_INTERVAL_S_DEFAULT
from opengrid.guardian import checks, pq_checks, stop_release
from opengrid.guardian.checks import CheckOutcome
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import BankSnapshot, GuardianPorts, ProposedBatch, ReleaseRequest
from opengrid.guardian.pq_ports import ProposedCalibrationCommand
from opengrid.platform.metrics import guardian_clock_offset_ms, guardian_verdicts_total

logger = logging.getLogger(__name__)

_MAX_CYCLE_HISTORY = 64  # bound the in-memory ramp accumulators; MVP-S runs a 2s cycle, never GC-free
_MAX_EVALUATED_PROPOSALS = 256  # > one tick's pending batches (main.py fetches at most 50 per tick)
_ITEM_LEVEL_RULES = frozenset({"G-01", "G-01-ENERGY", "G-02", "G-04", "G-24"})


@dataclass(frozen=True, slots=True)
class _OverrideEvidence:
    """What the guardian itself reads when a batch claims a K13 override (see `_override_evidence`)."""

    l2_instruction_active: bool
    bank_capability_kw: float | None  # from member hubs the guardian sees live; stale ones count 0
    bank_capability_upper_kw: float | None  # the same with every stale hub at its full rating


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

    _evaluated: OrderedDict[UUID, ProposedBatch] = field(default_factory=OrderedDict, init=False)
    _calibration_epoch: int | None = field(default=None, init=False)
    _calibration_seq_by_hub: dict[str, int] = field(default_factory=dict, init=False)

    async def evaluate_and_sign(self, batch: CommandBatchRow) -> Verdict:
        """Run every applicable G-check against independently-read state; PASS signs, any veto returns
        VETOED/PARTLY_VETOED, and a processing timeout (or a G-20 clock-quality failure) returns
        TIMEOUT -- all three are holds, never a stop (K7)."""
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
        return checks.check_g20_clock_quality(offset_ms, self.config.clock_offset_max_ms).ok

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
        fleet_delta_kw = 0.0
        additional_charge_kw = 0.0

        lease_ttl_h = max((proposal.expires_at - self.now_fn()).total_seconds(), 0.0) / 3600.0

        for item in proposal.items:
            hub = await self.ports.hubs.snapshot(item.hub_id)
            if hub is None:
                violations.append(CheckOutcome("G-01", False, "HUB_UNKNOWN", item.hub_id))
                continue
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
            g02 = checks.check_g02_hub_power(item, hub.params, inverter_cap_kw=self.config.inverter_cap_kw)
            if not g02.ok:
                violations.append(g02)

            g04 = checks.check_g04_hub_ramp(
                item, hub.prev_p_kw, self.config.cycle_interval_s, hub_ramp_kw_per_s(hub.params)
            )
            if not g04.ok:
                violations.append(g04)

            fleet_delta_kw += item.p_kw_setpoint - hub.prev_p_kw
            additional_charge_kw += max(item.p_kw_setpoint, 0.0) - max(hub.prev_p_kw, 0.0)

        bank = await self.ports.banks.snapshot(proposal.bank_id)
        if bank is None:
            violations.append(CheckOutcome("G-03", False, "BANK_UNKNOWN", proposal.bank_id))
        else:
            violations.extend(
                self._check_bank_loading(proposal.bank_id, bank, additional_charge_kw, fleet_delta_kw)
            )

        cumulative_fleet = self._accumulate(self._fleet_delta_by_cycle, proposal.cycle_id, fleet_delta_kw)
        g05 = checks.check_g05_fleet_ramp(
            cumulative_fleet,
            self.config.cycle_interval_s,
            is_firm_event=proposal.is_firm_event,
            discretionary_cap_kw_per_min=self.config.discretionary_ramp_cap_kw_per_min,
            non_firm_cap_kw_per_min=self.config.non_firm_ramp_cap_kw_per_min,
        )
        if not g05.ok:
            violations.append(g05)

        if bank is not None and bank.feeder_id is not None:
            feeder_key = (proposal.cycle_id, bank.feeder_id)
            cumulative_feeder = self._accumulate(self._feeder_delta_by_cycle, feeder_key, fleet_delta_kw)
            ceiling = bank.feeder_ceiling_kw_per_min or self.config.feeder_ramp_ceiling_kw_per_min.get(
                bank.feeder_id, self.config.default_feeder_ramp_ceiling_kw_per_min
            )
            g06 = checks.check_g06_feeder_ramp(
                cumulative_feeder, self.config.cycle_interval_s, ceiling, is_firm_event=proposal.is_firm_event
            )
            if not g06.ok:
                violations.append(g06)

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
        grant on the hub). On PASS it assigns the command's `(epoch, seq)` itself -- strictly increasing
        per hub across restarts (`_next_calibration_sequence`) -- signs the full wire envelope
        (`CalibrationCommand.signing_payload()`, the same fields the hub verifies) and returns it once the
        signed verdict is durably traced (K10). On any refusal it returns `None` (a hold: the ladder
        step is skipped, never forced through) and traces the refusal best-effort."""
        pq = self.ports.pq
        if pq is None:
            await self._trace_calibration_refusal(proposed, CheckOutcome("G-25", False, "PQ_PORTS_NOT_WIRED"))
            return None
        if not await self._clock_ok():
            await self._trace_calibration_refusal(
                proposed, CheckOutcome("G-20", False, "CLOCK_OFFSET_EXCEEDED")
            )
            return None

        now = self.now_fn()
        lease = pq_checks.check_g25_calibration_lease(
            proposed,
            now=now,
            max_lease_s=self.config.calibration_max_lease_s,
            max_issue_skew_s=self.config.calibration_max_issue_skew_s,
        )
        g25 = lease
        if lease.ok:
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
        if not g25.ok:
            await self._trace_calibration_refusal(proposed, g25)
            return None

        epoch, seq = self._next_calibration_sequence(proposed.hub_id)
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
        except Exception:
            # K10: a signed command is released only once its verdict is durable. Holding here is safe:
            # the ladder's attempt stays PENDING and ages out, and the drift escalates on its own.
            logger.exception(
                "failed to trace signed calibration verdict; withholding the command",
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

    def sign_calibration_command(self, command: CalibrationCommand) -> CalibrationCommand:
        """Sign a `CalibrationCommand` envelope over `signing_payload()` (every field but key_id/signature),
        exactly what the hub verifies -- mirrors `sign_command_batch` (interfaces/crypto.md S2.1)."""
        return command.model_copy(
            update={"signature": sign_payload(self.signing_seed, command.signing_payload())}
        )

    def _next_calibration_sequence(self, hub_id: str) -> tuple[int, int]:
        """Per-hub strictly increasing `(epoch, seq)` for calibration commands (K6 pattern). The epoch is
        this process's first-signing wall-clock second (G-20 has just vouched for that clock), so a
        restarted guardian always starts above every pair the previous process issued; `seq` counts up
        per hub within the epoch. No durable table is needed and nothing is taken from the proposer."""
        if self._calibration_epoch is None:
            self._calibration_epoch = int(self.now_fn().timestamp())
        seq = self._calibration_seq_by_hub.get(hub_id, 0) + 1
        self._calibration_seq_by_hub[hub_id] = seq
        return self._calibration_epoch, seq

    async def _trace_calibration_refusal(
        self, proposed: ProposedCalibrationCommand, outcome: CheckOutcome
    ) -> None:
        """Best-effort trace of a refused calibration command. The trace row is also what marks the
        ladder's PENDING attempt as evaluated, so it is not re-evaluated every cycle (S6.7: a refusal
        skips the ladder step; the drift escalates on its own timeline)."""
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
        evidence: _OverrideEvidence | None = None
        for obligation in active_obligations:
            obligation_key = str(obligation.obligation_id)
            new_kw = float(totals.get(obligation_key, Decimal(0)))  # omitted from the batch -> 0 kw
            frozen_kw = float(obligation.frozen_kw)
            prior = await self.ports.prior_grants.prior_granted_kw(obligation.obligation_id)
            prior_kw = float(prior) if prior is not None else frozen_kw
            reason_code = reason_by_obligation.get(obligation_key)
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
            corroborated = checks.check_g19_override_evidence(
                obligation_key,
                reason_code,
                l2_instruction_active=evidence.l2_instruction_active,
                bank_capability_kw=evidence.bank_capability_kw,
                bank_capability_upper_kw=evidence.bank_capability_upper_kw,
                committed_floor_kw=committed_floor_kw,
            )
            if not corroborated.ok:
                violations.append(corroborated)
        return violations

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
        if bank is None or not members:
            return _OverrideEvidence(
                instruction is not None, bank_capability_kw=None, bank_capability_upper_kw=None
            )
        lease_h = (
            max((proposal.expires_at - self.now_fn()).total_seconds(), self.config.cycle_interval_s) / 3600.0
        )
        seen: list[float] = []
        unseen_upper: list[float] = []
        for hub in members:
            if hub.health == "online":
                seen.append(
                    hub_sustainable_discharge_kw(
                        hub.soc_kwh, hub.params.r_kwh, hub.params.p_kw, lease_h, hub.params.eta_d
                    )
                )
            elif hub.health == "stale":
                unseen_upper.append(max(hub.params.p_kw, 0.0))
        return _OverrideEvidence(
            l2_instruction_active=instruction is not None,
            bank_capability_kw=bank_capability(seen, bank.params),
            bank_capability_upper_kw=bank_capability(seen + unseen_upper, bank.params),
        )

    @staticmethod
    def _accumulate(store: OrderedDict[Any, float], key: Any, delta: float) -> float:
        total = store.get(key, 0.0) + delta
        store[key] = total
        store.move_to_end(key)
        while len(store) > _MAX_CYCLE_HISTORY:
            store.popitem(last=False)
        return total

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
        await self._trace_verdict(verdict, violations or [])
        return verdict

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

    async def _trace_verdict(self, verdict: Verdict, violations: list[CheckOutcome]) -> None:
        """Best-effort audit trace of the verdict itself (GUARDIAN_VERDICT, 02a S8.1), including each
        distinct violation (rule, reason, hub/obligation) so a veto is explainable from the trace alone.
        Never raises -- the signing decision is already final; a tracing hiccup must not undo it (K7)."""
        payload = verdict.model_dump(mode="json")
        payload["violations"] = _violation_summary(violations)
        try:
            await self.ports.trace.append_verdict(verdict.command_batch_id, payload)
        except Exception:
            logger.exception(
                "failed to trace guardian verdict", extra={"command_batch_id": str(verdict.command_batch_id)}
            )
