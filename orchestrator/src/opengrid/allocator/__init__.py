"""opengrid.allocator -- the 2-second S1-S7 real-time cycle (02a S5). Owner: allocator agent
(BUILD.md S4).

Pure-logic core (`opengrid.allocator.cycle.cycle`) plus a thin adapter (`run_cycle`/`substitute_hub`
below): reads `fleet.capability`, plans grants respecting `opengrid.core.limits` (planning-time
checks), runs the `DIST_DEFERRAL` PI loop and water-filling with dwell/hysteresis, and hands the
proposed batch to `guardian` for signing.

The allocator NEVER selects new opportunities and NEVER reallocates a committed obligation's capacity
to a different obligation (K13) -- see `opengrid.allocator.cycle` for the full invariant discussion.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Awaitable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from opengrid.allocator import reasons
from opengrid.allocator.cycle import DEFAULT_LEASE_TTL_S, PrepMemoEntry, cycle
from opengrid.allocator.gateways import (
    CycleExtrasGateway,
    FleetGateway,
    LedgerGateway,
    ScadaGateway,
    ScheduleGateway,
)
from opengrid.allocator.models import (
    CycleExtras,
    CycleResult,
    DwellState,
    FleetState,
    PiState,
    ProposedGrant,
    Schedule,
    ShortfallReport,
)
from opengrid.core.models.engine import Grant

__all__ = ["configure", "cycle", "hub_allocations", "run_cycle", "substitute_hub"]

logger = logging.getLogger(__name__)

# One DIST_DEFERRAL PI integrator and one dwell tracker per bank, kept alive for the life of the
# process (K9: exactly one integrating controller per bank kVA loop). Module-level because
# `og-engine` runs the allocator as a single long-lived process (02a S0 "Stack").
_pi_states: dict[str, PiState] = {}
_dwell_states: dict[str, DwellState] = {}

# ALLOC-05: every gateway await in `run_cycle` is bounded by this timeout (K7 "degrade, don't trip" --
# a hung fleet/ledger/scada/schedule read must never hang the 2 s allocation cycle indefinitely). On
# timeout, `run_cycle` holds the last cycle's signed-off grants rather than propose a fresh (and
# possibly stale-input) batch, and traces `reasons.R_GATEWAY_TIMEOUT` (2 s: `01-saturday-delivery-plan`
# S0's own cycle budget -- a gateway that cannot answer within it should not block the cycle at all).
GATEWAY_TIMEOUT_S = 2.0

# The most recent cycle's successfully-built grant rows, held and re-returned verbatim on a gateway
# timeout (K7) rather than proposing a batch built from partial/stale inputs.
_last_grants: list[Grant] = []

#: The last cycle's per-hub realization of PQ-sensitive obligations, `(obligation_id, bank_id) ->
#: {hub_id: kW}`: the engine builds those obligations' hub items from it (only PQ-eligible hubs deliver).
_last_hub_allocations: dict[tuple[str, str], dict[str, float]] = {}
#: The current cycle's extras, shortfalls and pre-cycle PI/dwell states, so a same-cycle re-proposal
#: (`only_bank_ids`) neither re-steps controllers nor erases other banks' state.
_last_extras: list[CycleExtras] = []
_last_shortfalls: list[ShortfallReport] = []
_pre_cycle_pi: dict[str, PiState] = {}
_pre_cycle_dwell: dict[str, DwellState] = {}
#: The cycle's per-hub preparation memo (`cycle.cycle`'s `prep_memo`): pure work reused while a hub's inputs
#: are unchanged. Keyed by hub; rebuilt when hubs leave the fleet.
_prep_memo: dict[str, PrepMemoEntry] = {}


def hub_allocations() -> dict[tuple[str, str], dict[str, float]]:
    """The last cycle's per-hub allocations of PQ-sensitive obligations (a copy)."""
    return {key: dict(per_hub) for key, per_hub in _last_hub_allocations.items()}


async def _with_gateway_timeout[T](awaitable: Awaitable[T], *, gateway_name: str) -> T:
    """Bound one gateway call to `GATEWAY_TIMEOUT_S`, re-raising as `TimeoutError` with the gateway's
    name attached (so the caller's log/trace records which dependency stalled)."""
    try:
        return await asyncio.wait_for(awaitable, timeout=GATEWAY_TIMEOUT_S)
    except TimeoutError:
        raise TimeoutError(f"{gateway_name} gateway timed out after {GATEWAY_TIMEOUT_S}s") from None


async def run_cycle(
    cycle_id: str,
    *,
    fleet: FleetGateway | None = None,
    ledger: LedgerGateway | None = None,
    scada_gateway: ScadaGateway | None = None,
    schedule_gateway: ScheduleGateway | None = None,
    extras_gateway: CycleExtrasGateway | None = None,
    now: datetime | None = None,
    lease_ttl_s: float = DEFAULT_LEASE_TTL_S,
    only_bank_ids: Sequence[str] | None = None,
    retry_excluded_hub_ids: frozenset[str] = frozenset(),
    attempt: int = 0,
) -> list[Grant]:
    """One S1-S7 allocation cycle (02a S5.1-S5.2): builds this cycle's `grant` rows for every bank,
    honoring frozen commitments (K13), reserve/P/kVA/ramp limits (K1/K4), and the one-loop-per-quantity
    rule (K9, `DIST_DEFERRAL` PI loop is the sole integrating controller on bank kVA).

    This is the thin I/O adapter: it gathers the pure inputs via the injected gateways (production
    wiring supplies real ones backed by `opengrid.fleet`/`opengrid.ledger`/`opengrid.feeds`; tests
    inject fakes, per BUILD.md S5's "use fakes for the ledger and fleet"), calls the pure
    `opengrid.allocator.cycle.cycle`, persists the resulting grants, and returns them as `Grant` rows.

    `only_bank_ids` (the K4 veto re-proposal, one per cycle) re-solves just those banks and must not disturb
    anything else: the cycle's cached extras are reused (closed-loop controllers and PQ are not stepped
    twice) with `retry_excluded_hub_ids` added; the retried banks' PI/dwell states are restored to their
    pre-cycle values first (K9: one integration step per cycle); the other banks' last grants, hub
    allocations and shortfalls are kept (the shortfall report stays complete, so AT_RISK and escalation for
    other banks are unaffected); and no extras `observe` runs.
    """
    if fleet is None or ledger is None:
        raise NotImplementedError(
            "run_cycle requires FleetGateway/LedgerGateway wiring; opengrid.fleet/opengrid.ledger's "
            "public interface does not yet expose hub-level snapshots or grant persistence for "
            "MVP-S (see opengrid.allocator.gateways) -- inject fakes in tests, real gateways once "
            "engine wiring lands."
        )

    t = now or datetime.now(UTC)

    try:
        bank_ids = list(await _with_gateway_timeout(fleet.bank_ids(), gateway_name="fleet.bank_ids"))
        if only_bank_ids is not None:
            # A same-cycle re-proposal (K4 veto fail-safe) re-solves only the vetoed banks.
            wanted = set(only_bank_ids)
            bank_ids = [b for b in bank_ids if b in wanted]
        fleet_state = await _with_gateway_timeout(
            fleet.fleet_state(bank_ids, t), gateway_name="fleet.fleet_state"
        )
        if only_bank_ids is not None:
            fleet_state = FleetState(
                hubs=tuple(h for h in fleet_state.hubs if h.bank_id in wanted),
                banks=tuple(b for b in fleet_state.banks if b.bank_id in wanted),
            )
        ledger_view = await _with_gateway_timeout(
            ledger.ledger_view(bank_ids, t), gateway_name="ledger.ledger_view"
        )
        scada = (
            await _with_gateway_timeout(scada_gateway.samples(bank_ids), gateway_name="scada.samples")
            if scada_gateway is not None
            else {}
        )
        schedule = (
            await _with_gateway_timeout(schedule_gateway.schedule(bank_ids), gateway_name="schedule.schedule")
            if schedule_gateway is not None
            else Schedule()
        )
        instructions = (
            await _with_gateway_timeout(
                schedule_gateway.instructions(bank_ids), gateway_name="schedule.instructions"
            )
            if schedule_gateway is not None
            else ()
        )
        if only_bank_ids is not None:
            extras = dataclasses.replace(
                _last_extras[0] if _last_extras else CycleExtras(),
                excluded_hub_ids=(_last_extras[0].excluded_hub_ids if _last_extras else frozenset())
                | retry_excluded_hub_ids,
            )
        else:
            extras = (
                await _with_gateway_timeout(
                    extras_gateway.extras(fleet_state, ledger_view, t), gateway_name="extras.extras"
                )
                if extras_gateway is not None
                else CycleExtras()
            )
            _last_extras[:] = [extras]
    except TimeoutError as exc:
        # K7 "degrade, don't trip": hold the last cycle's signed-off grants rather than propose a
        # fresh batch built from a stalled/partial read of this cycle's inputs.
        logger.warning(
            "allocator gateway timeout, holding last grants",
            extra={"cycle_id": cycle_id, "error": str(exc), "reason_code": reasons.R_GATEWAY_TIMEOUT},
        )
        return list(_last_grants)

    if only_bank_ids is None:
        _pre_cycle_pi.clear()
        _pre_cycle_pi.update(_pi_states)
        _pre_cycle_dwell.clear()
        _pre_cycle_dwell.update(_dwell_states)
    else:
        for bank_id in wanted:
            _restore(_pi_states, _pre_cycle_pi, bank_id)
            _restore(_dwell_states, _pre_cycle_dwell, bank_id)
    result: CycleResult = cycle(
        t,
        fleet_state,
        ledger_view,
        schedule,
        scada,
        instructions,
        cycle_id=cycle_id,
        pi_states=_pi_states,
        dwell_states=_dwell_states,
        closed_loop_caps=extras.closed_loop_caps,
        pq=extras.pq,
        enforce_territory=extras.enforce_territory,
        flow_limits=extras.flow_limits,
        excluded_hub_ids=extras.excluded_hub_ids | extras.operator_hub_ids,
        operator_hub_ids=extras.operator_hub_ids,
        device_excluded_hub_ids=extras.device_excluded_hub_ids,
        lease_ttl_s=lease_ttl_s,
        prep_memo=_prep_memo,
    )
    if only_bank_ids is None and len(_prep_memo) > len(fleet_state.hubs):
        # Hubs left the fleet: keep only the current ones' entries.
        current = {h.hub_id for h in fleet_state.hubs}
        for hub_id in [k for k in _prep_memo if k not in current]:
            del _prep_memo[hub_id]
    if only_bank_ids is None:
        _last_hub_allocations.clear()
    else:
        for key in [k for k in _last_hub_allocations if k[1] in wanted]:
            _last_hub_allocations.pop(key)
    _last_hub_allocations.update(
        {(a.obligation_id, a.bank_id): dict(a.per_hub_kw) for a in result.hub_allocations}
    )
    if extras_gateway is not None and only_bank_ids is None:
        # Controller reconciliation, the PQ ladder and their traces; never costs the cycle (K7).
        try:
            await extras_gateway.observe(result, ledger_view, t)
        except Exception:
            logger.exception("cycle extras observe failed", extra={"cycle_id": cycle_id})

    # A re-proposal (`attempt` > 0) keeps the ORIGINAL cycle id on its grant rows (K13 accounting sees one
    # cycle); only the grant ids differ, so the rows never collide with the first attempt's.
    if attempt:
        await ledger.persist_grants(cycle_id, list(result.grants), attempt=attempt)  # type: ignore[call-arg]
    else:
        await ledger.persist_grants(cycle_id, list(result.grants))
    shortfalls = list(result.shortfalls)
    if only_bank_ids is None:
        _last_shortfalls[:] = shortfalls
    else:
        shortfalls = [s for s in _last_shortfalls if s.bank_id not in wanted] + shortfalls
        _last_shortfalls[:] = shortfalls
    try:
        await ledger.record_shortfalls(cycle_id, shortfalls)
    except Exception:
        logger.exception("failed to record shortfalls", extra={"cycle_id": cycle_id})
    if result.substitutions:
        # S5.3: every automatic hub swap is recorded; a recording failure never costs the cycle (K7).
        try:
            await ledger.record_substitution_events(cycle_id, list(result.substitutions))
        except Exception:
            logger.exception("failed to record hub substitutions", extra={"cycle_id": cycle_id})
    ledger_version = await ledger.ledger_version()
    grants = [_to_grant_row(cycle_id, ledger_version, g, attempt=attempt) for g in result.grants]
    if only_bank_ids is None:
        _last_grants[:] = grants
    else:
        _last_grants[:] = [g for g in _last_grants if str(g.bank_id) not in wanted] + grants
    return grants


def _restore[V](live: dict[str, V], snapshot: dict[str, V], bank_id: str) -> None:
    if bank_id in snapshot:
        live[bank_id] = snapshot[bank_id]
    else:
        live.pop(bank_id, None)


_ledger_gateway: LedgerGateway | None = None


def configure(ledger: LedgerGateway) -> None:
    """Wire the process's `LedgerGateway` for `substitute_hub` (called once by `opengrid.engine.main`;
    `run_cycle` takes its gateways per call)."""
    global _ledger_gateway
    _ledger_gateway = ledger


async def substitute_hub(obligation_id: str, from_hub_id: str, to_hub_id: str, reason_code: str) -> None:
    """Swap which hub realizes an obligation's unchanged `committed_kw` -- a `grant`-table change with
    reason `R-SUBSTITUTION`, always allowed, never a `commitment` write (02a S5.3, K13 exception list).
    Uses the gateway wired by `configure()` (it passed `ledger=None` before, so every call raised)."""
    await _substitute_hub(obligation_id, from_hub_id, to_hub_id, reason_code, ledger=_ledger_gateway)


async def _substitute_hub(
    obligation_id: str,
    from_hub_id: str,
    to_hub_id: str,
    reason_code: str,
    *,
    ledger: LedgerGateway | None,
) -> None:
    """Adapter body for `substitute_hub`, taking an injectable `ledger` so tests can pass a fake
    without monkeypatching the module-level public function's fixed signature.
    """
    if reason_code != reasons.R_SUBSTITUTION:
        raise ValueError(
            f"substitute_hub only accepts reason_code={reasons.R_SUBSTITUTION!r}, got {reason_code!r}"
        )
    if from_hub_id == to_hub_id:
        raise ValueError("substitute_hub requires from_hub_id != to_hub_id")
    if ledger is None:
        raise NotImplementedError(
            "substitute_hub requires LedgerGateway wiring; inject a fake in tests, a real gateway "
            "once engine wiring lands."
        )
    await ledger.record_substitution(obligation_id, from_hub_id, to_hub_id, reason_code)


def _obligation_uuid(obligation_id: str | None) -> UUID | None:
    """The obligation's real UUID. Only a non-UUID string id (test fakes) is mapped to a stable UUIDv5 --
    hashing a real id would detach the grant from its obligation (guardian/settle lookups)."""
    if not obligation_id:
        return None
    try:
        return UUID(obligation_id)
    except ValueError:
        return uuid5(NAMESPACE_URL, obligation_id)


def grant_key(cycle_id: str, grant: ProposedGrant, attempt: int = 0) -> str:
    """The stable key a grant id is derived from; a same-cycle re-proposal adds `:r<attempt>`."""
    key = f"{cycle_id}:{grant.bank_id}:{grant.obligation_id}:{grant.is_headroom}"
    return f"{key}:r{attempt}" if attempt else key


def _to_grant_row(cycle_id: str, ledger_version: int, grant: ProposedGrant, *, attempt: int = 0) -> Grant:
    """Convert one pure-logic `ProposedGrant` into a `Grant` row. `bank_id` stays the topology's text id
    (`bank-000`, `og.bank`/`og.grant.bank_id` are text since migration 0004) -- the engine looks the bank
    up in the fleet twin by it."""
    return Grant(
        grant_id=uuid5(NAMESPACE_URL, grant_key(cycle_id, grant, attempt)),
        cycle_id=cycle_id,
        obligation_id=_obligation_uuid(grant.obligation_id),
        bank_id=grant.bank_id,
        granted_kw=Decimal(str(round(grant.granted_kw, 3))),
        is_headroom=grant.is_headroom,
        ledger_version=ledger_version,
        reason_code=grant.reason_code or None,
    )
