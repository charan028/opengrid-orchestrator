# What the ERCOT data shows that most people miss (Track 1 evidence)

Built on Nicola's OpenGrid architecture; data analysis by Frank Brown with Claude Code.

## BLUF

Trading energy with a battery is a shrinking, spiky business. Real Texas grid batteries earned only 32% to 41% of what a perfect-hindsight energy trader could have made, and the perfect-hindsight trading value of a home battery fell 64% to 71% from 2023 to 2025. What is left sits in a few evening hours, and on most of the big 2026 evening spikes the grid battery fleet had already run low. So the money for a home-battery fleet is in capacity contracts, and the job of the orchestrator is to be charged and available in the few hours that matter.

Labels: **Sourced** = read directly from an ERCOT file or a public document. **Modeled** = our computation on sourced data. **Assumed** = an input we chose. **Second-hand** = a published figure we did not recompute.

## Findings

Every number below comes from a script in this folder and a committed output file, or from a cited public document. "Reproduce" is the command to run from `analysis/` (see "Reproduce it" for setup).

| # | Claim | Number | Label | Script | Output file | Reproduce |
|---|---|---|---|---|---|---|
| 1 | On most of the biggest 2026 evening price spikes, the grid battery fleet was already low when the price peaked | 5 of the 8 biggest 2026 evening spike days with battery data (Jan 1 to Jul 28) were at 16% to 34% of max state of charge. The dossier's 4 days: 17%, 65%, 34%, 16% | Sourced data, Modeled ratio | `spike_soc/run.py` | `spike_soc/output/spike_days.csv`, `summary.md` | `python spike_soc/run.py` |
| 2 | The exceptions are 2 January winter-event days and 04/24 | 01/24 and 01/25 at 89% to 90% full while holding 6.2 to 6.3 GW of reserve awards; 04/24 at 65% (spike came at max discharge) | Sourced data, Modeled ratio | same | same | same |
| 3 | The fleet fills up in the late afternoon and is spent by the late-evening spike | On the dossier's 4 days: peak 87% to 91% full at 17:15 to 18:45; spikes 1.6 to 4.0 hours later (3.2 to 4.0 hours on the 3 low days); 2.6 to 4.9 GW of reserve awards held at the spike; the fleet holds 1.87 to 1.92 hours of energy at full online output | Sourced data, Modeled ratio | same | same | same |
| 4 | Real-time spikes sit in the late evening | 68% of 2026 LZ_HOUSTON 15-min intervals above $300 (102 of 150, on 14 days, Jan 1 to Sep 19) fell in HE20 to HE23 | Sourced | same | `spike_soc/output/houston_over_300_by_hour_2026.csv` | same |
| 5 | Grid batteries capture a minority of the energy-trading value available, measured against a perfect-hindsight, energy-only trader at the hub price (excludes reserve revenue) | Fleet 32% in summer 2025 (62 days, 239 batteries), 41% in summer 2026 (30 days, 305 batteries); median battery 27% and 37% | Modeled | `capture_rate/run.py`, `pre_rtcb.py` | `capture_rate/output/summary.md`, `summer2025_summary.md` | `python capture_rate/run.py --days 30` then `python capture_rate/pre_rtcb.py` |
| 6 | Operators do better on spike days but still leave half | 53% on the top 10% price days vs 35% on other days (2026); 40% vs 29% (2025) | Modeled | same | same | same |
| 7 | Short batteries capture least | Under 1.2 hours of storage: 19% (2025) and 27% (2026); 1.8 hours or more: 36% and 45% | Modeled | `capture_rate/by_duration.py` | `capture_rate/output/capture_by_duration.csv` | `python capture_rate/by_duration.py` |
| 8 | What grid batteries actually earn from energy trading is small | Median $9/kW-yr (summer 2025) and $7 (summer 2026), annualized; top quartile $16 and $11 | Modeled | `capture_rate/*.py` | both summaries | as row 5 |
| 9 | A utility capacity contract pays far more | Austin Energy tolling agreement with Base Power: up to $4.08M a year for up to 40 MW, up to 10 years, so up to $102/kW-yr | Sourced | none | none | City of Austin RCA 26-1526, Item 8, Apr 23, 2026 (see sources) |
| 10 | The whole Texas battery market earned about $29/kW in 2025 (energy plus reserves, fleet average) | $29.4/kW for 2025 (Modo Energy Nowcast; $26.0 through November); $56 in 2024, $193 in 2023 | Second-hand | none | none | Modo Energy, Feb 3, 2026 (see sources). Not recomputed here |
| 11 | The perfect-hindsight trading value of a home battery fell sharply from 2023 to 2025 | LZ_HOUSTON $2,282 per home (2023), $790 (2024), $653 (2025). All 4 big zones: $1,927 to $2,818 in 2023, $653 to $979 in 2025 ($59 to $89/kW-yr); down 64% to 71%. 2023 was an unusually spiky year | Modeled | `price_history/arb.py`, `years.py` | `price_history/results/prices_years_2021_2026.csv` | `python price_history/fetch.py`, `arb.py`, `years.py` |
| 12 | The summer evening premium collapsed after 2023 | Evening (HE19 to HE22) minus solar hours (HE10 to HE15): $156 to $226/MWh in 2023, $23 to $41 in 2026 | Sourced prices, Modeled average | same | same | same |
| 13 | A few hours carry a large share of the trading value | The top 1% of 15-min intervals (about 88 hours a year) carry 27% to 54% of a home battery's value in 2024 to 2026 by zone; 35% to 44% since RTC+B (Dec 5, 2025 to Sep 19, 2026, about 9.5 months). The late-January 2026 winter event (Jan 24 to 31) supplies 29% to 39% of that top-1% value (11% to 16% of all value) | Modeled | `price_history/h5.py`, `h5_jan.py` | `price_history/results/prices_h5_top1pct_value_by_zone.csv`, `prices_h5_post_rtcb_jan2026_share.csv` | `python price_history/h5.py`, then `h5_jan.py` |
| 14 | Missing only those hours is expensive | Unavailable in exactly those intervals, even after re-optimizing the rest of the day: 18% to 46% of value lost in 2024 to 2026; 25% to 33% since RTC+B | Modeled | same | same | same |
| 15 | Most days are nearly worthless for trading | Median day $0.99 to $1.54 per home since RTC+B | Modeled | same | same | same |
| 16 | After RTC+B, day-ahead and real-time converge in normal hours but diverge in stress hours | LZ_HOUSTON median hourly spread moved from -$2.06 to -$0.98/MWh while its standard deviation rose from $35 to $53 | Sourced prices, Modeled statistics | `price_history/h7.py` | `price_history/results/prices_h7_da_rt_spread_by_zone.csv` | `python price_history/h7.py` |
| 17 | One winter event holds a big slice of post-RTC+B day-ahead value | Jan 25 to 26, 2026: day-ahead up to $1,694 (Houston) and $2,280 (CPS) while hourly real-time stayed at or below $379 (Houston); the 2 days hold 17% to 23% of post-RTC+B day-ahead arbitrage value | Sourced prices, Modeled value | same | `price_history/results/prices_h7_jan2026_event.csv` | same |
| 18 | Non-Spin is now the best-paid reserve product for a battery, and it pays in the evening | Day-ahead Non-Spin $35.9/kW-yr after RTC+B (up from $25.4 in 2025 before it; $29.7 without Jan 24 to 27, 2026) vs $15.0 to $18.8 for RRS, ECRS and Reg-Up; 57% of Non-Spin value falls in HE19 to HE24, 6% in HE14 to HE18 | Sourced prices, Modeled value | `reserves/nonspin.py` | `reserves/output/as_value_by_regime.csv`, `nspin_by_hour_post.csv`, `summary.md` | `python reserves/nonspin.py` |

### How the day set in finding 1 was chosen

The Friday supply dossier examined 4 large 2026 evening spike days (03/23, 04/24, 04/27, 07/22). They are **not** the 4 biggest 2026 spikes. Ranked by each day's highest 15-min HB_HUBAVG price (Jan 1 to Sep 19, 2026, `spike_soc/output/day_ranking_2026.csv`): the biggest day was 01/28 ($1,281, a morning spike at HE8); the top evening days were 04/24 ($1,085), 03/23 ($936), 01/25 ($915), 04/27 ($784) and 08/26 ($781); 07/22 ($349) ranks 14th overall. ERCOT publishes battery data 60 days late, so days after Jul 28 cannot be checked yet. `spike_soc/run.py` therefore uses a stated rule: every 2026 day through Jul 28 whose highest 15-min hub price was at least $300 and fell in HE17 to HE24. That gives 8 days, which are also the 8 biggest evening spike days with battery data.

## Check of the locked problem statement and Why

| Locked text | Status | Evidence | Recommended wording |
|---|---|---|---|
| "on 3 of the 4 biggest 2026 spikes, grid batteries were 16-34% charged when prices peaked" | Numbers reproduced exactly; "4 biggest" is wrong | Findings 1 to 3 | "On 5 of the 8 biggest 2026 evening price spikes with public battery data, Texas grid batteries were only 16% to 34% charged when prices peaked." Or keep the dossier set: "On 3 of 4 large 2026 evening spikes we examined, ..." |
| "captured only a third of the value available" | Reproduced (32% and 41%); "a third" fits 2025 only | Findings 5 and 7 | "captured only 32% to 41% of the energy-trading value a perfect-hindsight trader could have made (summers 2025 and 2026)" |
| "Austin Energy pays up to $102/kW-yr" | Cited | Finding 9, RCA 26-1526 | Keep "up to" |
| "about 3.5 times what the whole market paid batteries in 2025" | Original source found; ratio is arithmetic, not like-for-like | Finding 10 | "Texas grid batteries averaged about $29/kW from the whole market in 2025 (Modo Energy estimate)". If the ratio stays, say "about 3.5 times the 2025 market average (Modo Energy)" and note that one is a contract ceiling for callable capacity and the other is merchant revenue |
| Houston home battery $2,282 (2023) to $653 (2025) | Reproduced | Finding 11 | Add "2023 was an unusually spiky year; 2024 was $790" |
| "top 1% of intervals carry 25-33% of annual value since Dec 2025" | Wrong column | Findings 13 and 14 | "Since RTC+B, the top 1% of 15-minute intervals carry 35% to 44% of a home battery's trading value; missing them costs 25% to 33%, even after re-planning the rest of the day." Add "about 9.5 months of data, and about a third of that top-1% value came from the late-January 2026 winter event." |

## How each number was derived

**Grid battery fleet at spikes (findings 1 to 4).** ERCOT's 60-Day SCED Disclosure (report 13052) has an ESR table: every grid battery at every 5-minute SCED run, with output, State of Charge, Maximum SOC, HSL, status and reserve awards.
- Each SCED run gets the real-time HB_HUBAVG price (13061) of the 15-minute interval its timestamp falls in. The spike is the first SCED run in the day's highest-price interval.
- Fleet SoC % = sum of State of Charge / sum of Maximum SOC, across all batteries in that run.
- Discharge % = sum of positive output / sum of HSL over batteries with status ON or ONTEST.
- Fleet hours = total Maximum SOC / the day's highest total online HSL.
- Reserve awards = all upward awards (Reg-Up, RRS, ECRS, Non-Spin).
- This is the dossier's `h6.py` method, rewritten; see "Verification".

**Capture rate (findings 5 to 8).**
- Actual earnings = telemetered net output (MW, discharge positive) x time to the next SCED run x the real-time hub price (HB_HUBAVG, 13061). Charging counts as a cost.
- Perfect foresight = a small linear program that finds the best possible energy trading for a battery of the same MW and MWh on the same day's prices, known in advance. State of charge ends the day where it started.
- Capture rate = actual / perfect foresight. Fleet = total dollars over total dollars. Median = the median of each battery's own ratio.
- $/kW-yr = each battery's actual dollars / its MW / days in the window x 365.
- Before RTC+B there is no battery table. `pre_rtcb.py` pairs each battery's generator record (type "PWRSTR") with its charging load record by owner code (QSE) and name, for example ANCHOR_BESS2 with ANCHOR_LD2. 2025 files have no state of charge, so each battery's hours of storage come from its own 2026 data (97% of battery-days) or 1.5 hours [Assumed]. A 1-hour and 2-hour sensitivity is in the 2025 summary.
- `by_duration.py` splits both summers by each battery-day's hours of storage (MWh / MW).

**Price history (findings 11 to 17).** ERCOT's yearly price files, real-time 15-min (13061) and day-ahead hourly (13060), for the 8 load zones.
- `arb.py` solves a perfect-foresight linear program for 1 Base Power home battery, for every zone and day: 39.2 kWh, 11 kW, a 20% backup floor that is never crossed, 88% round-trip efficiency, at most 1 full cycle a day [Assumed].
- `years.py` sums that by year. Divide $ per home by 11 kW for $/kW-yr.
- `h5.py` ranks intervals by price, measures the share of value in the top 1%, then re-solves each day with the battery blocked in exactly those intervals.
- `h7.py` compares the hourly mean of the 4 real-time prices with the day-ahead price, before and after RTC+B, over the same calendar days (Dec 5 to Sep 19) on each side.

**Reserves (finding 18).** `nonspin.py` reads ERCOT 13091 (hourly day-ahead clearing prices for each reserve product). $/kW-yr = mean price ($/MW-h) x 8,760 hours / 1,000, the payment for 1 kW sold in every hour. Regimes: 2024; Jan 1 to Dec 4, 2025; Dec 5, 2025 to Sep 19, 2026.

## Reproduce it

No ERCOT login or API key. Python 3.10 or newer.

```
cd analysis
python -m venv .venv
.venv\Scripts\activate            (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

# Grid battery fleet at spikes: 8 SCED zips (about 55 MB each) + 1 price file, about 5 minutes
python spike_soc/run.py                     # all 8 evening spike days
python spike_soc/run.py --dossier-days      # only the dossier's 4 days
python spike_soc/run.py --raw-dir <folder>  # use already-downloaded 13052 zips and the 13061 2026 file

# Reserves: 3 small files, under 1 minute
python reserves/nonspin.py

# Price history: about 15 MB of downloads per year, about 10 to 20 minutes in total
python price_history/fetch.py
python price_history/arb.py
python price_history/years.py
python price_history/h5.py
python price_history/h7.py
python price_history/h5_jan.py

# Capture rate: about 55 MB download per operating day; 30 to 60 minutes for 30 days
python capture_rate/run.py --days 30
python capture_rate/pre_rtcb.py --start 2025-07-01 --end 2025-08-31   # run after run.py
python capture_rate/by_duration.py
```

Notes:
- `capture_rate/run.py --days 30` uses the newest 30 operating days on ERCOT's site, so the window moves forward 1 day each day. The committed result covers Jun 29 to Jul 28, 2026 (the newest 30 on Sep 26, 2026). To rerun that exact window, pass those days' ESR files with `--files`.
- `spike_soc/run.py` fixes its day window with `--through 2026-07-28` and its price window with `--price-through 2026-09-19`, so a later run gives the same answer. Move them forward to add newer days.
- Downloads go to each folder's `downloads/` (or `price_history/data/`), which are git-ignored.
- `capture_rate/output/summer2025_capture_by_battery_day.csv.gz` is gzipped to keep the repo small. pandas reads it directly.

## Verification (Sep 26, 2026)

| What | How | Result |
|---|---|---|
| `spike_soc` vs the supply dossier | `run.py --raw-dir data_safari/raw/supply --dossier-days` on the Friday raw files (4 SCED zips + 13061 2026 zip) | All 13 dossier metrics and all 6 timestamps match `supply_h6_esr_evening_summary.csv` exactly (SoC 17%, 65%, 34%, 16%); the 5-min and hourly tables match to rounding |
| `spike_soc` fresh download vs local files | Full run from ERCOT MIS, compared with the `--raw-dir` run | Identical rows for the 4 dossier days |
| Houston intervals above $300 | Same run | 150 intervals, 68% in HE20 to HE23, matches `supply_rt_spike_hour_distribution_2026.csv` |
| `reserves/nonspin.py` vs the reserves dossier | `--raw-dir data_safari/raw/reserves` and a fresh download | Identical to each other and to the dossier ($35.9, $25.4, 57%, 6%, 18%, $29.7) |
| Capture rate, summer 2026 | `run.py --files` on the 30 local ESR files, in a clean copy | Same 8,396 rows and same `summary.md` as committed (41%, 37%) |
| Capture rate, summer 2025 | `pre_rtcb.py` on the 62 local gen and load files, in a clean copy | Same per-battery-day rows as the committed `.csv.gz` and the same `summer2025_summary.md` (32%, 27%, sensitivity table) |
| Price history | Clean rerun of `fetch.py`, `arb.py`, `years.py`, `h5.py`, `h7.py` from fresh ERCOT downloads, in a clean copy | All 6 committed `price_history/results/*.csv` identical (Houston $2,282 / $790 / $653; top-1% 35% to 44% and lost 25% to 33% since RTC+B) |

## Data sources and provenance

All ERCOT data is real, public market data from ERCOT's Market Information System (MIS), downloadable with no login. Nothing here is synthetic. Each report is listed at `https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=<id>` and each file downloads from `https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=<DocID>`. This folder commits only our scripts and small derived tables, never the raw ERCOT files.

| ERCOT report id | Name | Used for | Date range used | Real or derived in repo |
|---|---|---|---|---|
| 13052 (NP3-965-ER) | 60-Day SCED Disclosure Reports (tables `60d_ESR_Data_in_SCED`, and before Dec 5, 2025 `60d_SCED_Gen_Resource_Data` and `60d_Load_Resource_Data_in_SCED`) | Battery output, state of charge, size, reserve awards | 2025-07-01 to 2025-08-31; 2026-06-29 to 2026-07-28; 2026-01-24, 01-25, 01-31, 03-23, 04-11, 04-24, 04-27, 07-22 | Raw is real ERCOT data, downloaded by the scripts; the repo holds derived per-battery-day and fleet tables |
| 13061 | Historical RTM Load Zone and Hub Prices (15-min) | Real-time prices, hub and load zones | 2021-01-01 to 2026-09-19 | Raw downloaded; derived tables committed |
| 13060 | Historical DAM Load Zone and Hub Prices (hourly) | Day-ahead prices | 2021-01-01 to 2026-09-19 (results from 2024) | Raw downloaded; derived tables committed |
| 13091 | Historical DAM Clearing Prices for Capacity (hourly MCPC by reserve product) | Reserve prices (finding 18) | 2024-01-01 to 2026-09-19 | Raw downloaded; derived tables committed |

External sources:

| Source | Used for | URL |
|---|---|---|
| City of Austin, Recommendation for Action, File 26-1526, Agenda Item 8, council meeting Apr 23, 2026 | Austin Energy battery tolling agreement with Base Power: up to 40 MW, up to $4,080,000 a year, up to 10 years (finding 9) | https://services.austintexas.gov/edims/document.cfm?id=471637 and https://www.austintexas.gov/council/2026/20260423-reg |
| American Public Power Association, "Austin Energy Enters Agreement with Base Power to Deploy 40 MW of Residential Battery Storage" | Second confirmation of the same agreement | https://www.publicpower.org/periodical/article/austin-energy-enters-agreement-with-base-power-deploy-40-mw-residential-battery-storage |
| Modo Energy, "Why were ERCOT battery revenues so low in 2025?", Feb 3, 2026 | 2025 ERCOT battery revenue: $26.0/kW through November, Nowcast $29.4/kW for the year; $56 in 2024, $193 in 2023 (finding 10) | https://modoenergy.com/research/en/why-were-ercot-battery-revenues-so-low-in-2025-weather-energy-arbitrage-builodout |

Terms: ERCOT posts these reports publicly; check ERCOT's website terms of use before redistributing raw files. No personal data is used or committed: no ESI IDs, addresses or contact data. Battery names and owner codes in the capture-rate CSVs are the public resource names and QSE codes that ERCOT itself discloses; the spike tables hold fleet totals only.

## Assumptions

- **Home battery [Assumed]:** 39.2 kWh usable and 11 kW per Base Power unit, 20% backup floor (7.84 kWh) never crossed, 88% round-trip efficiency, at most 1 floor-to-full cycle per day, state of charge ends each day where it started.
- **Grid battery [Assumed]:** 85% round-trip efficiency. Size from ERCOT's own data (max HSL for MW, Max SOC minus Min SOC for MWh).
- **Grid battery price [Assumed]:** the ERCOT hub average (HB_HUBAVG) stands in for each battery's own node price.
- **2025 battery size [Assumed]:** the same battery's 2026 hours of storage, or 1.5 hours when no 2026 match exists.
- **Perfect foresight** is an upper bound. No real operator knows tomorrow's prices.

## Caveats

- **Energy trading only.** Capture rate and home-battery value exclude reserve revenue. Many grid batteries earn reserves instead of trading, which makes them look like low-capture traders. The capture CSV has `as_awarded_mwh` to filter them. Batteries that hold reserves under 4 hours a day on average still capture only 42% in summer 2026 (35% in 2025), about the same as the rest, so reserves do not explain the gap [Modeled, `capture_rate/output/capture_by_reserve_use.csv`, from `by_duration.py`].
- **Hub vs node price.** Grid batteries settle at their own node, not the hub. Homes settle at their load zone, which is what the price-history analysis uses.
- **Short windows.** 30 and 62 summer days are not a year. Summer 2026 had only 1 notable spike (Jul 22). The capture rate is more portable than $/kW-yr.
- **Spike days.** 8 days is a small sample, and the 60-day lag hides Aug and Sep 2026 (including 08/26, the 5th biggest evening spike). On the 2 January winter-event days the fleet was nearly full but mostly held for reserves, so "ran low" is a summer and spring pattern, not a rule.
- **2 summers do not prove a cause.** The rise in capture from 2025 to 2026 fits better trading after RTC+B, but other things changed too.
- **Grid batteries are not home batteries.** They are larger, run by professional traders, and do not protect a home's backup floor. Treat capture rate as a benchmark for "what a good operator captures," not as Base's number.
- **2026 is partial.** Price files run through Sep 19, 2026; 2026 yearly values are annualized from 262 days.
- **Modo's $29.4/kW** is a published estimate (a Nowcast that included 1 forecast month), covers energy plus reserves for the whole fleet, and was not recomputed here.

## What this means for OpenGrid

1. **Capacity contracts are the business.** Austin Energy's tolling agreement pays up to $102/kW-yr [Sourced]. Texas grid batteries averaged about $29/kW from the whole market in 2025 [Second-hand, Modo Energy], and real grid batteries earned $7 to $9/kW-yr from energy trading in our windows [Modeled]. Even perfect foresight gave a home battery only $54 to $89/kW-yr in 2025 and 2026 [Modeled].
2. **Trading is shrinking upside.** The home-battery trading pie fell 64% to 71% from 2023 to 2025 [Modeled], though 2023 was an unusually spiky year. It is worth doing, but it is the upside, not the base case.
3. **The value is in being charged and available at the few hours that matter.** Since RTC+B, 35% to 44% of trading value sits in the top 1% of intervals, and missing them costs 25% to 33% [Modeled]. Those hours are mostly late evening, when on 5 of the 8 biggest 2026 evening spikes with data the grid battery fleet had drained to 16% to 34% [Sourced data, Modeled ratio]. A home battery that is still charged at 9 PM, and a fleet that does not drop homes during those intervals, is what a utility is paying for. On homes under a utility contract (like Austin Energy's), the utility decides when they run and Base provides the batteries and the uptime, so the orchestrator's work there is availability, never trading. On homes where Base is the retailer, it honors reserve commitments and the backup floor first, then trades what is left.

## Folder contents

| Path | What it is |
|---|---|
| `spike_soc/run.py` | Grid battery fleet at 2026 evening spikes (13052 ESR table + 13061) |
| `spike_soc/output/` | `spike_days.csv`, `fleet_5min.csv`, `fleet_hourly_evening.csv`, `day_ranking_2026.csv`, `houston_over_300_by_hour_2026.csv`, `summary.md` |
| `capture_rate/run.py` | Capture rate after RTC+B (ESR table in 13052) |
| `capture_rate/pre_rtcb.py` | Capture rate before RTC+B (paired generator and load records) |
| `capture_rate/by_duration.py` | Capture rate by hours of storage |
| `capture_rate/output/` | `summary.md`, `summer2025_summary.md`, per-battery-day CSVs (2025 gzipped), `capture_by_duration.csv` |
| `price_history/fetch.py` | Downloads and tidies 13061 and 13060 |
| `price_history/arb.py` | Home-battery perfect-foresight LP |
| `price_history/years.py`, `h5.py`, `h5_jan.py`, `h7.py` | Year trend, top-1% concentration and its January 2026 share, day-ahead vs real-time |
| `price_history/results/*.csv` | Result tables |
| `reserves/nonspin.py` | Day-ahead reserve value by product and regime (13091) |
| `reserves/output/` | `as_value_by_regime.csv`, `nspin_by_hour_post.csv`, `summary.md` |
| `requirements.txt` | pandas, numpy, scipy, openpyxl |
