"""opengrid.contracts -- customers/contracts CRUD, opportunity intake and admission, the obligation
state machine, product-rule enforcement, re-nomination points, and the obligation/commitment
queries `selector`/`ledger`/`settle` read (02a S1-S2, BUILD.md S4 "contracts" ownership).

This package owns `og.contract`, `og.product_rule`, `og.opportunity`, `og.obligation` and
`og.renomination_point`: nothing else writes those tables. `og-engine`'s `main.py` calls
`configure()` once at startup with a real `PgContractsRepo` (`pg_repo.py`) and the process's shared
`opengrid.trace.TraceStore`; every function below then uses that pair. Tests call `configure()` with
an in-memory fake repo/backend instead (`opengrid.trace.store.TraceBackend` is designed for exactly
this, per its own docstring) -- no other sibling module needs to be real for these tests to run.

Fixed public interface (orchestrator/INTERFACES.md; do not change these four signatures without the
architect's approval): `admit`, `get_contract`, `product_rules_for`, `AdmissionError`. Everything
else here is additional surface this module owns per BUILD.md S4's "contracts" row.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from opengrid.contracts import admission as _admission
from opengrid.contracts import crud as _crud
from opengrid.contracts import lifecycle as _lifecycle
from opengrid.contracts import renomination as _renomination
from opengrid.contracts.errors import (
    AdmissionError,
    ConcurrentUpdateError,
    IllegalTransitionError,
    RenominationError,
)
from opengrid.contracts.repository import ContractsRepo
from opengrid.core.models.engine import (
    Contract,
    Obligation,
    ObligationState,
    Opportunity,
    ProductRule,
    RenominationPoint,
)
from opengrid.trace import TraceStore

__all__ = [
    "AdmissionError",
    "ConcurrentUpdateError",
    "IllegalTransitionError",
    "RenominationError",
    "active_obligations",
    "admit",
    "admit_priced",
    "configure",
    "create_contract",
    "create_product_rule",
    "due_renomination_points",
    "exercise_renomination_point",
    "expire_unselected",
    "get_contract",
    "list_contracts",
    "list_customer_ids",
    "product_rules_for",
    "reset_for_testing",
    "set_contract_status",
    "transition_obligation",
]

_repo: ContractsRepo | None = None
_trace: TraceStore | None = None


def configure(repo: ContractsRepo, trace: TraceStore) -> None:
    """Wire this module's storage. Called once by `opengrid.engine.main` at process startup, and by
    test fixtures with fakes. Idempotent -- a later call simply replaces the pair."""
    global _repo, _trace
    _repo = repo
    _trace = trace


def reset_for_testing() -> None:
    """Clear the configured repo/trace (test teardown hygiene only)."""
    global _repo, _trace
    _repo = None
    _trace = None


def _require_repo() -> ContractsRepo:
    if _repo is None:
        raise RuntimeError("opengrid.contracts.configure(repo, trace) must be called before use")
    return _repo


def _require_trace() -> TraceStore:
    if _trace is None:
        raise RuntimeError("opengrid.contracts.configure(repo, trace) must be called before use")
    return _trace


async def admit(
    contract_id: UUID, window_start: datetime, window_end: datetime, requested_kw: Decimal
) -> Opportunity:
    """Admission-time feasibility check (uses `opengrid.core.products.is_feasible` against the
    contract's product rules) -- creates an `OFFERED` opportunity row, or raises `AdmissionError`
    with a `reason_code` if structurally infeasible (02a S2.1's `[*] -> OFFERED` / `-> REJECTED`)."""
    return await _admission.admit(
        _require_repo(), _require_trace(), contract_id, window_start, window_end, requested_kw
    )


async def admit_priced(
    contract_id: UUID,
    window_start: datetime,
    window_end: datetime,
    requested_kw: Decimal,
    *,
    value_per_mwh: Decimal | None = None,
    scenario_basis: str = "P50",
) -> Opportunity:
    """`admit()` plus recording the feed/forecast-derived `value_per_mwh`/`scenario_basis` a caller
    priced the opportunity at. Used by `opengrid.contracts.intake` (02a S1-S3); additional surface
    over the fixed `admit()` signature, not a replacement for it."""
    return await _admission.admit_priced(
        _require_repo(),
        _require_trace(),
        contract_id,
        window_start,
        window_end,
        requested_kw,
        value_per_mwh=value_per_mwh,
        scenario_basis=scenario_basis,
    )


async def get_contract(contract_id: UUID) -> Contract:
    """Fetch a contract row, or raise `LookupError`."""
    return await _admission.get_contract(_require_repo(), contract_id)


async def product_rules_for(contract_id: UUID) -> list[ProductRule]:
    """Return every product_rule row for `contract_id`."""
    return await _admission.product_rules_for(_require_repo(), contract_id)


# -- customers / contracts CRUD (ES04-S01; no dedicated customer table in MVP-S -- `customer_id` is
# an opaque uuid on `contract`, per 02a S1.2) -----------------------------------------------------


async def list_contracts(
    *, customer_id: UUID | None = None, service_type: str | None = None, status: str | None = None
) -> list[Contract]:
    return await _crud.list_contracts(
        _require_repo(), customer_id=customer_id, service_type=service_type, status=status
    )


async def list_customer_ids() -> list[UUID]:
    """Every distinct `customer_id` with at least one contract."""
    return await _crud.list_customer_ids(_require_repo())


async def create_contract(contract: Contract) -> Contract:
    return await _crud.create_contract(_require_repo(), contract)


async def set_contract_status(contract_id: UUID, status: str) -> Contract:
    return await _crud.set_contract_status(_require_repo(), contract_id, status)


async def create_product_rule(rule: ProductRule) -> ProductRule:
    return await _crud.create_product_rule(_require_repo(), rule)


# -- obligation lifecycle (02a S2.1) ---------------------------------------------------------------


async def transition_obligation(
    obligation_id: UUID,
    to_state: ObligationState,
    *,
    reason_code: str | None,
    payload: dict[str, object] | None = None,
    at_risk: bool | None = None,
) -> Obligation:
    """Validate and persist an obligation state transition, traced (02a S2.1). The only place
    `og.obligation.state` is written -- `selector`/`ledger`/`settle` call this rather than updating
    the row themselves."""
    return await _lifecycle.transition_obligation(
        _require_repo(),
        _require_trace(),
        obligation_id,
        to_state,
        reason_code=reason_code,
        payload=payload,
        at_risk=at_risk,
    )


async def expire_unselected(*, now: datetime | None = None) -> list[UUID]:
    """Sweep `OFFERED` obligations whose window has started with no selection (02a S2.1's
    `OFFERED -> EXPIRED`)."""
    return await _lifecycle.expire_unselected(_require_repo(), _require_trace(), now=now)


async def active_obligations(
    interval_start: datetime,
    interval_end: datetime,
    *,
    service_type: str | None = None,
    states: tuple[ObligationState, ...] = ("COMMITTED", "DELIVERING"),
) -> list[Obligation]:
    """Obligations in `states` overlapping `[interval_start, interval_end)` -- what
    `selector.load_frozen_commitments` and the allocator's per-cycle scope both read."""
    return await _require_repo().active_obligations_by_interval(
        interval_start, interval_end, service_type=service_type, states=states
    )


# -- re-nomination points (02a S1.7, ES04-S04) ------------------------------------------------------


async def due_renomination_points(*, as_of: datetime | None = None) -> list[RenominationPoint]:
    return await _renomination.due_renomination_points(_require_repo(), as_of=as_of)


async def exercise_renomination_point(
    renomination_point_id: UUID, outcome: str, *, plan_id: UUID | None = None
) -> RenominationPoint:
    """Exercise one re-nomination gate for its contract's obligation only (K13: every other
    obligation's commitment rows are untouched). `outcome='RESELECTED'` re-opens the obligation's
    `DELIVERING` self-loop (`R-RENOM-GATE`); `CONFIRMED`/`SKIPPED` just record the gate was reached."""
    return await _renomination.exercise_renomination_point(
        _require_repo(), _require_trace(), renomination_point_id, outcome, plan_id=plan_id
    )
