"""Two-market model row shapes (08-market-model-two-markets.md S3, 09-optimizer-dispatcher-update.md D1,
D2, D11): the market dimension of a contract, the regulated utilities (`og.utility`) and Base-owned
assets (`og.asset`: home banks and substation battery sets). Field names mirror
orchestrator/migrations/0025_market_model.sql exactly.

Vocabulary (one definition, imported everywhere else):

- `Market`: `REGULATED` (a vertically integrated utility is the customer; energy is territory-bound)
  or `FREE` (the ERCOT competitive market). Every contract belongs to exactly one.
- `UtilityId`: the regulated utilities OpenGrid can contract with. `None` on a FREE contract or an
  asset in the ERCOT competitive area.
- `AssetClass`: `HOME_BANK` (an aggregated bank of home hubs) or `SUBSTATION` (one Base-owned battery
  set sited at or next to a distribution substation; default 20 MW / 2 h).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict

Market = Literal["REGULATED", "FREE"]
UtilityId = Literal["AUSTIN_ENERGY", "CPS_ENERGY"]
#: Where an asset sits: inside a regulated utility's service territory, the ERCOT competitive area, or a
#: NOIE zone (a non-opt-in entity: a co-op or municipal utility outside retail choice that is not one of
#: our regulated customers). A NOIE asset serves neither market until the owner decides otherwise.
Territory = Literal["AUSTIN_ENERGY", "CPS_ENERGY", "ERCOT_COMPETITIVE", "NOIE"]
ERCOT_COMPETITIVE: Territory = "ERCOT_COMPETITIVE"
NOIE: Territory = "NOIE"
#: What `[zone_territory]` can assign a zone to: a regulated utility customer, or NOIE.
ZoneOwner = Literal["AUSTIN_ENERGY", "CPS_ENERGY", "NOIE"]
AssetClass = Literal["HOME_BANK", "SUBSTATION"]
CapacityPaymentBasis = Literal["USD_PER_KW_MONTH", "USD_PER_KW_YEAR"]
ChargingTariffKind = Literal["TOU_OFF_PEAK", "NIGHT_RATE"]

MARKETS: tuple[Market, ...] = ("REGULATED", "FREE")
UTILITY_IDS: tuple[UtilityId, ...] = ("AUSTIN_ENERGY", "CPS_ENERGY")

#: 08 S3a / D-22: at least 30% of regulated charging energy is solar. A floor, not a fixed ratio.
DEFAULT_SOLAR_SHARE_FLOOR = Decimal("0.30")


class _Row(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Utility(_Row):
    """One `og.utility` row: a regulated utility as a customer, with its capacity product and the
    terms under which Base charges inside its territory (09 S1.2 `c^{u,grid}`, `c^{u,sol}`)."""

    utility_id: UtilityId
    name: str
    territory_zones: list[str]
    capacity_product: str
    payment_basis: CapacityPaymentBasis
    capacity_price_usd_per_kw: Decimal | None = None
    charging_tariff_kind: ChargingTariffKind
    off_peak_rate_usd_per_kwh: Decimal
    mid_peak_rate_usd_per_kwh: Decimal | None = None
    on_peak_rate_usd_per_kwh: Decimal | None = None
    charging_adder_usd_per_kwh: Decimal = Decimal("0")
    solar_cost_usd_per_kwh: Decimal = Decimal("0.040")
    solar_share_floor: Decimal = DEFAULT_SOLAR_SHARE_FLOOR
    free_access_granted: bool = False
    tariff_ref: str
    source_note: str | None = None


class Asset(_Row):
    """One `og.asset` row (09 D11). A SUBSTATION asset carries its own rating, energy, round-trip
    efficiency and interconnection (POI) limits in both directions; a HOME_BANK asset mirrors an
    `og.bank` row. `utility_id` is the territory (None = ERCOT competitive area)."""

    asset_id: str
    asset_class: AssetClass
    bank_id: str | None = None
    feeder_id: str | None = None
    substation_id: str | None = None
    zone: str
    utility_id: UtilityId | None = None
    p_kw: Decimal
    e_kwh: Decimal
    eta_rt: Decimal
    floor_frac: Decimal = Decimal("0.20")
    poi_import_kva: Decimal | None = None
    poi_export_kva: Decimal | None = None
    capex_usd: Decimal | None = None
    status: Literal["PLANNED", "ACTIVE", "RETIRED"] = "PLANNED"
