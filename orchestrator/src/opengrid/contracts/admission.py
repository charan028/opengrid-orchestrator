"""Opportunity intake and admission (02a S2.1, ES04-S02/S03/S05). `admit()` is the fixed public
entry point every new call goes through: structural feasibility + eligibility first (no DB row
created on failure, per 02a S2.1's `[*] -> REJECTED` branch), then an `OFFERED` opportunity +
obligation pair created together (02a S1.4/S1.5's 1:1 "admitted as" edge).

Partial take (`min_qty`/`increment`/`block`) is enforced with `opengrid.core.products` -- the same
rounding the selector uses when building LP variables (02a S3.6) -- never re-derived here
(BUILD.md S1 dupcheck).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from opengrid.contracts.errors import AdmissionError
from opengrid.contracts.repository import ContractsRepo
from opengrid.core.models.engine import Contract, Obligation, Opportunity, ProductRule
from opengrid.core.products import ProductRule as RoundingRule
from opengrid.core.products import is_feasible, round_quantity
from opengrid.trace import TraceStore


def _admission_stream(contract_id: UUID) -> str:
    """One trace stream per contract (not a single global 'admission' stream): BUILD.md S2 requires
    many customers/contracts to admit concurrently, and a shared stream would serialize every
    admission in the fleet through one hash-chain sequence counter -- two contracts admitting at the
    same instant would race for the same `seq` and one would fail with a spurious UNIQUE-constraint
    error despite being completely unrelated calls."""
    return f"admission-{contract_id}"


def rounding_rule_for(rule: ProductRule | None) -> RoundingRule:
    """`None` (e.g. `HOME`, which has no market product per the seed data) means unconstrained --
    modeled as a zero min_qty/increment CONTINUOUS rule so `round_quantity` is a no-op cap.

    Public (not `_`-prefixed): `opengrid.contracts.intake`'s candidate generators
    (`ancillary.py`/`deferral.py`) build the same `opengrid.core.products.ProductRule` shape from a
    contract's product rule to size their candidates before ever calling `admit_priced` -- sharing
    this conversion rather than re-deriving it (BUILD.md S1 no-duplication)."""
    if rule is None:
        return RoundingRule(min_qty_kw=Decimal(0), increment_kw=Decimal(0), block=False)
    return RoundingRule(min_qty_kw=rule.min_qty_kw, increment_kw=rule.increment_kw, block=rule.block)


def _select_product_rule(rules: list[ProductRule]) -> ProductRule | None:
    """MVP-S contracts carry at most one saleable product rule (02a S1.3, 0002_seed_demo.sql); if
    several exist, admission uses the first -- callers needing a specific product should filter
    `product_rules_for()` themselves before calling `admit()` (the fixed signature has no
    `product_code` parameter)."""
    return rules[0] if rules else None


async def _reject(
    trace: TraceStore,
    contract_id: UUID,
    window_start: datetime,
    window_end: datetime,
    requested_kw: Decimal,
    reason_code: str,
) -> None:
    """Trace a rejection that never reached `OFFERED` (ES04-S05: every rejection is traced with a
    reason code and its inputs, even when no opportunity/obligation row was created)."""
    await trace.append(
        _admission_stream(contract_id),
        "ADMISSION",
        "ADMISSION",
        {
            "contract_id": str(contract_id),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "requested_kw": str(requested_kw),
        },
        [reason_code],
    )


async def admit(
    repo: ContractsRepo,
    trace: TraceStore,
    contract_id: UUID,
    window_start: datetime,
    window_end: datetime,
    requested_kw: Decimal,
) -> Opportunity:
    """Admission-time feasibility and eligibility check (02a S2.1). Creates an `OFFERED`
    opportunity (and its paired `OFFERED` obligation) on success; raises `AdmissionError` with a
    `reason_code` and traces the rejection on failure. Never touches any other obligation --
    admission only ever competes for uncommitted headroom later, at a selector gate (BUILD.md S2).
    """
    return await admit_priced(repo, trace, contract_id, window_start, window_end, requested_kw)


async def admit_priced(
    repo: ContractsRepo,
    trace: TraceStore,
    contract_id: UUID,
    window_start: datetime,
    window_end: datetime,
    requested_kw: Decimal,
    *,
    value_per_mwh: Decimal | None = None,
    scenario_basis: str = "P50",
) -> Opportunity:
    """Same admission-time feasibility/eligibility check as `admit()`, additionally recording the
    market value (`$/MWh`) and scenario basis a caller priced the opportunity at (02a S1.4's
    `opportunity.value_per_mwh`/`scenario_basis` columns). `admit()` is the fixed public signature
    (INTERFACES.md) and delegates here with `value_per_mwh=None`; `opengrid.contracts.intake` -- the
    only caller that has a live feed/forecast-derived price to record -- calls this directly instead
    (BUILD.md S1: sharing this one code path, not re-deriving admission feasibility, per no-duplication)."""
    if window_end <= window_start:
        await _reject(trace, contract_id, window_start, window_end, requested_kw, "R-ADMIT-INVALID-WINDOW")
        raise AdmissionError("R-ADMIT-INVALID-WINDOW")
    if requested_kw <= 0:
        await _reject(trace, contract_id, window_start, window_end, requested_kw, "R-ADMIT-INVALID-QUANTITY")
        raise AdmissionError("R-ADMIT-INVALID-QUANTITY")

    contract = await repo.get_contract(contract_id)
    if contract is None:
        await _reject(trace, contract_id, window_start, window_end, requested_kw, "R-ADMIT-UNKNOWN-CONTRACT")
        raise AdmissionError("R-ADMIT-UNKNOWN-CONTRACT")
    if contract.status != "ACTIVE":
        await _reject(trace, contract_id, window_start, window_end, requested_kw, "R-ADMIT-CONTRACT-INACTIVE")
        raise AdmissionError("R-ADMIT-CONTRACT-INACTIVE")

    rules = await repo.get_product_rules(contract_id)
    rule = _select_product_rule(rules)
    rounding_rule = rounding_rule_for(rule)

    if not is_feasible(requested_kw, rounding_rule, requested_kw):
        await _reject(
            trace, contract_id, window_start, window_end, requested_kw, "R-ADMIT-INFEASIBLE-PRODUCT-RULE"
        )
        raise AdmissionError("R-ADMIT-INFEASIBLE-PRODUCT-RULE")

    admitted_kw = round_quantity(requested_kw, rounding_rule, requested_kw)

    now = datetime.now(UTC)
    opportunity = Opportunity(
        opportunity_id=uuid4(),
        contract_id=contract_id,
        product_rule_id=rule.product_rule_id if rule else None,
        window_start=window_start,
        window_end=window_end,
        requested_kw=admitted_kw,
        value_per_mwh=value_per_mwh,
        scenario_basis=scenario_basis,
        state="OFFERED",
        admitted_at=now,
    )
    obligation = Obligation(
        obligation_id=uuid4(),
        opportunity_id=opportunity.opportunity_id,
        contract_id=contract_id,
        service_type=contract.service_type,
        tier=contract.tier,
        window_start=window_start,
        window_end=window_end,
        committed_qty_kw=admitted_kw,
        state="OFFERED",
    )
    await repo.create_opportunity_and_obligation(opportunity, obligation)
    await trace.append(
        _admission_stream(contract_id),
        "ADMISSION",
        "ADMISSION",
        {
            "opportunity_id": str(opportunity.opportunity_id),
            "obligation_id": str(obligation.obligation_id),
            "contract_id": str(contract_id),
            "requested_kw": str(requested_kw),
            "admitted_kw": str(admitted_kw),
            "value_per_mwh": str(value_per_mwh) if value_per_mwh is not None else None,
        },
        None,
    )
    return opportunity


async def get_contract(repo: ContractsRepo, contract_id: UUID) -> Contract:
    """Fetch a contract row, or raise `LookupError`."""
    contract = await repo.get_contract(contract_id)
    if contract is None:
        raise LookupError(f"no such contract: {contract_id}")
    return contract


async def product_rules_for(repo: ContractsRepo, contract_id: UUID) -> list[ProductRule]:
    """Return every product_rule row for `contract_id`."""
    return await repo.get_product_rules(contract_id)
