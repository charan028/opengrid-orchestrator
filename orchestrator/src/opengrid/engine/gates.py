"""Running the due selector gates inside the og-engine tick (02a S3.1), isolated from the tick's 2 s
dispatch work.

A gate that fails (solver error, commit-time exception, feed problem) must never cost the allocator
cycle that serves obligations already committed (K7 degrade-don't-trip, K13 commitment lock): each
failure is logged, traced and raised as an `ALR-SELECTOR-GATE-FAILED` alert, and the tick continues.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from opengrid.health.model import AlertFinding

logger = logging.getLogger(__name__)

ALR_SELECTOR_GATE_FAILED = "ALR-SELECTOR-GATE-FAILED"
_GATE_TRACE_STREAM = "engine-gates"


class Trigger(Protocol):
    @property
    def gate_kind(self) -> Any: ...

    @property
    def contract_scope(self) -> UUID | None: ...


class TraceAppender(Protocol):
    async def append(
        self, stream_id: str, decision_type: Any, event_class: str, payload: dict[str, Any], /
    ) -> object: ...


RunIntake = Callable[..., Awaitable[object]]
RunGate = Callable[..., Awaitable[object]]
RaiseAlert = Callable[[AlertFinding], Awaitable[object]]
OnRenomination = Callable[[UUID, UUID | None], Awaitable[object]]
ClearFailure = Callable[[str, UUID | None], Awaitable[object]]
#: True while `og.degraded_mode_state` holds NO_NEW_COMMITMENTS (02b S6.5 row 1: a feed crossed STALE).
NoNewCommitments = Callable[[], Awaitable[bool]]

NO_NEW_COMMITMENTS = "NO_NEW_COMMITMENTS"


async def intake_blocked(no_new_commitments: NoNewCommitments | None) -> bool:
    """Whether intake must not turn opportunities into candidates now. An unreadable degraded-mode state
    blocks it (fail closed): no new commitment is ever built on feeds that may be stale."""
    if no_new_commitments is None:
        return False
    try:
        return await no_new_commitments()
    except Exception:
        logger.exception("degraded-mode state unreadable; intake blocked (fail closed)")
        return True


def gate_failure_matches(gate_kind: str, contract_scope: UUID | None) -> Callable[[dict[str, Any]], bool]:
    """Matches the `detail` `_report_gate_failure` stores for this gate kind and contract scope."""
    scope = str(contract_scope) if contract_scope else None
    return lambda detail: detail.get("gate_kind") == gate_kind and detail.get("contract_scope") == scope


async def run_due_gates(
    triggers: list[Any],
    *,
    now: datetime,
    run_intake: RunIntake,
    run_gate: RunGate,
    trace: TraceAppender,
    raise_alert: RaiseAlert,
    on_renomination: OnRenomination | None = None,
    observe_duration: Callable[[str, float], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    clear_failure: ClearFailure | None = None,
    no_new_commitments: NoNewCommitments | None = None,
) -> int:
    """Run intake then the selector gate for each trigger. Returns the number of gates that failed.
    `observe_duration(gate_kind, seconds)` receives each gate's wall time (intake + gate), failed or not."""
    failed = 0
    for trigger in triggers:
        started = clock()
        try:
            gate_failed = await _run_one(
                trigger,
                now=now,
                run_intake=run_intake,
                run_gate=run_gate,
                trace=trace,
                raise_alert=raise_alert,
                on_renomination=on_renomination,
                clear_failure=clear_failure,
                no_new_commitments=no_new_commitments,
            )
        finally:
            if observe_duration is not None:
                observe_duration(str(trigger.gate_kind), clock() - started)
        failed += int(gate_failed)
    return failed


async def _run_one(
    trigger: Any,
    *,
    now: datetime,
    run_intake: RunIntake,
    run_gate: RunGate,
    trace: TraceAppender,
    raise_alert: RaiseAlert,
    on_renomination: OnRenomination | None,
    clear_failure: ClearFailure | None = None,
    no_new_commitments: NoNewCommitments | None = None,
) -> bool:
    """One trigger: intake, then the gate, then its re-nomination points. Returns True if the gate failed."""
    scope = {"gate_kind": trigger.gate_kind, "contract_scope": trigger.contract_scope}
    logger.info("running gate", extra=scope)
    # Intake generates this gate's OFFERED opportunities from live feeds first; a feed hiccup must
    # not block the gate itself. Under NO_NEW_COMMITMENTS (a feed crossed STALE) intake is skipped, so no
    # new candidate reaches the gate; the gate still runs for what is already committed (K13).
    if await intake_blocked(no_new_commitments):
        logger.warning("intake skipped: NO_NEW_COMMITMENTS", extra=scope)
        try:
            await trace.append(
                _GATE_TRACE_STREAM,
                "ALERT",
                "INTAKE_SKIPPED",
                {
                    "gate_kind": str(trigger.gate_kind),
                    "contract_scope": str(trigger.contract_scope) if trigger.contract_scope else None,
                    "mode": NO_NEW_COMMITMENTS,
                },
            )
        except Exception:
            logger.exception("could not trace a skipped intake", extra=scope)
    else:
        try:
            await run_intake(trigger.gate_kind, trigger.contract_scope, now=now)
        except Exception:
            logger.exception("intake failed ahead of gate -- running the gate anyway", extra=scope)
    try:
        plan = await run_gate(trigger.gate_kind, trigger.contract_scope)
    except Exception as exc:
        logger.exception("selector gate failed", extra=scope)
        await _report_gate_failure(trigger, exc, trace=trace, raise_alert=raise_alert)
        return True
    if clear_failure is not None:
        # The alert raised on an earlier failure of this gate is ours to clear once it succeeds again
        # (health only auto-clears its own rules).
        try:
            await clear_failure(str(trigger.gate_kind), trigger.contract_scope)
        except Exception:
            logger.exception("could not clear a resolved gate-failure alert", extra=scope)
    if trigger.gate_kind == "RENOMINATION" and on_renomination is not None and trigger.contract_scope:
        # The gate reached the contract's due re-nomination point(s): record them exercised, or the
        # engine re-runs this gate every tick (02a S1.7).
        try:
            await on_renomination(trigger.contract_scope, getattr(plan, "plan_id", None))
        except Exception:
            logger.exception("could not exercise re-nomination point(s)", extra=scope)
    return False


async def _report_gate_failure(
    trigger: Any, exc: Exception, *, trace: TraceAppender, raise_alert: RaiseAlert
) -> None:
    detail: dict[str, Any] = {
        "gate_kind": trigger.gate_kind,
        "contract_scope": str(trigger.contract_scope) if trigger.contract_scope else None,
        "error": type(exc).__name__,
        "reason_code": getattr(exc, "reason_code", None),
    }
    try:
        await trace.append(_GATE_TRACE_STREAM, "ALERT", "GATE_FAILED", detail)
        await raise_alert(
            AlertFinding(
                rule=ALR_SELECTOR_GATE_FAILED,
                severity="warning",
                summary=f"Selector gate {trigger.gate_kind} failed ({type(exc).__name__})",
                condition_key=f"gate:{trigger.gate_kind}:{detail['contract_scope']}",
                detail=detail,
            )
        )
    except Exception:
        logger.exception("failed to trace/alert a selector gate failure", extra=detail)
