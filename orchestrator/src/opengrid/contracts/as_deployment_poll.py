"""ERCOT AS deployment instruction poller, hosted by og-feeds (D-35; protocol-adapters.md S6.7).

Every `interval_s` the poller asks a `DispatchInstructionSource` (the `ercot_mms` adapter: ERCOT EWS, or the
ogsim MMS simulator on the same wire) for the dispatch instructions not yet acknowledged, and turns each
one into exactly what the operator route `POST /og/api/dispatch/as-deployments` would do -- through the
SAME core function (`AsCallGateway` -> `opengrid.calls`), never a copy of its checks:

- AS_DEPLOYMENT: the instruction names a resource and an AS type; `[feeds.ercot_as_poll.awards]` maps
  `"<resource>:<SERVICE>"` to the contract that carries those awards, and the ERCOT_AS obligation of that
  contract whose window covers the instruction's start is the award deployed. The core decides (404 no
  such award, 409 not deployable / not ERCOT_AS / overlap / over the product duration / over the awarded
  kW, 422 malformed) and writes the deployment; the poller only acknowledges and alerts.
- AS_RECALL: ends the deployment it names (`recalls`) through the core's cancel.
- Anything else (a plain VDI) is acknowledged and traced for operator attention; it never dispatches.

Guarantees:

- **Idempotent by instruction id**: the id is the core's idempotency key (principal `ercot:<backend>`), so a
  duplicate delivery (at-least-once transport, or a restart before the acknowledgement) never deploys twice;
  it is acknowledged again with the first answer.
- **Out-of-order safe**: a batch is processed in issue order, and a recall that arrives before its
  deployment is held (left unacknowledged, so the source keeps re-sending it) for `recall_hold_s`; the
  deployment, when it arrives, is superseded -- never started. A late instruction (older than
  `max_instruction_age_s` on first sight) is refused, never acted on.
- **Never the TOLLING contract**: origin ERCOT_POLL is limited to ERCOT_AS obligations by the core, and the
  award map only ever names ERCOT_AS contracts.
- **Traced** (`origin = ERCOT_POLL`) on stream `ercot_as_poll` before each acknowledgement (K10).
- **Alerts**: `ALR-ERCOT-AS-REFUSED` per refused instruction; `ALR-ERCOT-AS-POLL-FAILED` after
  `failure_alert_after` consecutive failed polls and `ALR-ERCOT-AS-POLL-STALE` once no poll has succeeded
  for `stale_after_s` (both cleared by the next good poll). A failed poll is retried with the feeds'
  capped doubling backoff, scaled to this cadence: `interval_s`, 2x, 4x ... up to `retry_cap_s`.

Off by default (`[feeds.ercot_as_poll].enabled = false`): `build_as_deployment_poller` then returns None and
nothing is polled, traced or written. With it on and no instruction received, nothing is written either:
committed ERCOT_AS obligations keep their 0 kW capacity hold until ERCOT actually deploys them.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opengrid.integrations.ercot_mms.client import ErcotMmsSettings
from opengrid.integrations.ercot_mms.intake import AwardContractMap
from opengrid.integrations.interfaces import DispatchInstruction, DispatchInstructionSource, InstructionBatch

logger = logging.getLogger(__name__)

ORIGIN = "ERCOT_POLL"
TRACE_STREAM = "ercot_as_poll"
TRACE_DECISION_TYPE = "FEED_CHANGE"
ALR_REFUSED = "ALR-ERCOT-AS-REFUSED"
ALR_POLL_FAILED = "ALR-ERCOT-AS-POLL-FAILED"
ALR_POLL_STALE = "ALR-ERCOT-AS-POLL-STALE"
#: Local refusal codes (before the core is reached). Everything else is the core's own reason code.
R_MALFORMED = "R-ERCOT-AS-MALFORMED"
R_NO_AWARD = "R-ERCOT-AS-NO-AWARD"
R_AMBIGUOUS = "R-ERCOT-AS-AMBIGUOUS-AWARD"
R_LATE = "R-ERCOT-AS-LATE"
R_NOTHING_TO_RECALL = "R-ERCOT-AS-NOTHING-TO-RECALL"


class ErcotAsPollSettings(BaseModel):
    """`[feeds.ercot_as_poll]`; `mms` is `[feeds.ercot_as_poll.mms]` (an `ErcotMmsSettings`: the sim URL
    in `endpoint`, `signing = "none"` against the sim), `awards` is `[feeds.ercot_as_poll.awards]`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    interval_s: float = Field(default=5.0, gt=0, le=60)
    retry_cap_s: float = Field(default=60.0, gt=0)
    failure_alert_after: int = Field(default=3, ge=1)
    stale_after_s: float = Field(default=60.0, gt=0)
    lookback_s: float = Field(default=900.0, gt=0)
    max_instruction_age_s: float = Field(default=300.0, gt=0)
    recall_hold_s: float = Field(default=300.0, gt=0)
    mms: ErcotMmsSettings | None = None
    awards: dict[str, UUID] = {}

    @model_validator(mode="after")
    def _validate(self) -> ErcotAsPollSettings:
        if self.enabled and self.mms is None:
            raise ValueError("[feeds.ercot_as_poll].enabled needs [feeds.ercot_as_poll.mms]")
        if self.stale_after_s <= self.interval_s:
            raise ValueError("[feeds.ercot_as_poll].stale_after_s must exceed interval_s")
        return self

    @property
    def award_map(self) -> AwardContractMap:
        return AwardContractMap(ancillary=dict(self.awards))


def settings_from(cfg: object) -> ErcotAsPollSettings:
    """`[feeds.ercot_as_poll]` from a `Config` (duck-typed `.get(dotted, default)`)."""
    raw = cfg.get("feeds.ercot_as_poll", {}) if hasattr(cfg, "get") else {}
    return ErcotAsPollSettings.model_validate(dict(raw or {}))


def retry_delay_s(settings: ErcotAsPollSettings, consecutive_failures: int) -> float:
    """Seconds to the next poll: `interval_s` while healthy, else doubling from `interval_s` up to
    `retry_cap_s` (the feeds' FAILURE_RETRY shape at this poller's cadence)."""
    if consecutive_failures <= 0:
        return settings.interval_s
    return float(min(settings.retry_cap_s, settings.interval_s * 2 ** (consecutive_failures - 1)))


# -- ports ------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CallOutcome:
    """The core's answer to one deployment or recall."""

    accepted: bool
    reason_code: str | None = None
    http_status: int | None = None
    detail: str | None = None
    call_id: str | None = None
    duplicate: bool = False


class AsCallGateway(Protocol):
    """The shared AS-deployment core (`opengrid.calls`), seen from the poller."""

    async def deploy(
        self, instruction: DispatchInstruction, *, obligation_id: UUID, principal: str, now: datetime
    ) -> CallOutcome: ...

    async def recall(
        self, deployment_instruction_id: str, *, principal: str, now: datetime
    ) -> CallOutcome: ...

    async def has_call(self, instruction_id: str, *, principal: str) -> bool: ...


class AwardLookup(Protocol):
    async def awards_covering(self, contract_id: UUID, at: datetime) -> list[UUID]:
        """ERCOT_AS obligations of `contract_id` whose delivery window covers `at` (any live state)."""
        ...


class PollAlerts(Protocol):
    async def raise_once(
        self, rule: str, condition_key: str, summary: str, detail: dict[str, Any]
    ) -> None: ...

    async def clear(self, rule: str, condition_key: str) -> None: ...


class PollTrace(Protocol):
    async def append(
        self, stream_id: str, decision_type: str, event_class: str, payload: dict[str, Any]
    ) -> object: ...


# -- the poller -------------------------------------------------------------------------------------------


@dataclass
class ErcotAsPoller:
    source: DispatchInstructionSource
    calls: AsCallGateway
    awards: AwardLookup
    alerts: PollAlerts
    trace: PollTrace
    settings: ErcotAsPollSettings
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    _next_poll_at: datetime | None = field(default=None, init=False)
    _failures: int = field(default=0, init=False)
    _last_success_at: datetime | None = field(default=None, init=False)
    _answered: dict[str, tuple[bool, str | None]] = field(default_factory=dict, init=False)
    _held_recalls: dict[str, DispatchInstruction] = field(default_factory=dict, init=False)
    _superseded: set[str] = field(default_factory=set, init=False)
    _last_error: str = field(default="", init=False)

    @property
    def principal(self) -> str:
        return f"ercot:{self.source.backend}"

    async def poll_if_due(self, *, now: datetime | None = None) -> None:
        """For og-feeds' 5 s tick: poll when due, never raise (a poll error is counted and alerted)."""
        now = now or datetime.now(UTC)
        if self._next_poll_at is not None and now < self._next_poll_at:
            return
        ok = await self.poll_once(now=now)
        self._failures = 0 if ok else self._failures + 1
        self._next_poll_at = now + timedelta(seconds=retry_delay_s(self.settings, self._failures))
        await self._health_alerts(ok, now)

    async def poll_once(self, *, now: datetime) -> bool:
        """One poll and every instruction in it. False when the source could not be read."""
        try:
            batch = await self.source.fetch_instruction_batch(
                now - timedelta(seconds=self.settings.lookback_s)
            )
        except Exception as exc:  # any adapter/transport failure: counted, alerted, retried with backoff
            logger.warning("ERCOT AS instruction poll failed", extra={"error": str(exc)[:300]})
            self._last_error = str(exc)[:300]
            return False
        self._last_success_at = now
        await self.process(batch, now=now)
        return True

    async def process(self, batch: InstructionBatch, *, now: datetime) -> None:
        for bad in batch.malformed:
            await self._answer_malformed(bad.instruction_id, bad.error, now)
        ordered = sorted(batch.instructions, key=lambda i: (i.issued_at, i.instruction_id))
        recalled_in_batch = {i.recalls for i in ordered if i.kind == "AS_RECALL" and i.recalls}
        for instruction in ordered:
            if instruction.instruction_id in self._answered:
                await self._reanswer(instruction)
            elif instruction.kind == "AS_DEPLOYMENT":
                await self._deployment(
                    instruction, now, superseded=instruction.instruction_id in recalled_in_batch
                )
            elif instruction.kind == "AS_RECALL":
                await self._recall(instruction, now)
            else:
                await self._finish(
                    instruction, "AS_INSTRUCTION_NOTED", True, None, now, {"text": instruction.text}
                )

    # -- per kind ----------------------------------------------------------------------------------------

    async def _deployment(self, instruction: DispatchInstruction, now: datetime, *, superseded: bool) -> None:
        # Already applied (a re-delivery, e.g. after a restart): answer it; a recall in the same batch then
        # ends the real call instead of treating the deployment as never started.
        if await self.calls.has_call(instruction.instruction_id, principal=self.principal):
            await self._finish(instruction, "AS_INSTRUCTION_DUPLICATE", True, None, now)
            return
        held = next((r for r in self._held_recalls.values() if r.recalls == instruction.instruction_id), None)
        if superseded or held is not None:
            self._superseded.add(instruction.instruction_id)
            await self._finish(
                instruction, "AS_INSTRUCTION_SUPERSEDED", True, "recalled before it started", now
            )
            if held is not None:
                self._held_recalls.pop(held.instruction_id, None)
                await self._finish(held, "AS_RECALL_APPLIED", True, None, now, {"superseded": True})
            return
        if (now - instruction.issued_at).total_seconds() > self.settings.max_instruction_age_s:
            await self._refuse(instruction, 409, R_LATE, "instruction arrived too late to act on", now)
            return
        obligation_id = await self._resolve(instruction, now)
        if obligation_id is None:
            return
        outcome = await self.calls.deploy(
            instruction, obligation_id=obligation_id, principal=self.principal, now=now
        )
        if outcome.accepted:
            extra = {
                "obligation_id": str(obligation_id),
                "call_id": outcome.call_id,
                "duplicate": outcome.duplicate,
            }
            await self._finish(instruction, "AS_INSTRUCTION_ACCEPTED", True, None, now, extra)
        else:
            await self._refuse_outcome(instruction, outcome, now, obligation_id)

    async def _recall(self, instruction: DispatchInstruction, now: datetime) -> None:
        target = instruction.recalls
        if not target:
            await self._refuse(instruction, 422, R_NOTHING_TO_RECALL, "recall names no deployment", now)
            return
        if not await self.calls.has_call(target, principal=self.principal):
            settled = target in self._superseded or target in self._answered
            held_since = (now - instruction.issued_at).total_seconds()
            if not settled and held_since < self.settings.recall_hold_s:
                # Out of order: its deployment has not arrived. Leave it unacknowledged (the source re-sends it)
                # and supersede the deployment when it comes.
                self._held_recalls[instruction.instruction_id] = instruction
                return
            self._held_recalls.pop(instruction.instruction_id, None)
            await self._finish(instruction, "AS_RECALL_APPLIED", True, None, now, {"nothing_active": True})
            return
        outcome = await self.calls.recall(target, principal=self.principal, now=now)
        self._held_recalls.pop(instruction.instruction_id, None)
        if outcome.accepted:
            await self._finish(
                instruction, "AS_RECALL_APPLIED", True, None, now, {"call_id": outcome.call_id}
            )
        else:
            await self._refuse_outcome(instruction, outcome, now, None)

    async def _resolve(self, instruction: DispatchInstruction, now: datetime) -> UUID | None:
        contract_id = self.settings.award_map.contract_for_as(instruction.resource_id, instruction.service)
        found = (
            []
            if contract_id is None
            else await self.awards.awards_covering(contract_id, instruction.start_at)
        )
        if len(found) == 1:
            return found[0]
        if not found:
            detail = f"no award for {instruction.resource_id}:{instruction.service} at {instruction.start_at}"
            await self._refuse(instruction, 404, R_NO_AWARD, detail, now)
        else:
            await self._refuse(instruction, 409, R_AMBIGUOUS, f"{len(found)} awards cover the start", now)
        return None

    # -- answers -----------------------------------------------------------------------------------------

    async def _answer_malformed(self, instruction_id: str | None, error: str, now: datetime) -> None:
        payload = {"origin": ORIGIN, "instruction_id": instruction_id, "http_status": 422, "error": error}
        await self.trace.append(TRACE_STREAM, TRACE_DECISION_TYPE, "AS_INSTRUCTION_REFUSED", payload)
        # An instruction with no readable id cannot be acknowledged, so the source re-sends it every poll:
        # key its alert on the error itself so it opens once.
        key = instruction_id or f"unreadable:{hashlib.sha256(error.encode()).hexdigest()[:16]}"
        await self.alerts.raise_once(
            ALR_REFUSED,
            f"{ALR_REFUSED}:{key}",
            f"ERCOT AS instruction {key} refused (422 malformed)",
            payload,
        )
        if instruction_id is not None and instruction_id not in self._answered:
            await self._ack(instruction_id, False, f"422 {R_MALFORMED}: {error}"[:200])

    async def _refuse_outcome(
        self,
        instruction: DispatchInstruction,
        outcome: CallOutcome,
        now: datetime,
        obligation_id: UUID | None,
    ) -> None:
        status = outcome.http_status or 409
        await self._refuse(
            instruction, status, outcome.reason_code or "R-REFUSED", outcome.detail or "", now, obligation_id
        )

    async def _refuse(
        self,
        instruction: DispatchInstruction,
        status: int,
        code: str,
        detail: str,
        now: datetime,
        obligation_id: UUID | None = None,
    ) -> None:
        reason = f"{status} {code}: {detail}"[:200]
        extra = {"http_status": status, "reason_code": code, "detail": detail}
        if obligation_id is not None:
            extra["obligation_id"] = str(obligation_id)
        await self._finish(instruction, "AS_INSTRUCTION_REFUSED", False, reason, now, extra)
        await self.alerts.raise_once(
            ALR_REFUSED,
            f"{ALR_REFUSED}:{instruction.instruction_id}",
            f"ERCOT {instruction.service or 'AS'} instruction {instruction.instruction_id} refused ({status} {code})",
            {"origin": ORIGIN, "instruction_id": instruction.instruction_id, "resource": instruction.resource_id,
             "service": instruction.service, "mw": str(instruction.mw), **extra},
        )  # fmt: skip

    async def _finish(
        self,
        instruction: DispatchInstruction,
        event_class: str,
        accepted: bool,
        reason: str | None,
        now: datetime,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Trace (K10), then acknowledge, then remember the answer for duplicates."""
        await self.trace.append(
            TRACE_STREAM, TRACE_DECISION_TYPE, event_class, {**_payload(instruction), **(extra or {}),
                                                              "accepted": accepted, "reason": reason,
                                                              "decided_at": now.isoformat()},
        )  # fmt: skip
        await self._ack(instruction.instruction_id, accepted, reason)

    async def _reanswer(self, instruction: DispatchInstruction) -> None:
        accepted, reason = self._answered[instruction.instruction_id]
        await self.trace.append(
            TRACE_STREAM,
            TRACE_DECISION_TYPE,
            "AS_INSTRUCTION_DUPLICATE",
            {**_payload(instruction), "accepted": accepted},
        )
        await self._ack(instruction.instruction_id, accepted, reason)

    async def _ack(self, instruction_id: str, accepted: bool, reason: str | None) -> None:
        self._answered[instruction_id] = (accepted, reason)
        try:
            receipt = await self.source.acknowledge_instruction(
                instruction_id, accepted=accepted, reason=reason
            )
        except Exception as exc:  # the source re-sends an unacknowledged instruction; the answer is kept
            logger.warning(
                "ERCOT AS acknowledgement failed", extra={"instruction_id": instruction_id, "error": str(exc)}
            )
            return
        if not receipt.accepted:
            logger.warning(
                "ERCOT AS acknowledgement not accepted",
                extra={"instruction_id": instruction_id, "status": receipt.status, "errors": receipt.errors},
            )

    # -- poll health -------------------------------------------------------------------------------------

    async def _health_alerts(self, ok: bool, now: datetime) -> None:
        if ok:
            await self.alerts.clear(ALR_POLL_FAILED, ALR_POLL_FAILED)
            await self.alerts.clear(ALR_POLL_STALE, ALR_POLL_STALE)
            return
        detail = {"origin": ORIGIN, "consecutive_failures": self._failures, "error": self._last_error}
        if self._failures >= self.settings.failure_alert_after:
            await self.alerts.raise_once(
                ALR_POLL_FAILED,
                ALR_POLL_FAILED,
                f"ERCOT AS instruction poll failing ({self._failures} in a row)",
                detail,
            )
        last_good = self._last_success_at or self.started_at
        if (now - last_good).total_seconds() >= self.settings.stale_after_s:
            await self.alerts.raise_once(
                ALR_POLL_STALE,
                ALR_POLL_STALE,
                f"No ERCOT AS instructions read since {last_good.isoformat(timespec='seconds')}: deployments may be missed",
                {**detail, "last_success_at": last_good.isoformat()},
            )


def _payload(instruction: DispatchInstruction) -> dict[str, Any]:
    return {
        "origin": ORIGIN,
        "instruction_id": instruction.instruction_id,
        "kind": instruction.kind,
        "resource": instruction.resource_id,
        "service": instruction.service,
        "mw": str(instruction.mw) if instruction.mw is not None else None,
        "start_at": instruction.start_at.isoformat(),
        "end_at": instruction.end_at.isoformat() if instruction.end_at else None,
        "ramp_minutes": instruction.ramp_minutes,
        "recalls": instruction.recalls,
        "issued_at": instruction.issued_at.isoformat(),
    }
