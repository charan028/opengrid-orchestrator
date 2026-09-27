"""The engine's `allocator.gateways.CycleExtrasGateway`: the per-cycle inputs beyond S1-S7's core, and what
happens with each cycle's result.

Before the cycle (`extras`):
- closed-loop caps from the DATA_CENTER/PIPELINE_AC controllers (`engine.closed_loop`, when enabled);
- the S5.2 PQ context (`engine.pq_eligibility.dispatch_context`) with the S5.4 ladder's hub exclusions;
- K15 territory enforcement and the 09 S1.9 flow limits, from config.

After the cycle (`observe`):
- the controllers are reconciled with what was granted, and a controller mode change is traced;
- `PQ_CAPABILITY_REDUCED` is traced when PQ eligibility leaves an obligation less than it needs (on entry,
  not every 2 s), `TERRITORY_BLOCK` likewise for a K15 block;
- the S5.4 ladder (`engine.pq_ladder`) acts on each monitored obligation's PCC measurement.

Nothing here ever fails the cycle: a broken input degrades to the safe default (no caps = the committed
schedule; no PQ verdicts = no eligible hub for a PQ-sensitive profile, fail closed) and is logged (K7).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from opengrid.allocator.models import (
    CycleExtras,
    CycleResult,
    FleetState,
    FlowLimits,
    LedgerView,
    PqDispatchContext,
)
from opengrid.engine.closed_loop import ClosedLoopRunner
from opengrid.engine.flow_topology import FlowTopology
from opengrid.engine.pq_ladder import PqLadderExecutor, TracePort

logger = logging.getLogger(__name__)

PQ_CAPABILITY_REDUCED = "PQ_CAPABILITY_REDUCED"
TERRITORY_BLOCK = "TERRITORY_BLOCK"
CLOSED_LOOP_MODE = "CLOSED_LOOP_MODE"

PqContextSource = Callable[[Mapping[str, frozenset[str]]], PqDispatchContext]


class EngineCycleExtras:
    def __init__(
        self,
        trace: TracePort,
        *,
        pq_context: PqContextSource,
        closed_loop: ClosedLoopRunner | None = None,
        ladder: PqLadderExecutor | None = None,
        enforce_territory: bool = True,
        flow_limits: FlowLimits | None = None,
        flow_topology: FlowTopology | None = None,
        excluded_hub_ids: Callable[[], frozenset[str]] | None = None,
        operator_hub_ids: Callable[[], frozenset[str]] | None = None,
        device_excluded_hub_ids: Callable[[], frozenset[str]] | None = None,
    ) -> None:
        self._trace = trace
        self._pq_context = pq_context
        self._closed_loop = closed_loop
        self._ladder = ladder
        self._enforce_territory = enforce_territory
        self._flow_limits = flow_limits or FlowLimits()
        self._flow_topology = flow_topology
        self._excluded_hub_ids = excluded_hub_ids or frozenset
        self._operator_hub_ids = operator_hub_ids or frozenset
        self._device_excluded_hub_ids = device_excluded_hub_ids or frozenset
        self._reduced: set[tuple[str, str]] = set()
        self._blocked: set[tuple[str, str | None, str]] = set()
        self._last_pq: PqDispatchContext = PqDispatchContext()

    async def extras(self, fleet_state: FleetState, ledger_view: LedgerView, now: datetime) -> CycleExtras:
        caps: dict[tuple[str, str], float] = {}
        if self._closed_loop is not None:
            try:
                caps = await self._closed_loop.caps(fleet_state, ledger_view, now)
            except Exception:
                logger.exception(
                    "closed-loop controllers failed; obligations follow their schedule this cycle"
                )
        excluded = self._ladder.excluded_by_obligation() if self._ladder is not None else {}
        try:
            pq = self._pq_context(excluded)
            self._last_pq = pq
        except Exception:
            logger.exception("PQ context unavailable; PQ-sensitive profiles keep the last verdicts")
            pq = self._last_pq
        flow_limits = self._flow_limits
        if self._flow_topology is not None:
            flow_limits = await self._flow_topology.limits(now)
        return CycleExtras(
            closed_loop_caps=caps,
            pq=pq,
            enforce_territory=self._enforce_territory,
            flow_limits=flow_limits,
            excluded_hub_ids=self._excluded_hub_ids(),
            operator_hub_ids=self._operator_hub_ids(),
            device_excluded_hub_ids=self._device_excluded_hub_ids(),
        )

    async def observe(self, result: CycleResult, ledger_view: LedgerView, now: datetime) -> None:
        if self._closed_loop is not None:
            self._closed_loop.reconcile(result)
            for change in self._closed_loop.mode_changes:
                await self._append(
                    f"closed-loop-{change.obligation_id}",
                    CLOSED_LOOP_MODE,
                    {"obligation_id": change.obligation_id, "mode": change.mode},
                    [change.reason_code],
                )
        await self._trace_reductions(result)
        await self._trace_blocks(result)
        await self._run_ladder(result, ledger_view, now)

    async def _trace_reductions(self, result: CycleResult) -> None:
        current = {(r.obligation_id, r.bank_id) for r in result.pq_reductions}
        for reduction in result.pq_reductions:
            key = (reduction.obligation_id, reduction.bank_id)
            if key in self._reduced:
                continue
            await self._append(
                f"pq-capability-{reduction.obligation_id}",
                PQ_CAPABILITY_REDUCED,
                {
                    "obligation_id": reduction.obligation_id,
                    "bank_id": reduction.bank_id,
                    "needed_kw": reduction.needed_kw,
                    "capability_before_kw": reduction.capability_before_kw,
                    "capability_after_kw": reduction.capability_after_kw,
                    "excluded_hub_ids": list(reduction.excluded_hub_ids),
                },
                [PQ_CAPABILITY_REDUCED],
            )
        self._reduced = current

    async def _trace_blocks(self, result: CycleResult) -> None:
        current = {(b.bank_id, b.obligation_id, b.reason_code) for b in result.territory_blocks}
        for bank_id, obligation_id, reason in sorted(current - self._blocked, key=str):
            await self._append(
                f"territory-{bank_id}",
                TERRITORY_BLOCK,
                {"bank_id": bank_id, "obligation_id": obligation_id},
                [reason],
            )
        self._blocked = current

    async def _run_ladder(self, result: CycleResult, ledger_view: LedgerView, now: datetime) -> None:
        if self._ladder is None or self._closed_loop is None:
            return
        try:
            measurements = self._closed_loop.measurements(now)
        except Exception:
            logger.exception("PQ measurements unavailable this cycle")
            return
        allocated: dict[str, list[str]] = {}
        for alloc in result.hub_allocations:
            allocated.setdefault(alloc.obligation_id, []).extend(h for h, _ in alloc.per_hub_kw)
        eligible_by_service = {
            svc: set(r.eligible_hub_ids) for svc, r in self._last_pq.results_by_service.items()
        }
        excluded = self._ladder.excluded_by_obligation()
        for oid, (measurement, limits) in measurements.items():
            spec = self._closed_loop.specs.get(oid)
            banks = {c.bank_id for c in ledger_view.calls if c.obligation_id == oid}
            pool = {
                h
                for c in ledger_view.calls
                if c.obligation_id == oid
                for h in c.eligible_hub_ids
                if spec is None or h in eligible_by_service.get(spec.service_type, set(c.eligible_hub_ids))
            }
            spare = len(pool - set(allocated.get(oid, [])) - excluded.get(oid, frozenset()))
            if not banks:
                continue
            try:
                await self._ladder.observe(
                    oid, measurement, limits, allocated.get(oid, []), spare_hubs=spare, now=now
                )
            except Exception:
                logger.exception("PQ ladder step failed", extra={"obligation_id": oid})
        self._ladder.forget(set(measurements))

    async def _append(
        self, stream: str, event_class: str, payload: dict[str, Any], reasons: list[str]
    ) -> None:
        try:
            await self._trace.append(stream, "ALERT", event_class, payload, reason_codes=reasons)
        except Exception:
            logger.exception("could not trace cycle extras event", extra={"event_class": event_class})
