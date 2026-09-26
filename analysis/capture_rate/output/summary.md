# Capture rate of Texas grid batteries

Days analysed: 30 (2026-06-29 to 2026-07-28), batteries: 305.
Price: real-time HB_HUBAVG (ERCOT 13061). Round-trip efficiency 85% [Assumed]. Energy trading only.

| Measure | Value |
|---|---|
| Fleet capture rate, energy trading (actual / perfect foresight) [Modeled] | 41% |
| Median battery capture rate [Modeled] | 37% |
| Capture on the top 10% price days [Modeled] | 53% |
| Capture on all other days [Modeled] | 35% |
| Median actual energy earnings, annualised, $/kW-yr [Modeled] | 7 |
| Top-quartile battery, $/kW-yr [Modeled] | 11 |
| Share of perfect-foresight value earned on top 10% price days | 35% |

Caveats: excludes reserve (ancillary service) payments, which many batteries earn instead of trading;
uses the hub price, not each battery's own node; annualising a short window overstates or understates
seasonal effects. Compare with the Austin Energy toll of $102/kW-yr [Sourced] and the home-battery
perfect-foresight value of $54 to $89/kW-yr in 2025 and 2026 [Modeled, prices_years_2021_2026.csv].
