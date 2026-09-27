"""Grid-link service for ONE utility: protocol-neutral command handling, fail-safe heartbeat watchdog,
L2 levels and outbound status (grid-link.md S5; decision log D-34).

Transports (DNP3 today; ICCP later) call `offer(command, peer)` synchronously for every inbound control
and get a `ControlVerdict` back at once. Accepted commands are queued and applied, in order, by `run()`:

- every command except a heartbeat is traced first (decision OPERATOR_ACTION, event GRID_LINK_COMMAND,
  `origin = GRID_LINK`); refusals are traced as AUTHZ_DENY (rate-limited per peer and reason);
- a toll call goes through `TollCallPort` -- the SAME core function the operator route and the customer
  API use (`opengrid.calls`); nothing here decides whether a call is valid beyond the link's own limits;
- L2 LIMIT/BLOCK levels go through `L2Book` and are delivered as `ScadaUtilityInstruction`s into the
  `ScadaSink` (the MQTT bridge in production, so the guardian's independent L2 port sees them, K5).

Fail safe (S5.3): the link is healthy only while heartbeats arrive within `heartbeat_timeout_s`. When it
is not, NEW toll calls are refused (INHIBITED); cancels are still accepted; a call already running
continues to its own end (bounded by its product rule, D-29); L2 levels stay exactly as last received.
Across an og-engine restart the L2 levels are rebuilt from the link's own trace (estore_l2, pg_history).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from opengrid.core.models.mqtt import ScadaUtilityInstruction
from opengrid.integrations.grid_link.config import UtilityLinkSettings
from opengrid.integrations.grid_link.l2 import L2Book
from opengrid.integrations.grid_link.model import (
    BankStatus,
    CallOutcome,
    CallPhase,
    CancelCall,
    ControlVerdict,
    GridCommand,
    Heartbeat,
    L2Block,
    L2Limit,
    L2LimitValue,
    LinkStatus,
    TollCall,
    reason_number,
)
from opengrid.integrations.grid_link.ports import BanksOfZone, TelemetryPort, TollCallPort, TraceAppend
from opengrid.integrations.interfaces import ScadaSink

logger = logging.getLogger(__name__)

__all__ = ["ORIGIN", "GridLinkService"]

ORIGIN = "GRID_LINK"
TRACE_DECISION = "OPERATOR_ACTION"
TRACE_EVENT_COMMAND = "GRID_LINK_COMMAND"
TRACE_EVENT_LINK = "GRID_LINK_STATE"
TRACE_DENY_DECISION = "AUTHZ_DENY"
TRACE_EVENT_DENY = "GRID_LINK_DENY"
#: A hostile or misconfigured peer must not flood the trace: one deny per (peer, reason) per window.
DENY_TRACE_MIN_INTERVAL_S = 5.0
#: Bounded work queue: a stalled core never grows memory without limit (overflow -> INHIBITED).
COMMAND_QUEUE_MAX = 256
#: Largest L2 limit value accepted (kW); anything above is a units error on the EMS side.
MAX_L2_LIMIT_KW = 1_000_000.0
_MAX_CALL_ID = 2**31 - 1
_LIVE_PHASES = (CallPhase.ACCEPTED, CallPhase.ACTIVE)


@dataclass(frozen=True, slots=True)
class _Work:
    command: GridCommand
    peer: str


@dataclass(frozen=True, slots=True)
class _Deny:
    peer: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class _CallView:
    ems_call_id: int
    outcome: CallOutcome


class GridLinkService:
    """One utility's link state and command worker (module docstring)."""

    def __init__(
        self,
        settings: UtilityLinkSettings,
        *,
        calls: TollCallPort,
        telemetry: TelemetryPort,
        trace: TraceAppend,
        sink: ScadaSink,
        banks_of_zone: BanksOfZone,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self.utility_id = settings.utility_id
        self._calls = calls
        self._telemetry = telemetry
        self._trace = trace
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(UTC))
        self._monotonic = monotonic or time.monotonic
        self._stream = f"grid_link:{self.utility_id}"
        self.l2 = L2Book(
            settings.l2_targets,
            source=self._stream,
            issued_by=settings.issued_by,
            banks_of_zone=banks_of_zone,
        )
        self._queue: asyncio.Queue[_Work | _Deny] = asyncio.Queue(maxsize=COMMAND_QUEUE_MAX)
        self._last_heartbeat: float | None = None
        self._heartbeats = 0
        self._reported_healthy = False
        self._deny_seen: dict[tuple[str, str], float] = {}
        self._call: _CallView | None = None
        self._banks: tuple[BankStatus, ...] = ()
        self._unsent: list[ScadaUtilityInstruction] = []

    # -- synchronous side (called by transports) ------------------------------------------------------

    def link_healthy(self) -> bool:
        last = self._last_heartbeat
        return last is not None and self._monotonic() - last <= self.settings.heartbeat_timeout_s

    def validate(self, command: GridCommand) -> ControlVerdict:
        """The link's own limits and the per-utility allow-list; never the call's commercial validity."""
        s = self.settings
        match command:
            case Heartbeat():
                return ControlVerdict.ACCEPTED
            case TollCall(ems_call_id=call_id, setpoint_kw=kw, duration_min=minutes):
                if not s.accept_toll_calls:
                    return ControlVerdict.NOT_AUTHORIZED
                if not self.link_healthy():
                    return ControlVerdict.INHIBITED
                if not (0 < kw <= s.max_setpoint_kw and 1 <= minutes <= s.max_duration_min):
                    return ControlVerdict.OUT_OF_RANGE
                return ControlVerdict.ACCEPTED if 0 < call_id <= _MAX_CALL_ID else ControlVerdict.OUT_OF_RANGE
            case CancelCall():
                return ControlVerdict.ACCEPTED if s.accept_toll_calls else ControlVerdict.NOT_AUTHORIZED
            case L2LimitValue(target=target, limit_kw=limit_kw):
                verdict = self._l2_verdict(target)
                if verdict is ControlVerdict.ACCEPTED and not 0 <= limit_kw <= MAX_L2_LIMIT_KW:
                    return ControlVerdict.OUT_OF_RANGE
                return verdict
            case L2Limit(target=target) | L2Block(target=target):
                return self._l2_verdict(target)
        return ControlVerdict.NOT_SUPPORTED

    def _l2_verdict(self, target: str) -> ControlVerdict:
        if not self.settings.accept_l2:
            return ControlVerdict.NOT_AUTHORIZED
        return ControlVerdict.ACCEPTED if self.l2.knows(target) else ControlVerdict.NOT_SUPPORTED

    def offer(self, command: GridCommand, peer: str) -> ControlVerdict:
        """Validate and, when accepted, queue `command`. Heartbeats are applied at once (never queued), so
        a slow database can never make a live link look dead."""
        verdict = self.validate(command)
        if verdict is not ControlVerdict.ACCEPTED:
            self.deny(peer, f"control-{verdict.value.lower()}", type(command).__name__)
            return verdict
        if isinstance(command, Heartbeat):
            self._last_heartbeat = self._monotonic()
            self._heartbeats += 1
            return verdict
        try:
            self._queue.put_nowait(_Work(command, peer))
        except asyncio.QueueFull:
            logger.error("grid link command queue full; refusing", extra={"utility_id": self.utility_id})
            return ControlVerdict.INHIBITED
        return verdict

    def deny(self, peer: str, reason: str, detail: str) -> None:
        """Record a refusal (connection or control) for the AUTHZ_DENY trace, rate-limited."""
        key, now = (peer, reason), self._monotonic()
        last = self._deny_seen.get(key)
        if last is not None and now - last < DENY_TRACE_MIN_INTERVAL_S:
            return
        self._deny_seen[key] = now
        logger.warning(
            "grid link refusal", extra={"utility_id": self.utility_id, "peer": peer, "reason": reason}
        )
        with contextlib.suppress(asyncio.QueueFull):
            self._queue.put_nowait(_Deny(peer, reason, detail))

    def status(self) -> LinkStatus:
        """The outbound snapshot (cached telemetry and call view; refreshed by `tick`)."""
        banks = tuple(replace(b, l2_ceiling_kw=self.l2.ceiling_kw(b.bank_id)) for b in self._banks)
        capacity = sum(b.capacity_kwh for b in banks)
        call = self._call
        outcome = call.outcome if call is not None else CallOutcome(CallPhase.IDLE)
        return LinkStatus(
            available_kw=round(sum(b.available_kw for b in banks), 3),
            delivered_kw=round(sum(b.delivered_kw for b in banks), 3),
            call_phase=outcome.phase,
            ems_call_id=call.ems_call_id if call is not None else 0,
            call_reason=reason_number(outcome.reason_code),
            call_granted_kw=outcome.granted_kw or 0.0,
            soc_pct=round(100.0 * sum(b.soc_kwh for b in banks) / capacity, 2) if capacity > 0 else None,
            heartbeat_count=self._heartbeats,
            link_healthy=self.link_healthy(),
            telemetry_stale=len(banks) < len(self.settings.banks) or any(b.soc_pct is None for b in banks),
            toll_calls_enabled=self.settings.accept_toll_calls,
            banks=banks,
            targets=self.l2.target_status(),
        )

    # -- asynchronous side ------------------------------------------------------------------------------

    async def run(self) -> None:
        """Apply queued commands in order and refresh status every `status_refresh_s`. Never returns;
        one failing item is logged and never stops the worker."""
        interval = self.settings.status_refresh_s
        next_tick = self._monotonic()
        while True:
            timeout = max(0.0, next_tick - self._monotonic())
            try:
                item = await asyncio.wait_for(self._queue.get(), timeout=timeout)
            except TimeoutError:
                item = None
            try:
                if item is not None:
                    await self.handle(item)
                if self._monotonic() >= next_tick:
                    await self.tick()
                    next_tick = self._monotonic() + interval
            except Exception:
                logger.exception("grid link worker item failed", extra={"utility_id": self.utility_id})

    async def drain(self) -> None:
        """Apply everything queued now (tests and shutdown)."""
        while not self._queue.empty():
            await self.handle(self._queue.get_nowait())

    async def handle(self, item: _Work | _Deny) -> None:
        if isinstance(item, _Deny):
            await self._append(
                TRACE_DENY_DECISION,
                TRACE_EVENT_DENY,
                {"peer": item.peer, "reason": item.reason, "detail": item.detail},
            )
            return
        traced = await self._append(TRACE_DECISION, TRACE_EVENT_COMMAND, _command_payload(item))
        match item.command:
            case TollCall() as call:
                await self._issue(call, traced=traced)
            case CancelCall(ems_call_id=call_id):
                await self._cancel(call_id)
            case L2LimitValue() | L2Limit() | L2Block() as l2_command:
                self._apply_l2(l2_command)
                await self._publish_l2()

    async def restore_l2(self, payloads: list[dict[str, Any]]) -> int:
        """Re-apply traced L2 commands (oldest first; `pg_history.load_l2_commands`) at start-up, WITHOUT
        tracing them again, then deliver the resulting instructions so the fleet twin and the guardian see
        the levels the link held before the restart. Commands for targets no longer configured are skipped.
        Returns how many commands were applied."""
        applied = 0
        for payload in payloads:
            command = _l2_command(payload)
            if command is None or not self.l2.knows(command.target):
                continue
            self._apply_l2(command)
            applied += 1
        if applied:
            await self._publish_l2()
            await self._append(
                TRACE_DECISION, TRACE_EVENT_LINK, {"state": "L2_RESTORED", "commands": applied}
            )
            logger.info(
                "grid link L2 levels restored", extra={"utility_id": self.utility_id, "commands": applied}
            )
        return applied

    def _apply_l2(self, command: L2LimitValue | L2Limit | L2Block) -> None:
        match command:
            case L2LimitValue(target=target, limit_kw=limit_kw):
                self.l2.set_limit_value(target, limit_kw)
            case L2Limit(target=target, active=active):
                self.l2.set_limit_active(target, active)
            case L2Block(target=target, active=active):
                self.l2.set_block(target, active)

    async def tick(self) -> None:
        """Heartbeat transitions, telemetry refresh, live-call status refresh, and L2 re-delivery."""
        await self._watch_heartbeat()
        try:
            self._banks = tuple(await self._telemetry.bank_status(self.settings.banks))
        except Exception:
            logger.exception("grid link telemetry read failed", extra={"utility_id": self.utility_id})
            self._banks = ()
        await self._refresh_call()
        if self._unsent:
            await self._deliver([])

    # -- internals ---------------------------------------------------------------------------------------

    async def _append(self, decision: str, event: str, payload: dict[str, Any]) -> bool:
        body = {"origin": ORIGIN, "utility_id": self.utility_id, **payload}
        try:
            await self._trace(self._stream, decision, event, body)
        except Exception:
            logger.exception("grid link trace append failed", extra={"utility_id": self.utility_id})
            return False
        return True

    async def _issue(self, call: TollCall, *, traced: bool) -> None:
        if not traced:  # K10: a call that cannot be traced is not issued
            self._call = _CallView(call.ems_call_id, CallOutcome(CallPhase.REJECTED, "R-GL-INTERNAL"))
            return
        try:
            outcome = await asyncio.wait_for(
                self._calls.issue(self.utility_id, call.ems_call_id, call.setpoint_kw, call.duration_min),
                timeout=self.settings.core_timeout_s,
            )
        except TimeoutError:
            outcome = CallOutcome(CallPhase.REJECTED, "R-GL-CORE-TIMEOUT")  # re-read by `_refresh_call`
        except Exception:
            logger.exception("grid link toll call failed", extra={"utility_id": self.utility_id})
            outcome = CallOutcome(CallPhase.REJECTED, "R-GL-INTERNAL")
        self._call = _CallView(call.ems_call_id, outcome)

    async def _cancel(self, ems_call_id: int | None) -> None:
        target = ems_call_id if ems_call_id is not None else (self._call.ems_call_id if self._call else None)
        if target is None:
            return
        try:
            outcome = await asyncio.wait_for(
                self._calls.cancel(self.utility_id, target), timeout=self.settings.core_timeout_s
            )
        except Exception:
            logger.exception("grid link cancel failed", extra={"utility_id": self.utility_id})
            return
        # the call points always show the latest call event, so a cancel is visible even after a newer
        # call was refused
        self._call = _CallView(target, outcome)

    async def _refresh_call(self) -> None:
        call = self._call
        if call is None:
            return
        pending = call.outcome.phase in _LIVE_PHASES or call.outcome.reason_code == "R-GL-CORE-TIMEOUT"
        if not pending:
            return
        try:
            outcome = await asyncio.wait_for(
                self._calls.status(self.utility_id, call.ems_call_id), timeout=self.settings.core_timeout_s
            )
        except Exception:
            logger.warning("grid link call status read failed", extra={"utility_id": self.utility_id})
            return
        if self._call is call:
            self._call = _CallView(call.ems_call_id, outcome)

    async def _watch_heartbeat(self) -> None:
        healthy = self.link_healthy()
        if healthy == self._reported_healthy:
            return
        self._reported_healthy = healthy
        state = "HEALTHY" if healthy else "HEARTBEAT_LOST"
        log = logger.info if healthy else logger.error
        log("grid link state change", extra={"utility_id": self.utility_id, "state": state})
        await self._append(
            TRACE_DECISION,
            TRACE_EVENT_LINK,
            {"state": state, "heartbeat_timeout_s": self.settings.heartbeat_timeout_s},
        )

    async def _publish_l2(self) -> None:
        await self._deliver(self.l2.instructions(self._clock()))

    async def _deliver(self, instructions: list[ScadaUtilityInstruction]) -> None:
        """Deliver new instructions after any earlier undelivered ones; keep what fails for the next tick
        (the tracker has already moved on, so a lost instruction would otherwise never be re-sent)."""
        pending, self._unsent = [*self._unsent, *instructions], []
        for index, instruction in enumerate(pending):
            try:
                await self._sink.on_utility_instruction(instruction)
            except Exception:
                logger.exception(
                    "grid link L2 delivery failed; will retry", extra={"utility_id": self.utility_id}
                )
                self._unsent = pending[index:]
                return


def _l2_command(payload: dict[str, Any]) -> L2LimitValue | L2Limit | L2Block | None:
    """Rebuild an L2 command from its GRID_LINK_COMMAND trace payload (`_command_payload`); None when malformed."""
    try:
        target = str(payload["target"])
        match payload.get("command"):
            case "L2LimitValue":
                return L2LimitValue(target, float(payload["limit_kw"]))
            case "L2Limit":
                return L2Limit(target, bool(payload["active"]))
            case "L2Block":
                return L2Block(target, bool(payload["active"]))
    except (KeyError, TypeError, ValueError):
        logger.warning("skipped a malformed traced L2 command", extra={"payload_keys": sorted(payload)})
    return None


def _command_payload(work: _Work) -> dict[str, Any]:
    command = work.command
    fields = {k: getattr(command, k) for k in getattr(command, "__slots__", ())}
    return {"peer": work.peer, "command": type(command).__name__, **fields}
