"""opengrid.contracts.intake -- turns live market data + contract terms into `OFFERED` opportunities
(BUILD.md intake task brief; 02a S1-S3: opportunities, obligations, admission, selector gates, product
rules). Runs once per selector gate (`opengrid.engine`'s `GateScheduler`, every 15 minutes plus
admission/renomination events), for every `ACTIVE` contract, immediately *before*
`opengrid.selector.run_gate` -- so the selector always has this gate's candidates instead of relying on
demo seed data that is gone after the first delivery cycle (`qa/merge-notes.md`'s "zero
opportunity/obligation rows, selector has no candidates" finding).

This module only ever creates `OFFERED` opportunities via `opengrid.contracts.admit`/`admit_priced` --
it never selects, commits, reserves, or touches an existing obligation. K13 (the commitment lock) is
entirely the selector/ledger's concern; intake's own K13 discipline is narrower and structural: it
never proposes a window that overlaps one of *the same contract's* `COMMITTED`/`DELIVERING`
obligations, so it never tries to "re-offer" or "replace" capacity that is already spoken for --
new opportunities compete only for headroom the selector hasn't frozen yet.

Per-service generation (task brief):
- `ERCOT_ENERGY` -- `energy.py`'s spread math against the live `np6-905-cd` price and
  `opengrid.forecast`'s P50 scenarios (continuous product).
- `ERCOT_AS` -- `ancillary.py`'s capacity-hold math against the latest `np4-188-cd` MCPC for the
  contract's AS product, for the next operating day's hours (semi-continuous, 0.1 MW min/increment).
- `DIST_DEFERRAL` -- `deferral.py`'s calendar-derived peak-window block.
- `PARTNER_CAPACITY` -- event-driven only, never speculatively generated here: an event call arrives
  through `POST /og/api/opportunities` (or the control plane's partner-call scenario), which calls
  `opengrid.contracts.admit` directly (`opengrid.api.routers.contracts`) -- there is nothing for a
  15-minute gate to poll.
- `HOME` -- never an opportunity. The always-on reserve is an L1 envelope constraint
  (`opengrid.core.limits.check_reserve_floor`), not a sellable capacity; intake explicitly skips it.

Idempotency: `ContractsRepo.find_opportunity_by_window` is checked before every `admit`/`admit_priced`
call, so re-running intake for the same contract/interval across gates never creates a duplicate.
Obligations nobody selected still expire at their gate via `opengrid.contracts.expire_unselected`
(called once per `run_intake_gate`, after generation, per the task brief).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

if TYPE_CHECKING:
    # Type-only: `opengrid.forecast.models` has no side effects, but intake still avoids a *runtime*
    # import of the `opengrid.forecast` package itself (module docstring: dependency injected via
    # `configure(forecast_scenarios=...)`, not imported directly, mirroring `opengrid.forecast`'s own
    # `HistoryProvider` injection pattern).
    from opengrid.forecast.models import ScenarioPoint

from opengrid.contracts.admission import admit, admit_priced, rounding_rule_for
from opengrid.contracts.errors import AdmissionError
from opengrid.contracts.intake import ancillary, deferral, energy
from opengrid.contracts.intake.ports import MarketDataPort
from opengrid.contracts.lifecycle import expire_unselected
from opengrid.contracts.repository import ContractsRepo
from opengrid.core.models.engine import Contract, Opportunity, ProductRule
from opengrid.core.products import ProductRule as RoundingRule
from opengrid.core.products import round_quantity
from opengrid.trace import TraceStore

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_ENERGY_SERIES_KEY",
    "ForecastScenariosFn",
    "configure",
    "reset_for_testing",
    "run_intake_gate",
]

#: ERCOT settlement-point/load-zone the demo contracts' energy price is read against. A real
#: deployment would key this per contract (`Contract.territory_id`/`profile_ref`) -- MVP-S has one
#: ERCOT_ENERGY demo contract, so one configured series is enough; documented rather than hidden
#: (BUILD.md S5a).
#:
#: Must be a **load-zone** code (`[fleet].zones`, e.g. `LZ_HOUSTON`), never a hub code: `feeds.ercot`
#: queries `np6-905-cd` with `settlementPointType=LZ` (`forecast/README.md`'s canonical series-key
#: table), so `og.feed_obs.series`/`opengrid.forecast`'s `price_series` only ever carry `LZ_*` keys.
#: This was previously `"HB_HOUSTON"`, a hub code that never matches a real `feed_obs` row or a
#: `forecast.scenarios()` P50 point under that `settlementPointType`, which silently made every
#: `ERCOT_ENERGY`/`ERCOT_AS`-adjacent intake gate return `charge_price is None` and generate zero
#: opportunities despite live ERCOT prices flowing into `feed_obs` -- the exact class of bug
#: `forecast/service.py`'s own `DEFAULT_PRICE_SERIES` docstring already fixed once for that module.
DEFAULT_ENERGY_SERIES_KEY = "LZ_HOUSTON"


class ForecastScenariosFn(Protocol):
    async def __call__(self, horizon_start: datetime, horizon_end: datetime) -> list[ScenarioPoint]: ...


@dataclass(slots=True)
class _IntakeState:
    repo: ContractsRepo
    trace: TraceStore
    market: MarketDataPort
    forecast_scenarios: ForecastScenariosFn | None
    energy_series_key: str


_state: _IntakeState | None = None


def configure(
    repo: ContractsRepo,
    trace: TraceStore,
    market: MarketDataPort,
    *,
    forecast_scenarios: ForecastScenariosFn | None = None,
    energy_series_key: str = DEFAULT_ENERGY_SERIES_KEY,
) -> None:
    """Wire this module's dependencies once at process startup (`opengrid.engine.main`, alongside
    `opengrid.contracts.configure`). `forecast_scenarios` is injected (not a direct
    `opengrid.forecast` import) so intake stays testable with a fake and has no import-time coupling
    to a sibling module's own `configure()` state (mirrors `opengrid.forecast`'s own `HistoryProvider`
    injection pattern)."""
    global _state
    _state = _IntakeState(
        repo=repo,
        trace=trace,
        market=market,
        forecast_scenarios=forecast_scenarios,
        energy_series_key=energy_series_key,
    )


def reset_for_testing() -> None:
    global _state
    _state = None


def _require_state() -> _IntakeState:
    if _state is None:
        raise RuntimeError("opengrid.contracts.intake.configure(...) must be called before use")
    return _state


async def run_intake_gate(
    gate_kind: str, contract_scope: UUID | None = None, *, now: datetime | None = None
) -> list[Opportunity]:
    """Generate this gate's `OFFERED` opportunities for every in-scope `ACTIVE` contract, then sweep
    unselected `OFFERED` opportunities whose window has already started (`expire_unselected`).
    `contract_scope` narrows to one contract for an `ADMISSION`/`RENOMINATION` trigger; `None` (the
    `SCHEDULED_15MIN` trigger) covers every active contract, per BUILD.md S2's "several customers ...
    concurrently"."""
    state = _require_state()
    now = now or datetime.now(UTC)

    if contract_scope is not None:
        contract = await state.repo.get_contract(contract_scope)
        contracts = [contract] if contract is not None else []
    else:
        contracts = await state.repo.list_contracts(status="ACTIVE")

    created: list[Opportunity] = []
    for contract in contracts:
        if contract.status != "ACTIVE":
            continue
        try:
            created.extend(await _generate_for_contract(state, contract, now))
        except Exception:
            logger.exception(
                "intake failed for contract, degrading (no opportunities this gate)",
                extra={"contract_id": str(contract.contract_id), "gate_kind": gate_kind},
            )

    await expire_unselected(state.repo, state.trace, now=now)
    return created


async def _generate_for_contract(state: _IntakeState, contract: Contract, now: datetime) -> list[Opportunity]:
    if contract.service_type == "HOME":
        # K1 reserve is an L1 envelope constraint (opengrid.core.limits.check_reserve_floor), never a
        # sellable opportunity -- nothing to generate, nothing to trace as a rejection.
        return []
    if contract.service_type == "PARTNER_CAPACITY":
        # Event-driven only (module docstring): POST /og/api/opportunities or the control plane's
        # partner-call scenario admits these directly; a 15-minute gate has nothing to poll for.
        return []

    rules = await state.repo.get_product_rules(contract.contract_id)
    rule = rules[0] if rules else None

    if contract.service_type == "ERCOT_ENERGY":
        return await _intake_energy(state, contract, rule, now)
    if contract.service_type == "ERCOT_AS":
        return await _intake_as(state, contract, rule, now)
    if contract.service_type == "DIST_DEFERRAL":
        return await _intake_deferral(state, contract, rule, now)
    return []


async def _already_committed(
    state: _IntakeState, contract: Contract, window_start: datetime, window_end: datetime
) -> bool:
    """K13: never propose a window overlapping this contract's own committed capacity (module
    docstring) -- the selector enforces the lock fleet-wide; intake's own job is simply to not waste a
    gate offering something already spoken for."""
    active = await state.repo.active_obligations_by_interval(
        window_start, window_end, service_type=contract.service_type, states=("COMMITTED", "DELIVERING")
    )
    return any(o.contract_id == contract.contract_id for o in active)


async def _admit_candidate(
    state: _IntakeState,
    contract: Contract,
    *,
    window_start: datetime,
    window_end: datetime,
    requested_kw: Decimal,
    value_per_mwh: Decimal | None,
    rationale: dict[str, object],
) -> Opportunity | None:
    if (
        await state.repo.find_opportunity_by_window(contract.contract_id, window_start, window_end)
        is not None
    ):
        return None  # idempotent: this contract/interval was already offered at an earlier gate
    if await _already_committed(state, contract, window_start, window_end):
        return None  # K13: capacity here is already committed -- not intake's to re-offer

    try:
        if value_per_mwh is None:
            opportunity = await admit(
                state.repo, state.trace, contract.contract_id, window_start, window_end, requested_kw
            )
        else:
            opportunity = await admit_priced(
                state.repo,
                state.trace,
                contract.contract_id,
                window_start,
                window_end,
                requested_kw,
                value_per_mwh=value_per_mwh,
            )
    except AdmissionError:
        logger.info(
            "intake candidate rejected at admission",
            extra={"contract_id": str(contract.contract_id), "window_start": window_start.isoformat()},
        )
        return None

    await state.trace.append(
        f"intake-{contract.contract_id}",
        "ADMISSION",
        "INTAKE",
        {
            "contract_id": str(contract.contract_id),
            "service_type": contract.service_type,
            "opportunity_id": str(opportunity.opportunity_id),
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            **rationale,
        },
        None,
    )
    return opportunity


async def _intake_energy(
    state: _IntakeState, contract: Contract, rule: ProductRule | None, now: datetime
) -> list[Opportunity]:
    charge_price = await state.market.latest_energy_price_usd_per_mwh(state.energy_series_key)
    if charge_price is None or state.forecast_scenarios is None:
        return []

    horizon_start = now
    horizon_end = horizon_start + timedelta(minutes=15 * (energy.ENERGY_HORIZON_INTERVALS + 1))
    scenario_points = await state.forecast_scenarios(horizon_start, horizon_end)
    p50_by_interval = {
        point.interval_start: point.value
        for point in scenario_points
        if getattr(point, "scenario", None) == "P50"
        and getattr(point, "kind", None) == "price"
        and getattr(point, "series_key", None) == state.energy_series_key
    }

    candidates = energy.compute_energy_candidates(
        now=now,
        charge_price_usd_per_mwh=charge_price,
        discharge_p50_by_interval=p50_by_interval,
        degradation_usd_per_kwh=contract.degradation_cost,
    )
    rounding_rule = rounding_rule_for(rule)
    created: list[Opportunity] = []
    for candidate in candidates:
        requested_kw = _round_offer(energy.DEFAULT_ENERGY_OFFER_KW, rounding_rule)
        if requested_kw <= 0:
            continue
        opportunity = await _admit_candidate(
            state,
            contract,
            window_start=candidate.window_start,
            window_end=candidate.window_end,
            requested_kw=requested_kw,
            value_per_mwh=candidate.value_per_mwh,
            rationale={
                "charge_price_usd_per_mwh": charge_price,
                "discharge_price_usd_per_mwh": p50_by_interval.get(candidate.window_start),
                "spread_usd_per_mwh": str(candidate.value_per_mwh),
            },
        )
        if opportunity is not None:
            created.append(opportunity)
    return created


async def _intake_as(
    state: _IntakeState, contract: Contract, rule: ProductRule | None, now: datetime
) -> list[Opportunity]:
    if rule is None:
        return []
    mcpc = await state.market.latest_as_mcpc_usd_per_mwh(rule.product_code)
    if mcpc is None:
        return []
    rounding_rule = rounding_rule_for(rule)
    candidates = ancillary.compute_as_candidates(now=now, mcpc_usd_per_mwh=mcpc, rule=rounding_rule)
    created: list[Opportunity] = []
    for candidate in candidates:
        opportunity = await _admit_candidate(
            state,
            contract,
            window_start=candidate.window_start,
            window_end=candidate.window_end,
            requested_kw=candidate.requested_kw,
            value_per_mwh=candidate.value_per_mwh,
            rationale={"product_code": rule.product_code, "mcpc_usd_per_mwh": mcpc},
        )
        if opportunity is not None:
            created.append(opportunity)
    return created


async def _intake_deferral(
    state: _IntakeState, contract: Contract, rule: ProductRule | None, now: datetime
) -> list[Opportunity]:
    rounding_rule = rounding_rule_for(rule)
    candidates = deferral.compute_deferral_candidates(now=now, rule=rounding_rule)
    created: list[Opportunity] = []
    for candidate in candidates:
        opportunity = await _admit_candidate(
            state,
            contract,
            window_start=candidate.window_start,
            window_end=candidate.window_end,
            requested_kw=candidate.requested_kw,
            value_per_mwh=candidate.value_per_mwh,
            rationale={
                "basis": "delivery_calendar_peak_window",
                "capacity_payment_usd_per_mwh": str(candidate.value_per_mwh),
            },
        )
        if opportunity is not None:
            created.append(opportunity)
    return created


def _round_offer(offer_kw: Decimal, rounding_rule: RoundingRule) -> Decimal:
    return round_quantity(offer_kw, rounding_rule, offer_kw)
