# Grid battery fleet at 2026 evening price spikes

Sources: ERCOT 60-Day SCED Disclosure, ESR table (13052, NP3-965-ER) and 2026 real-time hub prices (13061) [Sourced]. Ratios and day selection [Modeled].

**Which days.** The Friday supply dossier examined 4 large 2026 evening spike days: 2026-03-23, 2026-04-24, 2026-04-27, 2026-07-22. They are not the 4 biggest 2026 spikes. Ranked by each day's highest 15-minute HB_HUBAVG price (2026-01-01 to 2026-09-19, `day_ranking_2026.csv`), the biggest day was 2026-01-28 ($1,281, HE8, a morning), and the top 5 evening days were 2026-04-24 ($1,085), 2026-03-23 ($936), 2026-01-25 ($915), 2026-04-27 ($784), 2026-08-26 ($781). Battery data (60-day SCED) only reaches 2026-07-28. This run covers every evening day (spike in HE17 to HE24) at or above $300 with battery data, or the days given with --days.

**Spike** = the first SCED run in the 15-minute interval with the day's highest HB_HUBAVG price. **Fleet SoC %** = total State of Charge / total Maximum SOC. **Fleet hours** = total Maximum SOC / the day's highest online HSL. **Reserve awards** = all upward ancillary service awards (Reg-Up, RRS, ECRS, Non-Spin).

| Day | Dossier | Rank (all days / evening) | Spike, $/MWh | SCED time | Fleet SoC at spike, MWh (% of max) | Discharge at spike, MW (% of online HSL) | Reserve awards at spike, MW | Max discharge (time, MW) | Fleet peak SoC (time, MWh, % full) | Hours from peak SoC to spike | Fleet hours |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-01-24 | no | 15 / 12 | 326 | 17:45 | 24,683 (90%) | 464 (3%) | 6,281 | 09:15, 960 | 01:10, 26,082, 93% | 16.6 | 1.84 |
| 2026-01-25 | no | 4 / 3 | 915 | 18:00 | 24,785 (89%) | 1,510 (10%) | 6,246 | 07:35, 1,885 | 04:50, 25,807, 94% | 13.2 | 1.84 |
| 2026-01-31 | no | 12 / 9 | 372 | 21:00 | 8,502 (32%) | 2,298 (18%) | 3,691 | 18:25, 6,097 | 16:30, 24,660, 92% | 4.5 | 1.80 |
| 2026-03-23 | yes | 3 / 2 | 936 | 21:30 | 4,965 (17%) | 3,473 (28%) | 2,595 | 20:00, 9,464 | 18:00, 27,268, 90% | 3.5 | 1.92 |
| 2026-04-11 | no | 16 / 13 | 307 | 23:00 | 5,961 (21%) | 3,222 (24%) | 2,572 | 20:45, 5,269 | 17:25, 26,333, 88% | 5.6 | 1.86 |
| 2026-04-24 | yes | 2 / 1 | 1,085 | 19:45 | 20,261 (65%) | 9,546 (58%) | 4,883 | 19:45, 9,546 | 18:10, 27,459, 87% | 1.6 | 1.90 |
| 2026-04-27 | yes | 5 / 4 | 784 | 21:15 | 10,281 (34%) | 6,786 (44%) | 3,541 | 20:15, 8,553 | 17:15, 27,205, 89% | 4.0 | 1.87 |
| 2026-07-22 | yes | 14 / 11 | 349 | 22:00 | 5,628 (16%) | 4,329 (30%) | 3,370 | 20:25, 12,016 | 18:45, 33,003, 91% | 3.2 | 1.91 |

## Headline checks

- The dossier's 4 days: fleet SoC at the spike was 17%, 65%, 34%, 16% of max SoC, so 3 of 4 were at 16% to 34%. The fleet peaked 87% to 91% full at 17:15 to 18:45; the spikes came 1.6 to 4.0 hours later, while 2.6 to 4.9 GW of reserve awards were held. Fleet energy: 1.87 to 1.92 hours at full online output [Sourced 13052, 13061; Modeled ratios].
- All 8 days in this run: 5 of 8 had fleet SoC below 35% at the spike (16% to 34%); the median was 33%. The exceptions: 2026-01-24 at 90% (spike 17:45, discharging 3% of online HSL, 6.3 GW of reserve awards); 2026-01-25 at 89% (spike 18:00, discharging 10% of online HSL, 6.2 GW of reserve awards); 2026-04-24 at 65% (spike 19:45, discharging 58% of online HSL, 4.9 GW of reserve awards).
- LZ_HOUSTON, 2026-01-01 to 2026-09-19: 150 15-minute intervals above $300 on 14 days; 102 (68%) in HE20 to HE23 [Sourced 13061].

Caveats: fleet SoC % sums every ESR in the SCED run, online or not; each 5-minute SCED run takes its 15-minute interval's price, so timing is good to about 20 seconds; hub price, not each battery's node.
