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

from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from opengrid.allocator import reasons
from opengrid.allocator.cycle import cycle
from opengrid.allocator.gateways import FleetGateway, LedgerGateway, ScadaGateway, ScheduleGateway
from opengrid.allocator.models import CycleResult, DwellState, PiState, ProposedGrant, Schedule
from opengrid.core.models.engine import Grant

__all__ = ["cycle", "run_cycle", "substitute_hub"]

# One DIST_DEFERRAL PI integrator and one dwell tracker per bank, kept alive for the life of the
# process (K9: exactly one integrating controller per bank kVA loop). Module-level because
# `og-engine` runs the allocator as a single long-lived process (02a S0 "Stack").
_pi_states: dict[str, PiState] = {}
_dwell_states: dict[str, DwellState] = {}


async def run_cycle(
    cycle_id: str,
    *,
    fleet: FleetGateway | None = None,
    ledger: LedgerGateway | None = None,
    scada_gateway: ScadaGateway | None = None,
    schedule_gateway: ScheduleGateway | None = None,
    now: datetime | None = None,
) -> list[Grant]:
    """One S1-S7 allocation cycle (02a S5.1-S5.2): builds this cycle's `grant` rows for every bank,
    honoring frozen commitments (K13), reserve/P/kVA/ramp limits (K1/K4), and the one-loop-per-quantity
    rule (K9, `DIST_DEFERRAL` PI loop is the sole integrating controller on bank kVA).

    This is the thin I/O adapter: it gathers the pure inputs via the injected gateways (production
    wiring supplies real ones backed by `opengrid.fleet`/`opengrid.ledger`/`opengrid.feeds`; tests
    inject fakes, per BUILD.md S5's "use fakes for the ledger and fleet"), calls the pure
    `opengrid.allocator.cycle.cycle`, persists the resulting grants, and returns them as `Grant` rows.
    """
    if fleet is None or ledger is None:
        raise NotImplementedError(
            "run_cycle requires FleetGateway/LedgerGateway wiring; opengrid.fleet/opengrid.ledger's "
            "public interface does not yet expose hub-level snapshots or grant persistence for "
            "MVP-S (see opengrid.allocator.gateways) -- inject fakes in tests, real gateways once "
            "engine wiring lands."
        )

    t = now or datetime.now(UTC)
    bank_ids = list(await fleet.bank_ids())

    fleet_state = await fleet.fleet_state(bank_ids, t)
    ledger_view = await ledger.ledger_view(bank_ids, t)
    scada = await scada_gateway.samples(bank_ids) if scada_gateway is not None else {}
    schedule = await schedule_gateway.schedule(bank_ids) if schedule_gateway is not None else Schedule()
    instructions = await schedule_gateway.instructions(bank_ids) if schedule_gateway is not None else ()

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
    )

    await ledger.persist_grants(cycle_id, list(result.grants))
    ledger_version = await ledger.ledger_version()
    return [_to_grant_row(cycle_id, ledger_version, g) for g in result.grants]


async def substitute_hub(obligation_id: str, from_hub_id: str, to_hub_id: str, reason_code: str) -> None:
    """Swap which hub realizes an obligation's unchanged `committed_kw` -- a `grant`-table change with
    reason `R-SUBSTITUTION`, always allowed, never a `commitment` write (02a S5.3, K13 exception list).
    """
    await _substitute_hub(obligation_id, from_hub_id, to_hub_id, reason_code, ledger=None)


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


def _to_grant_row(cycle_id: str, ledger_version: int, grant: ProposedGrant) -> Grant:
    """Convert one pure-logic `ProposedGrant` into a persisted `Grant` row. Bank/obligation ids are
    deterministically derived UUIDv5s over their string ids (MVP-S's pure core works in plain
    strings for numpy-friendliness; production bank/obligation ids are already UUIDs end to end, so
    this derivation only matters for the string ids fakes/tests use).
    """
    obligation_uuid = uuid5(NAMESPACE_URL, grant.obligation_id) if grant.obligation_id else None
    return Grant(
        grant_id=uuid5(
            NAMESPACE_URL, f"{cycle_id}:{grant.bank_id}:{grant.obligation_id}:{grant.is_headroom}"
        ),
        cycle_id=cycle_id,
        obligation_id=obligation_uuid,
        bank_id=uuid5(NAMESPACE_URL, grant.bank_id),
        granted_kw=Decimal(str(round(grant.granted_kw, 3))),
        is_headroom=grant.is_headroom,
        ledger_version=ledger_version,
    )
