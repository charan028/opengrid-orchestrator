"""`MarketModel`: an immutable snapshot of territories, utilities, TDSP tariffs, banks and assets, and the
read API the selector (OPTIMIZER) and allocator (DISPATCH) call (09 S2.1 "Eligibility", S2.3).

Build it once per gate or cycle with `load_market_model(...)` (or the constructor, in tests), then
query it. Everything after construction is pure. Unknown banks, zones or utilities are never eligible
(K15 fail-safe).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from opengrid.core.models.market import ERCOT_COMPETITIVE, Asset, Territory, Utility, UtilityId
from opengrid.core.timeutil import to_market_tz
from opengrid.market.charging import ChargingCost, regulated_charging_cost
from opengrid.market.config import DEFAULT_UTILITIES, load_zone_territory
from opengrid.market.free_charging import free_charging_cost
from opengrid.market.territory import (
    MarketModelError,
    MarketRef,
    check_territory,
    territory_of_zone,
    utility_of_territory,
)
from opengrid.settle.tariffs import (
    TdspTariff,
    load_tdsp_tariffs,
    resolve_tariff,
    resolve_tdsp_tariffs_path,
    tdsp_for_zone,
)


class MarketModel:
    """Territory and charging-cost lookups over one snapshot of the fleet."""

    def __init__(
        self,
        *,
        zone_territory: Mapping[str, UtilityId],
        utilities: Mapping[UtilityId, Utility],
        banks: Iterable[tuple[str, str]],
        assets: Sequence[Asset] = (),
        tdsp_tariffs: Sequence[TdspTariff] = (),
        zone_default_tdsp: Mapping[str, str] | None = None,
    ) -> None:
        self._zone_territory = dict(zone_territory)
        self._utilities = dict(utilities)
        self._tdsp_tariffs = list(tdsp_tariffs)
        self._zone_default_tdsp = dict(zone_default_tdsp or {})
        self._bank_zone = dict(banks)
        self._bank_territory: dict[str, Territory | None] = {
            bank_id: territory_of_zone(zone, self._zone_territory)
            for bank_id, zone in self._bank_zone.items()
        }
        self._assets = {a.asset_id: a for a in assets}
        self._asset_territory: dict[str, Territory | None] = {a.asset_id: self._asset_terr(a) for a in assets}

    def _asset_terr(self, asset: Asset) -> Territory | None:
        """An asset's explicit `utility_id` wins; otherwise its zone decides. An explicit utility that
        contradicts a regulated zone is ambiguous and so unknown (fail closed)."""
        by_zone = territory_of_zone(asset.zone, self._zone_territory)
        if asset.utility_id is None:
            return by_zone
        if by_zone not in (asset.utility_id, ERCOT_COMPETITIVE):
            return None
        return asset.utility_id

    # --- territory ------------------------------------------------------------------------------

    def utility(self, utility_id: UtilityId) -> Utility:
        try:
            return self._utilities[utility_id]
        except KeyError as exc:
            raise MarketModelError(f"no terms loaded for utility {utility_id}") from exc

    def free_access(self, territory: Territory | None) -> bool:
        """Whether assets in `territory` may take FREE opportunities (K15 b; default no)."""
        if territory is None or territory == ERCOT_COMPETITIVE:
            return territory == ERCOT_COMPETITIVE
        utility_id = utility_of_territory(territory)
        utility = self._utilities.get(utility_id) if utility_id is not None else None
        return utility is not None and utility.free_access_granted

    def territory_of_bank(self, bank_id: str) -> Territory | None:
        """The bank's territory; `None` for an unknown bank or zone (never eligible)."""
        return self._bank_territory.get(bank_id)

    def territory_of_asset(self, asset_id: str) -> Territory | None:
        return self._asset_territory.get(asset_id)

    def territory_bank_ids(self, utility_id: UtilityId) -> frozenset[str]:
        """Banks inside `utility_id`'s service territory."""
        return frozenset(b for b, t in self._bank_territory.items() if t == utility_id)

    def territory_asset_ids(self, utility_id: UtilityId) -> frozenset[str]:
        """Assets (home banks and substation sets) inside `utility_id`'s service territory."""
        return frozenset(a for a, t in self._asset_territory.items() if t == utility_id)

    def bank_eligible(self, bank_id: str, ref: MarketRef) -> bool:
        territory = self.territory_of_bank(bank_id)
        return check_territory(ref, territory, free_access=self.free_access(territory)) is None

    def eligible_bank_ids(self, ref: MarketRef) -> frozenset[str]:
        """C25 a/b: the banks that may serve an obligation in market `ref` (FREE also covers headroom)."""
        return frozenset(b for b in self._bank_territory if self.bank_eligible(b, ref))

    def eligible_asset_ids(self, ref: MarketRef) -> frozenset[str]:
        return frozenset(
            a
            for a, t in self._asset_territory.items()
            if check_territory(ref, t, free_access=self.free_access(t)) is None
        )

    # --- charging cost --------------------------------------------------------------------------

    def charging_cost(
        self,
        zone: str,
        interval: datetime,
        *,
        wholesale_usd_per_kwh: Decimal | None = None,
        solar_share: Decimal | None = None,
    ) -> ChargingCost:
        """The cost of a kWh charged in `zone` during the interval starting at `interval`.

        Regulated zone: the utility's tariff for the period, blended with its solar floor, no M1.
        Competitive zone: `wholesale_usd_per_kwh` (required) plus the zone TDSP's M1 on grid kWh; the
        solar share defaults to 0 (the 30% floor is a regulated-contract term). Raises
        `MarketModelError` for an unknown zone or a missing wholesale price."""
        territory = territory_of_zone(zone, self._zone_territory)
        if territory is None:
            raise MarketModelError(f"unknown zone {zone!r}")
        utility_id = utility_of_territory(territory)
        if utility_id is not None:
            return regulated_charging_cost(self.utility(utility_id), zone, interval, solar_share=solar_share)
        if wholesale_usd_per_kwh is None:
            raise MarketModelError(f"a wholesale price is required to cost charging in {zone}")
        as_of: date = to_market_tz(interval).date()
        tariff = resolve_tariff(self._tdsp_tariffs, tdsp_for_zone(self._zone_default_tdsp, zone), as_of)
        return free_charging_cost(
            zone,
            wholesale_usd_per_kwh=wholesale_usd_per_kwh,
            tdsp_tariff=tariff,
            solar_share=solar_share if solar_share is not None else Decimal("0"),
        )


def load_market_model(
    *,
    banks: Iterable[tuple[str, str]],
    assets: Sequence[Asset] = (),
    utilities: Sequence[Utility] | None = None,
    config_path: str | Path | None = None,
) -> MarketModel:
    """Build a `MarketModel` from `tdsp_tariffs.toml` (`[zone_territory]`, the TDSP tariffs and the zone
    default TDSPs; path resolved like `opengrid.settle.tariffs`) plus the caller's `(bank_id, zone)` pairs,
    `og.asset` rows and `og.utility` rows (`None` = the planning defaults, `config.DEFAULT_UTILITIES`).
    Raises `OSError` when the tariff file is missing (no silent fallback)."""
    path = resolve_tdsp_tariffs_path(config_path)
    tariffs, zone_default_tdsp = load_tdsp_tariffs(path)
    utility_map: dict[UtilityId, Utility] = (
        dict(DEFAULT_UTILITIES) if utilities is None else {u.utility_id: u for u in utilities}
    )
    return MarketModel(
        zone_territory=load_zone_territory(path),
        utilities=utility_map,
        banks=banks,
        assets=assets,
        tdsp_tariffs=tariffs,
        zone_default_tdsp=zone_default_tdsp,
    )
