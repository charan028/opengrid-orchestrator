# ERCOT solar regions to our zones (D-28)

**Date:** 2026-09-26. **Status:** proposed, pending lead sign-off. **Config:** `[feeds.ercot.solar_share_zones]` in `orchestrator/config/orchestrator.toml`.

## What the config keys are

The ratio in `opengrid.feeds.solar_share` is solar MW (NP4-745-CD, by solar region) over load MW (NP6-345-CD, by weather zone). So the keys are the 8 ERCOT **weather zones**, not `LZ_*` ids: a key like `LZ_AEN` would find no load series and always return `None`. Load zones reach the mapping through `forecast.service.DEFAULT_WEATHER_ZONES_BY_LOAD_ZONE` (`LZ_AEN -> southC`, and so on).

## Mapping

Region membership per county is **Sourced** from ERCOT's county table [1]. Which cities sit in which weather zone is **Sourced** from ERCOT's weather zone map [2, slide 9]. Where a zone straddles regions, the pick follows where the load is (**Assumed**).

| Weather zone | Solar region(s) | Why |
|---|---|---|
| `coast` | FarEast | Harris, Fort Bend, Brazoria, Galveston are all FarEast [1]. |
| `east` | FarEast | Tyler (Smith), Longview (Gregg), Lufkin (Angelina) are FarEast [1]. |
| `farWest` | FarWest | Midland, Ector (Odessa), Reeves, Pecos are FarWest [1]. |
| `north` | NorthWest, CenterWest | Lubbock is NorthWest; Wichita Falls (Wichita) is CenterWest [1]. Zone spans both [2]. |
| `northC` | CenterEast | Dallas, Tarrant, Collin, Denton, McLennan are CenterEast [1]. East suburbs (Kaufman, Rockwall, Hunt) are FarEast but small next to DFW, so left out (**Assumed**). |
| `southC` | CenterEast, SouthEast | Travis, Williamson, Hays are CenterEast; Bexar, Guadalupe are SouthEast [1]. Austin and San Antonio are both in this zone [2]. |
| `southern` | SouthEast | Nueces, Webb, Hidalgo, Cameron are SouthEast [1]. |
| `west` | CenterWest | Abilene (Taylor), San Angelo (Tom Green) are CenterWest [1]. |

All 6 regions are used at least once. CenterEast and SouthEast each feed 2 zones, so zonal shares do not add up to the system share.

**Resulting load zone view:** LZ_HOUSTON: FarEast. LZ_NORTH: NorthWest, CenterWest, CenterEast. LZ_SOUTH: SouthEast, CenterEast. LZ_WEST: CenterWest, FarWest. LZ_AEN, LZ_LCRA: CenterEast, SouthEast (Travis itself is CenterEast [1]). LZ_CPS: SouthEast, CenterEast (Bexar is SouthEast [1]). LZ_RAYBN: CenterEast (Collin, Grayson; Fannin is FarEast [1]). These inherit the "approximate" caveat on the `LZ_AEN`/`LZ_CPS`/`LZ_LCRA`/`LZ_RAYBN` weather zone picks.

## Caveat: this is a proxy, and what it means for D-28

- Solar regions count where the panels are, not who uses the power. ERCOT says the regions are only an aggregation for solar profiles and forecasts, and do not change load zones or pricing [2, slide 4] (**Sourced**).
- In 2021 about 85% of ERCOT solar MW sat in the Far West and West weather zones (6,249 of 7,360 MW) [2, slides 3 and 9] (**Sourced**). So `farWest`, `west` and `north` shares will often top 1.0 at midday and clamp to 1.0 in `core.solar_share`; `coast` will read low.
- NP4-745-CD reports grid-scale solar plants. Rooftop solar is behind the meter and shows up as lower load, not as solar (**Assumed**). Our hubs' own PV is caught by D-28 source 1 (telemetry), not by this feed.

**Effect on D-28:** source 1 (hub telemetry) stays the real measurement. For source 2, we suggest the system-wide share (`zone = "total"`) as the default for settlement (M1 grid-charged kWh, PnL), since power flows across zones. Use a zonal share only where the zone and its regions overlap well (`southC` for the Austin fleet, `southern`, `east`), and record it as a proxy. Either way the source used is logged per interval, as D-28 already requires.

## Sources

1. ERCOT, Wind and Solar Regions to County Mapping (2024-05-31): https://www.ercot.com/files/docs/2024/05/31/Wind%20and%20Solar%20Regions%20to%20County%20Mapping.xlsx
2. ERCOT CMWG, Discussion on Solar Regions (2021-06-14): https://www.ercot.com/files/docs/2021/06/11/Discussion_On_SolarRegions_CMWG_06142021_v5.pptx
3. NP4-745-CD product page: https://www.ercot.com/mp/data-products/data-product-details?id=NP4-745-CD
