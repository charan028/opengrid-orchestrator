"""Market-model configuration: the regulated utilities' planning terms and the zone -> territory table.

- `DEFAULT_UTILITIES`: the planning values for Austin Energy and CPS Energy, sourced in
  `docs/orchestrator/07-delivery/integrations/regulated-utilities-austin-cps-2026-09.md`. They are the
  same values `dev/seed/market_model_seed.sql` writes to `og.utility`; a loaded `og.utility` row (or an
  executed contract) overrides them.
- `load_zone_territory`: reads `[zone_territory]` from `orchestrator/config/tdsp_tariffs.toml` (the
  file `opengrid.settle.tariffs` already owns and resolves; this module only reads the one table
  settle does not).

Only `load_zone_territory` does I/O (a local file read).
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path
from typing import Any

from opengrid.core.models.market import UTILITY_IDS, Utility, UtilityId, ZoneOwner

#: Planning solar charging cost (08 S3c: utility solar at 4.0 cents/kWh). Config, per utility.
DEFAULT_SOLAR_COST_USD_PER_KWH = Decimal("0.040")

#: Hardware view of one home unit (08 S1, S3b): $7,000 per 11 kW unit.
HOME_UNIT_CAPEX_USD = Decimal("7000")
HOME_UNIT_KW = Decimal("11")
HOME_UNIT_USABLE_KWH = Decimal("39.2")
#: Substation battery set planning capex (09 S3): $1,000/kW for 2 h, $1,500/kW for 4 h.
SUBSTATION_CAPEX_USD_PER_KW = {2: Decimal("1000"), 4: Decimal("1500")}
#: The target payback (08 S3c, D-23): about 3 years.
TARGET_PAYBACK_YEARS = Decimal("3")

AUSTIN_ENERGY = Utility(
    utility_id="AUSTIN_ENERGY",
    name="Austin Energy",
    territory_zones=["LZ_AEN"],
    capacity_product="UTILITY_TOLLING",
    payment_basis="USD_PER_KW_YEAR",
    capacity_price_usd_per_kw=Decimal("102"),
    charging_tariff_kind="TOU_OFF_PEAK",
    off_peak_rate_usd_per_kwh=Decimal("0.02677"),
    mid_peak_rate_usd_per_kwh=Decimal("0.04118"),
    on_peak_rate_usd_per_kwh=Decimal("0.08442"),
    charging_adder_usd_per_kwh=Decimal("0"),
    solar_cost_usd_per_kwh=DEFAULT_SOLAR_COST_USD_PER_KWH,
    solar_share_floor=Decimal("0.30"),
    free_access_granted=False,
    tariff_ref="AE-FY2026-RES-TOU-PILOT",
    source_note=(
        "AE FY2026 tariff (eff. 2025-11-01) residential TOU pilot power supply: off-peak 2.677, mid 4.118, "
        "on 8.442 cents/kWh. Capacity $102/kW-yr fixed: utility tolling, RCA 26-1526 terms (D-29). Adders 0 per 08 S3c."
    ),
)

CPS_ENERGY = Utility(
    utility_id="CPS_ENERGY",
    name="CPS Energy",
    territory_zones=["LZ_CPS"],
    capacity_product="DEMAND_RESPONSE",
    payment_basis="USD_PER_KW_YEAR",
    capacity_price_usd_per_kw=Decimal("45"),
    charging_tariff_kind="NIGHT_RATE",
    off_peak_rate_usd_per_kwh=Decimal("0.05026"),
    charging_adder_usd_per_kwh=Decimal("0"),
    solar_cost_usd_per_kwh=DEFAULT_SOLAR_COST_USD_PER_KWH,
    solar_share_floor=Decimal("0.30"),
    free_access_granted=False,
    tariff_ref="CPS-2024-PL-PLANNING",
    source_note=(
        "PLACEHOLDER pending the contract (09 OQ-6): CPS has no published TOU night rate; uses Schedule PL "
        "additional-kWh energy 3.610 + fuel base 1.416 cents/kWh. Capacity $45/kW per season (C&I DR)."
    ),
)

DEFAULT_UTILITIES: dict[UtilityId, Utility] = {u.utility_id: u for u in (AUSTIN_ENERGY, CPS_ENERGY)}


#: `[zone_territory]` market labels. FREE entries document a competitive zone's delivery-charge reason only.
ZONE_MARKETS = frozenset({"REGULATED", "FREE", "NOIE"})
NOIE_OWNER: ZoneOwner = "NOIE"


def _read_zone_territory(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)
    table = raw.get("zone_territory", {})
    return table if isinstance(table, dict) else {}


def parse_zone_territory(raw: dict[str, Any]) -> dict[str, UtilityId]:
    """`{zone: utility_id}` for every REGULATED entry of a parsed `[zone_territory]` table. An entry
    naming an unknown utility raises (never silently drop a regulated zone into the free market).
    NOIE and FREE entries are not in this map; `parse_zone_owners` includes NOIE."""
    out: dict[str, UtilityId] = {}
    for zone, entry in raw.items():
        if not isinstance(entry, dict) or entry.get("market", "REGULATED") != "REGULATED":
            continue
        utility = str(entry.get("utility", ""))
        match = next((u for u in UTILITY_IDS if u == utility), None)
        if match is None:
            raise ValueError(f"[zone_territory].{zone}: unknown utility {utility!r}")
        out[str(zone)] = match
    return out


def parse_zone_owners(raw: dict[str, Any]) -> dict[str, ZoneOwner]:
    """`{zone: owner}`: every REGULATED entry (its utility, as `parse_zone_territory`) plus every
    `market = "NOIE"` entry as `"NOIE"` (its `utility` field is informational, e.g. LCRA). FREE entries are
    omitted (competitive by default). An unknown `market` label raises: a typo must never silently turn a
    non-competitive zone into the free market."""
    out: dict[str, ZoneOwner] = dict(parse_zone_territory(raw))
    for zone, entry in raw.items():
        if not isinstance(entry, dict):
            continue
        market = str(entry.get("market", "REGULATED"))
        if market not in ZONE_MARKETS:
            raise ValueError(f"[zone_territory].{zone}: unknown market {market!r}")
        if market == "NOIE":
            out[str(zone)] = NOIE_OWNER
    return out


def load_zone_territory(path: Path) -> dict[str, UtilityId]:
    """The REGULATED zones of `[zone_territory]` in `tdsp_tariffs.toml` (resolve the path with
    `opengrid.settle.tariffs.resolve_tdsp_tariffs_path`). Raises `OSError` if the file is missing.
    Prefer `load_zone_owners`, which also marks NOIE zones (K15: a NOIE bank serves neither market)."""
    return parse_zone_territory(_read_zone_territory(path))


def load_zone_owners(path: Path) -> dict[str, ZoneOwner]:
    """REGULATED and NOIE zones of `[zone_territory]` (`parse_zone_owners`). Raises `OSError` if the
    file is missing. Pass the result to `territory_of_zone` / `MarketModel`."""
    return parse_zone_owners(_read_zone_territory(path))
