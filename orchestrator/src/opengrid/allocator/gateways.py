"""Thin-adapter dependency seams for `opengrid.allocator.run_cycle`/`substitute_hub` (02a S5).

The pure logic in `opengrid.allocator.cycle` never touches I/O; these `Protocol`s are the only
boundary the adapter functions in `opengrid.allocator.__init__` cross to reach `opengrid.fleet` /
`opengrid.ledger`. Tests inject fakes here instead of a database (BUILD.md S5's "use fakes for the
ledger and fleet" instruction) -- production wiring (owned by `engine`, 02b S1.2) supplies real
gateways once `opengrid.fleet`/`opengrid.ledger` grow the hub-level and grant-persistence methods
their current fixed public interface (`opengrid.fleet.capability`, `opengrid.ledger.reserve/release/
ledger_version`) does not yet expose -- those two functions are bank-aggregate only for MVP-S
(`INTERFACES.md`); per-hub snapshots and grant persistence are engine-wiring concerns layered on top.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from opengrid.allocator.models import (
    CycleExtras,
    CycleResult,
    FleetState,
    Instruction,
    LedgerView,
    ProposedGrant,
    ScadaSample,
    Schedule,
    ShortfallReport,
    SubstitutionEvent,
)


class FleetGateway(Protocol):
    """Read-only access to this cycle's fleet snapshot (02b `fleet`)."""

    async def bank_ids(self) -> Sequence[str]:
        """Every bank with an active obligation or live event this cycle (02a S5.1's cadence rule)."""
        ...

    async def fleet_state(self, bank_ids: Sequence[str], interval_start: datetime) -> FleetState:
        """Per-hub and per-bank capability snapshot for `bank_ids` at `interval_start`."""
        ...


class LedgerGateway(Protocol):
    """Read-only access to this cycle's committed obligations (02a S1/S2), plus grant persistence."""

    async def ledger_view(self, bank_ids: Sequence[str], interval_start: datetime) -> LedgerView:
        """Active `COMMITTED`/`DELIVERING` calls for `bank_ids` at `interval_start`."""
        ...

    async def ledger_version(self) -> int:
        """The current monotonic ledger version, stamped onto every persisted grant (02a S1.9)."""
        ...

    async def persist_grants(self, cycle_id: str, grants: Sequence[ProposedGrant]) -> None:
        """S7: write this cycle's proposed grants (insert-only `og.grant` rows)."""
        ...

    async def record_substitution(
        self, obligation_id: str, from_hub_id: str, to_hub_id: str, reason_code: str
    ) -> None:
        """S5.3: persist a single manual/one-off hub swap for `obligation_id` as a `grant`-table
        change with `reason_code` (always `R-SUBSTITUTION`) -- never a `commitment` write.
        """
        ...

    async def record_shortfalls(self, cycle_id: str, shortfalls: Sequence[ShortfallReport]) -> None:
        """This cycle's shortfalls (possibly none), for the engine's sustained-exception escalation."""
        ...

    async def record_substitution_events(self, cycle_id: str, events: Sequence[SubstitutionEvent]) -> None:
        """S5.3: record the automatic hub substitutions one 2 s cycle made (trace, `R-SUBSTITUTION`)."""
        ...


class ScadaGateway(Protocol):
    """Read-only access to this cycle's SCADA samples, keyed by bank_id (02b device boundary)."""

    async def samples(self, bank_ids: Sequence[str]) -> dict[str, ScadaSample]: ...


class ScheduleGateway(Protocol):
    """Read-only access to this cycle's price schedule (`opengrid.feeds`) and any live L2
    instructions (`opengrid.platform.mqtt` device boundary).
    """

    async def schedule(self, bank_ids: Sequence[str]) -> Schedule: ...

    async def instructions(self, bank_ids: Sequence[str]) -> Sequence[Instruction]: ...


class CycleExtrasGateway(Protocol):
    """The optional per-cycle inputs beyond S1-S7's core (`models.CycleExtras`): closed-loop caps, the PQ
    context, K15 territory enforcement and flow limits -- and the hook that sees each cycle's result
    (controller reconciliation, PQ ladder, traces)."""

    async def extras(
        self, fleet_state: FleetState, ledger_view: LedgerView, now: datetime
    ) -> CycleExtras: ...

    async def observe(self, result: CycleResult, ledger_view: LedgerView, now: datetime) -> None: ...
