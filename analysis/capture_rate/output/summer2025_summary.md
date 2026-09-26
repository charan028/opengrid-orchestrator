# Capture rate: summer 2025 (before RTC+B) vs summer 2026

2025: 62 operating days (2025-07-01 to 2025-08-31), 239 paired batteries [Sourced: ERCOT 13052]. 2026: 30 days (2026-06-29 to 2026-07-28), 305 batteries.
Price: real-time HB_HUBAVG (ERCOT 13061) [Sourced]. Round-trip efficiency 85% [Assumed]. Energy trading only.
Pairing (2025): 100% of active storage battery-days paired to a charging load (tier 1 unit number 88%, tier 2 single candidate 11%, tier 3 MW match 1%); 243 distinct active storage gens.
2025 energy size [Assumed]: 97% of battery-days use the same battery's 2026 SOC range; the rest assume 1.5 h at max HSL.

| Measure | Summer 2025 | Summer 2026 |
|---|---|---|
| Fleet capture rate [Modeled] | 32% | 41% |
| Median battery capture rate [Modeled] | 27% | 37% |
| Capture on top 10% price days [Modeled] | 40% | 53% |
| Capture on all other days [Modeled] | 29% | 35% |
| Median actual energy earnings, $/kW-yr annualized [Modeled] | 9 | 7 |
| Top-quartile battery, $/kW-yr [Modeled] | 16 | 11 |
| Median perfect-foresight value, $/kW-yr [Modeled] | 32 | 20 |
| Share of perfect-foresight value on top 10% days [Modeled] | 30% | 35% |
| Highest 15-min hub price in window, $/MWh [Sourced] | 1754 | 349 |

Sensitivity to energy size (every battery set to the same duration) [Assumed size, Modeled result]:

| Fleet capture rate | Summer 2025 | Summer 2026 |
|---|---|---|
| 1 h at max MW | 43% | 61% |
| Main case (2025: 2026 SOC or 1.5 h; 2026: actual SOC range) | 32% | 41% |
| 2 h at max MW | 29% | 38% |
| Median battery, 1 h / 2 h | 29% / 20% | 47% / 30% |

Caveats: hub price, not each battery's node; energy trading only, excludes ancillary service revenue;
2025 batteries are gen + load pairs matched by name, and their MWh is estimated, not reported.
