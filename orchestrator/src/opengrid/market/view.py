"""The profitability screen's $/kW data (08 S3.4 "shown on the profitability screen"; 09 S4, WP-2M-11).

`profitability_per_kw` turns per-contract (and per-headroom) period totals into the payload of
`GET /og/api/profitability/per-kw`: one row per contract, one per market, one fleet row, the hardware
view ($7,000 / 11 kW) and the 08 S3c illustrative reference, each with the 3-year target flag. Pure.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from opengrid.core.models.market import MARKETS, Market
from opengrid.market.config import HOME_UNIT_CAPEX_USD, HOME_UNIT_KW, TARGET_PAYBACK_YEARS
from opengrid.market.economics import (
    METHOD_VERSION,
    KwEconomics,
    PeriodTotals,
    illustrative_home_unit,
    period_kw_economics,
    rollup,
)


class PerKwSummary(BaseModel):
    """Response body of `GET /og/api/profitability/per-kw`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    method_version: str
    period_hours: Decimal | None
    hardware_view_usd_per_kw: Decimal
    target_payback_years: Decimal
    contracts: list[KwEconomics]
    markets: dict[Market, KwEconomics]
    fleet: KwEconomics
    illustrative_home_unit: KwEconomics


def profitability_per_kw(scopes: Sequence[PeriodTotals]) -> PerKwSummary:
    """Per-contract rows (and HEADROOM pseudo-scopes), per-market rollups and the fleet rollup. All
    `scopes` must cover the same period. A scope with `market=None` counts toward the fleet only."""
    per_market: dict[Market, KwEconomics] = {
        m: period_kw_economics(
            rollup([s for s in scopes if s.market == m], scope_kind="MARKET", scope_ref=m, market=m)
        )
        for m in MARKETS
    }
    fleet = period_kw_economics(rollup(list(scopes), scope_kind="FLEET", scope_ref="fleet", market=None))
    return PerKwSummary(
        method_version=METHOD_VERSION,
        period_hours=scopes[0].hours if scopes else None,
        hardware_view_usd_per_kw=HOME_UNIT_CAPEX_USD / HOME_UNIT_KW,
        target_payback_years=TARGET_PAYBACK_YEARS,
        contracts=[period_kw_economics(s) for s in scopes],
        markets=per_market,
        fleet=fleet,
        illustrative_home_unit=illustrative_home_unit(),
    )
