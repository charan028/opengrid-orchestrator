# What the ERCOT data shows that most people miss (Track 1 evidence)

Built on Nicola's OpenGrid architecture; data analysis by Frank Brown with Claude Code.

## BLUF

Trading energy with a battery is a shrinking, spiky business. Real Texas grid batteries capture only about 2 in 5 of the trading dollars on the table, and the whole trading pie for a home battery shrank 64% to 71% from 2023 to 2025. What is left sits in a few evening hours, exactly when the grid battery fleet has already run low. So the money for a home-battery fleet is in capacity contracts, and the job of the orchestrator is to be charged and available in the few hours that matter.

Labels: **Sourced** = read directly from an ERCOT file or a public document. **Modeled** = our computation on sourced data. **Assumed** = an input we chose.

| # | Finding | Number | Label | Evidence |
|---|---|---|---|---|
| 1 | Texas grid batteries capture a minority of the energy-trading value available to them. Summer 2026, 30 days, 305 batteries | Fleet 41%, median battery 37% | Modeled | `capture_rate/output/summary.md` |
| 2 | Capture is higher on spike days, so operators do show up when it counts, but still leave half on the table | 53% on the top 10% price days, 35% on other days | Modeled | same |
| 3 | Capture was lower before ERCOT's Dec 5, 2025 market redesign (RTC+B). Summer 2025, 62 days, 239 batteries | Fleet 32%, median 27%, top 10% days 40% | Modeled | `capture_rate/output/summer2025_summary.md` |
| 4 | What grid batteries actually earn from energy trading is small | Median $7/kW-yr (summer 2026), $9 (summer 2025); top quartile $11 and $16 | Modeled | both summaries |
| 5 | A utility capacity contract pays an order of magnitude more | Austin Energy tolling agreement with Base Power: about $102/kW-yr ($4.08M a year for 40 MW, up to 10 years) | Sourced | City of Austin council item, RCA 26-1526, Item 8, Apr 23, 2026 |
| 6 | The perfect-foresight arbitrage value of a home battery fell sharply from 2023 to 2025 | $1,927 to $2,818 per home in 2023 ($175 to $256/kW-yr), $653 to $979 in 2025 ($59 to $89/kW-yr); down 64% to 71% by zone | Modeled | `price_history/results/prices_years_2021_2026.csv` |
| 7 | The summer evening premium collapsed after 2023 | Evening (HE19-22) minus solar hours (HE10-15): $156 to $226/MWh in 2023, $23 to $41 in 2026 | Sourced | same |
| 8 | A few hours pay for the battery | The top 1% of 15-min intervals (about 88 hours a year) hold 27% to 54% of a home battery's arbitrage value | Modeled | `price_history/results/prices_h5_top1pct_value_by_zone.csv` |
| 9 | Missing only those hours is expensive | Unavailable in the top 1% intervals: 18% to 46% of annual value lost, even after re-optimizing the rest of the day; 25% to 33% after RTC+B | Modeled | same |
| 10 | Most days are nearly worthless for trading | Median day $0.99 to $1.54 per home after RTC+B | Modeled | same |
| 11 | After RTC+B, day-ahead and real-time converge in normal hours but diverge in stress hours | At LZ_HOUSTON the median hourly spread moved from -$2.06 to -$0.98/MWh while its standard deviation rose from $35 to $53 | Sourced | `price_history/results/prices_h7_da_rt_spread_by_zone.csv` |
| 12 | One winter event holds a big slice of post-RTC+B day-ahead value | Jan 25-26, 2026: day-ahead up to $1,694 (Houston) and $2,280 (CPS) while hourly real-time stayed at or below $379 (Houston); the 2 days hold 17% to 23% of all post-RTC+B day-ahead arbitrage value | Sourced prices, Modeled value | `price_history/results/prices_h7_jan2026_event.csv` |
| 13 | The grid battery fleet runs low before the price peaks | Fleet state of charge at the 4 biggest 2026 evening spikes: 16% to 65%; on 3 of the 4 days it was 16% to 34%, and the price peak came 1.0 to 1.6 hours after peak fleet discharge | Sourced | Supply dossier, see "Other findings" |
| 14 | The most valuable reserve product for a battery is now Non-Spin, and it pays in the evening | Day-ahead Non-Spin $35.9/kW-yr after RTC+B (up from $25.4), vs $15.0 to $18.8 for RRS, ECRS and Reg-Up; 57% of Non-Spin value falls in HE19-24 | Modeled | Reserves dossier, see "Other findings" |

## How each number was derived

**Capture rate (findings 1 to 4).** ERCOT's 60-Day SCED Disclosure (report 13052) shows every 5 minutes, for every grid battery in Texas, what it actually did: MW in or out, state of charge, size, and reserve awards.
- Actual earnings = telemetered net output (MW, discharge positive) x time to the next SCED run x the real-time hub price (HB_HUBAVG, report 13061). Charging counts as a cost.
- Perfect foresight = a small linear program (the same kind the orchestrator uses) that finds the best possible trading for a battery of the same MW and MWh on the same day's prices, knowing them in advance. State of charge ends the day where it started.
- Capture rate = actual / perfect foresight. Fleet = total dollars over total dollars. Median = the median of each battery's own ratio over the window.
- $/kW-yr = each battery's actual dollars / its MW / days in the window x 365.
- Before RTC+B there is no battery table. Each battery was 2 records: a generator (type "PWRSTR") for discharging and a load resource for charging. `pre_rtcb.py` pairs them by owner code (QSE) and name, for example ANCHOR_BESS2 with ANCHOR_LD2. 99.9% of active battery-days paired. 2025 files have no state of charge, so each battery's hours of storage come from its own 2026 data (97% of battery-days) or 1.5 hours (the 2026 fleet median) [Assumed]. A 1-hour and 2-hour sensitivity is in the summary: 2026 capture stays above 2025 under every size assumption.

**Price history (findings 6 to 12).** ERCOT's yearly price files, real-time 15-min (13061) and day-ahead hourly (13060), for the 8 load zones.
- A perfect-foresight linear program for one Base Power home battery is solved for every zone and every day (`arb.py`). The battery is 39.2 kWh, 11 kW, a 20% backup floor that is never crossed, 88% round-trip efficiency, and at most 1 full cycle a day [Assumed].
- `years.py` sums that by year (findings 6 and 7). Divide $ per home by 11 kW for $/kW-yr.
- `h5.py` ranks intervals by price and measures the share of value in the top 1%, then re-solves the day with the battery blocked in exactly those intervals (findings 8 to 10).
- `h7.py` compares the hourly mean of the 4 real-time prices with the day-ahead price, pre vs post RTC+B, over the same calendar days (Dec 5 to Sep 19) on each side (findings 11 and 12).

## Reproduce it

No ERCOT login or API key. Python 3.10 or newer.

```
cd analysis
python -m venv .venv
.venv\Scripts\activate            (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt

# Price history: about 15 MB of downloads per year, 20 to 40 minutes in total
python price_history/fetch.py     # downloads 13061 + 13060 for 2021-2026 into price_history/data/
python price_history/arb.py       # home-battery LP for every zone-day
python price_history/years.py     # -> results/prices_years_2021_2026.csv
python price_history/h5.py        # -> results/prices_h5_top1pct_value_by_zone.csv
python price_history/h7.py        # -> results/prices_h7_*.csv

# Capture rate: about 55 MB download per operating day; 30 to 60 minutes for 30 days
python capture_rate/run.py --days 30
python capture_rate/pre_rtcb.py --start 2025-07-01 --end 2025-08-31   # run after run.py
```

Notes:
- `run.py --days 30` uses the newest 30 operating days on ERCOT's site, so the window moves forward 1 day each day. The committed result covers June 29 to July 28, 2026 (the newest 30 on Sep 26, 2026).
- Downloads and intermediate files go to `capture_rate/downloads/` and `price_history/data/`, which are git-ignored. Set `OPENGRID_PRICE_DATA` to keep the price data somewhere else.
- `capture_rate/output/summer2025_capture_by_battery_day.csv.gz` is the 2025 detail, gzipped to keep the repo small. pandas reads it directly: `pd.read_csv("...csv.gz")`.

## Datasets and provenance

All data is real, public ERCOT market data. Nothing here is synthetic. Every report is listed at `https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=<id>` and each file downloads from `https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=<DocID>`.

| ERCOT report id | Name | Used for | Date range used | Real or synthetic | Terms |
|---|---|---|---|---|---|
| 13052 (NP3-965-ER) | 60-Day SCED Disclosure Reports | Battery telemetry: output, state of charge, size, reserve awards | 2025-07-01 to 2025-08-31; 2026-06-29 to 2026-07-28 | Real | ERCOT public data |
| 13061 | Historical RTM Load Zone and Hub Prices (15-min) | Real-time prices, hub and load zones | 2021-01-01 to 2026-09-19 | Real | ERCOT public data |
| 13060 | Historical DAM Load Zone and Hub Prices (hourly) | Day-ahead prices | 2024-01-01 to 2026-09-19 in results (fetch.py pulls 2021 on) | Real | ERCOT public data |
| 13091 | Historical DAM Clearing Prices for Capacity | Non-Spin and other reserve prices (finding 14) | 2025 to 2026-09-19 | Real | ERCOT public data |
| n/a | City of Austin council item RCA 26-1526, Item 8, Apr 23, 2026 | Austin Energy tolling agreement value (finding 5) | 2026 | Real | Public record |

"ERCOT public data" means reports ERCOT posts publicly on its Market Information System with no login. Check ERCOT's website terms of use before redistributing raw files; this folder commits only our derived summaries, never the raw ERCOT files. No personal data is used or committed. Battery and owner names in the CSVs are the public resource and QSE codes that ERCOT itself discloses.

## Assumptions

- **Home battery [Assumed]:** 39.2 kWh usable and 11 kW per Base Power unit, 20% backup floor (7.84 kWh) never crossed, 88% round-trip efficiency, at most 1 floor-to-full cycle per day, state of charge ends each day where it started.
- **Grid battery [Assumed]:** 85% round-trip efficiency. Size from ERCOT's own data (max HSL for MW, Max SOC minus Min SOC for MWh).
- **Grid battery price [Assumed]:** the ERCOT hub average (HB_HUBAVG) stands in for each battery's own node price.
- **2025 battery size [Assumed]:** the same battery's 2026 hours of storage, or 1.5 hours when no 2026 match exists.
- **Perfect foresight** is an upper bound. No real operator knows tomorrow's prices.

## Caveats

- **Energy trading only.** Both analyses exclude ancillary service (reserve) revenue. Many grid batteries earn reserves instead of trading, which makes them look like low-capture traders. The capture CSV has `as_awarded_mwh` so you can filter them. Batteries that rarely hold reserves (under 4 hours a day) still capture only 42%, so reserves do not explain the gap.
- **Hub vs node price.** Grid batteries settle at their own node, not the hub. Homes settle at their load zone, which is what the price-history analysis uses.
- **Short windows.** 30 and 62 summer days are not a year. Summer 2026 had only 1 notable spike (July 22). Annualized $/kW-yr swings with the season; the capture rate is the more portable number.
- **2 summers do not prove a cause.** The rise in capture from 2025 to 2026 fits better trading after RTC+B, but other things changed too.
- **Grid batteries are not home batteries.** They are larger, run by professional traders, and do not protect a home's backup floor. Treat capture rate as a benchmark for "what a good operator captures," not as Base's number.
- **2026 is partial.** Price files run through Sep 19, 2026; 2026 yearly values are annualized from 262 days.

## What this means for OpenGrid

1. **Capacity contracts are the business.** Austin Energy's tolling agreement pays about $102/kW-yr [Sourced: City of Austin council item]. Real grid batteries earned $7 to $9/kW-yr from energy trading in these windows [Modeled], and even perfect foresight gave a home battery only $54 to $89/kW-yr in 2025 and 2026 [Modeled].
2. **Trading is shrinking upside.** The home-battery trading pie fell 64% to 71% from 2023 to 2025 as more batteries chase the same evening spread [Modeled]. It is worth doing, but it is the upside, not the base case.
3. **The value is in being charged and available at the few hours that matter.** 27% to 54% of trading value sits in about 88 hours a year, and missing them costs 18% to 46% [Modeled]. Those hours are in the evening, when the grid battery fleet has often already drained to 16% to 34% [Sourced]. A home battery that is still charged at 9 PM, and a fleet that does not drop homes during those intervals, is what a utility is paying for. That is the orchestrator's job: honor the capacity commitment and the backup floor first, then trade what is left.

## Other findings from the same data pass (scripts not yet in this folder)

These come from the team's Friday night data survey of ERCOT's public reports. The numbers are real, but their scripts have not been moved into this repo yet.
- **Grid battery fleet at 2026 evening spikes** (13052 ESR table + 13061): on 03/23, 04/27 and 07/22, peak fleet discharge came around 20:00 to 20:25 and the price peak 1.0 to 1.6 hours later, with fleet state of charge at 16% to 34%. On 04/24 the peak coincided with max discharge at 65%. 68% of 2026 LZ_HOUSTON 15-min intervals above $300 fell in HE20-23 [Sourced].
- **Non-Spin** (13091): after RTC+B, Non-Spin is the only reserve product whose day-ahead price rose, to $35.9/kW-yr vs $25.4 in 2025 before RTC+B. 57% of its value sits in HE19-24 and only 6% in HE14-18 [Modeled].

## Folder contents

| Path | What it is |
|---|---|
| `capture_rate/run.py` | Capture rate after RTC+B (ESR table in 13052) |
| `capture_rate/pre_rtcb.py` | Capture rate before RTC+B (paired generator and load records) |
| `capture_rate/output/summary.md`, `summer2025_summary.md` | Results tables |
| `capture_rate/output/capture_by_battery_day.csv` | Summer 2026 detail, 1 row per battery per day |
| `capture_rate/output/summer2025_capture_by_battery_day.csv.gz` | Summer 2025 detail |
| `price_history/fetch.py` | Downloads and tidies 13061 and 13060 |
| `price_history/arb.py` | Home-battery perfect-foresight LP |
| `price_history/years.py`, `h5.py`, `h7.py` | Year trend, top-1% concentration, day-ahead vs real-time |
| `price_history/results/*.csv` | Result tables |
| `requirements.txt` | pandas, numpy, scipy, openpyxl |
