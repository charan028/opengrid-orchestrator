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
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from opengrid.core.crypto import sha256_hex_of_json, sign_payload
from opengrid.core.models.engine import CommandBatchRow, Verdict, VerdictOutcome
from opengrid.core.models.mqtt import CommandBatch
from opengrid.core.physics import hub_ramp_kw_per_s
from opengrid.guardian import checks
from opengrid.guardian.checks import CheckOutcome
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import GuardianPorts, ProposedBatch
from opengrid.platform.metrics import guardian_clock_offset_ms, guardian_verdicts_total

logger = logging.getLogger(__name__)

_MAX_CYCLE_HISTORY = 64  # bound the in-memory ramp accumulators; MVP-S runs a 2s cycle, never GC-free
_ITEM_LEVEL_RULES = frozenset({"G-01", "G-01-ENERGY", "G-02", "G-04"})


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

    async def evaluate_and_sign(self, batch: CommandBatchRow) -> Verdict:
        """Run every applicable G-check against independently-read state; PASS signs, any veto returns
        VETOED/PARTLY_VETOED, and a processing timeout (or a G-20 clock-quality failure) returns
        TIMEOUT -- all three are holds, never a stop (K7)."""
        started = self.monotonic_fn()
        verdict_id = uuid4()

        offset_ms = await self.ports.clock.offset_from_ntp_ms()
        guardian_clock_offset_ms.set(offset_ms)
        if not checks.check_g20_clock_quality(offset_ms, self.config.clock_offset_max_ms).ok:
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

    async def _run_checks(self, batch: CommandBatchRow) -> list[CheckOutcome]:
        violations: list[CheckOutcome] = []

        proposal = await self.ports.proposals.fetch(batch.command_batch_id)
        if proposal is None:
            return [CheckOutcome("G-14", False, "PROPOSAL_NOT_FOUND")]

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
        elif additional_charge_kw > 0:
            # G-03 bounds the load a batch ADDS to the bank. A batch that adds none (discharge, or less
            # charging) cannot worsen loading; vetoing it on an already-overloaded bank (live
            # 2026-09-26: SCADA 3,000 kVA on 600 kVA banks) would block the very relief it provides.
            g03 = checks.check_g03_bank_kva(
                proposal.bank_id,
                additional_charge_kw,
                bank.bank_load_kva,
                bank.params,
                loading_pct=self.config.bank_loading_pct,
            )
            if not g03.ok:
                violations.append(g03)

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

    async def _check_l2_boundary(self, proposal: ProposedBatch) -> list[CheckOutcome]:
        instruction = await self.ports.l2_instructions.active_instruction(proposal.bank_id)
        aggregate_abs_kw = sum(abs(item.p_kw_setpoint) for item in proposal.items)
        g15 = checks.check_g15_l2_boundary(instruction, proposal.bank_id, aggregate_abs_kw)
        return [] if g15.ok else [g15]

    async def _check_commitment_lock(self, proposal: ProposedBatch) -> list[CheckOutcome]:
        """GUARD-01/K13: enumerate ACTIVE obligations independently (never from the batch's own item
        list) so omitting an obligation, or relabelling it `obligation_id=None`, cannot evade G-19. A
        missing obligation contributes new_kw=0 to the check below, which vetoes it unless an allowed
        reason code was given for it."""
        violations: list[CheckOutcome] = []
        totals = checks.obligation_totals(proposal.items)
        reason_by_obligation: dict[str, str | None] = {}
        for item in proposal.items:
            if item.obligation_id is not None:
                reason_by_obligation.setdefault(str(item.obligation_id), item.reason_code)

        active_obligations = await self.ports.commitments.active_obligations_for_bank(
            proposal.bank_id, proposal.cycle_id
        )
        for obligation in active_obligations:
            obligation_key = str(obligation.obligation_id)
            new_kw = totals.get(obligation_key, Decimal(0))  # omitted from the batch -> counts as 0 kw
            frozen_kw = obligation.frozen_kw
            prior = await self.ports.prior_grants.prior_granted_kw(obligation.obligation_id)
            prior_kw = prior if prior is not None else frozen_kw
            reason_code = reason_by_obligation.get(obligation_key)
            g19 = checks.check_g19_commitment_lock(
                obligation_key,
                float(new_kw),
                float(frozen_kw),
                float(prior_kw),
                reason_code,
                as_release_enabled=self.config.as_release_enabled,
            )
            if not g19.ok:
                violations.append(g19)
        return violations

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
