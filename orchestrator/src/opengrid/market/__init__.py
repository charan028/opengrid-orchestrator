"""opengrid.market -- the two-market model (08-market-model-two-markets.md, 09-optimizer-dispatcher-update.md
S1.5-S1.6, S4, S11): market membership, territory resolution (K15), charging cost, the regulated capacity
payment and the $/kW economics.

Read API for the selector and allocator (09 S11.3):

- `market_of(...)`, `market_of_contract(contract)` -> `MarketRef`
- `check_territory(ref, asset_territory, free_access=...)` -> reason code or None (the ONE K15 predicate)
- `load_market_model(banks=..., assets=...)` -> `MarketModel` with `territory_bank_ids(utility_id)`,
  `eligible_bank_ids(ref)`, `territory_of_bank(bank_id)`, `charging_cost(zone, interval, ...)`
- `regulated_capacity_payment(...)`
- `profitability_per_kw(scopes)` -> `PerKwSummary` (GET /og/api/profitability/per-kw)

`opengrid.market.pg_backend` (the only I/O module) is imported explicitly, not re-exported here.
"""

from opengrid.market.capacity import annual_capacity_price, regulated_capacity_payment
from opengrid.market.charging import ChargingCost, tou_period
from opengrid.market.economics import (
    KwEconomics,
    PeriodTotals,
    UnitEconomicsInputs,
    illustrative_home_unit,
    period_kw_economics,
    rollup,
    stored_energy_avg_cost,
    unit_economics,
)
from opengrid.market.model import MarketModel, load_market_model
from opengrid.market.territory import (
    FREE,
    R_TERRITORY_NO_FREE_ACCESS,
    R_TERRITORY_OUTSIDE,
    R_TERRITORY_UNKNOWN,
    MarketModelError,
    MarketRef,
    check_territory,
    market_of,
    market_of_contract,
    territory_of_zone,
)
from opengrid.market.view import PerKwSummary, profitability_per_kw

__all__ = [
    "FREE",
    "R_TERRITORY_NO_FREE_ACCESS",
    "R_TERRITORY_OUTSIDE",
    "R_TERRITORY_UNKNOWN",
    "ChargingCost",
    "KwEconomics",
    "MarketModel",
    "MarketModelError",
    "MarketRef",
    "PerKwSummary",
    "PeriodTotals",
    "UnitEconomicsInputs",
    "annual_capacity_price",
    "check_territory",
    "illustrative_home_unit",
    "load_market_model",
    "market_of",
    "market_of_contract",
    "period_kw_economics",
    "profitability_per_kw",
    "regulated_capacity_payment",
    "rollup",
    "stored_energy_avg_cost",
    "territory_of_zone",
    "tou_period",
    "unit_economics",
]
