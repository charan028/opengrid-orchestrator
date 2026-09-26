"""Is the spread between cheap and expensive hours still large? Home-battery arbitrage value by year, 2021 to 2026.

One command:  python years.py      (after fetch.py and arb.py)
Output:       results/prices_years_2021_2026.csv

Per year and load zone (real-time 15-min prices, ERCOT 13061; battery as in arb.py [Assumed]):
  value_per_home_yr   perfect-foresight arbitrage $ per home, annualized (divide by 11 for $/kW-yr) [Modeled]
  median_day          median daily value $ [Modeled]
  share_top10_days    share of the year's value earned on its 10 best days [Modeled]
  days_over_5usd      days worth more than $5 [Modeled]
  summer_eve_*        Jun-Sep average price HE19-22 minus HE10-15 (solar hours), $/MWh [Sourced 13061]
Also repeated with Winter Storm Uri (Feb 10 to 20, 2021) removed.
"""

import argparse
import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("OPENGRID_PRICE_DATA", os.path.join(HERE, "data"))
RESULTS = os.path.join(HERE, "results")
ZONES = ["LZ_HOUSTON", "LZ_NORTH", "LZ_SOUTH", "LZ_WEST"]


def summ(df, label):
    out = []
    for (y, sp), g in df.groupby(["year", "sp"]):
        v = g.value.sort_values(ascending=False)
        days = len(v)
        out.append(
            dict(
                set=label,
                year=y,
                zone=sp,
                days=days,
                value_per_home_yr=round(v.sum() * 365 / days, 0),
                median_day=round(v.median(), 2),
                share_top10_days=round(v.head(10).sum() / v.sum(), 3),
                days_over_5usd=int((v > 5).sum()),
            )
        )
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    p = pd.read_pickle(os.path.join(DATA, "arb_rt.pkl"))
    p = p[p.sp.isin(ZONES)]
    p = p[p.date >= "2021-01-01"].copy()
    p["year"] = p.date.dt.year
    day = p.groupby(["sp", "date"]).agg(value=("rev", "sum"), pmax=("price", "max"), pmin=("price", "min"))
    day = day.reset_index()
    day["year"] = day.date.dt.year

    res = summ(day, "all days")
    uri = (day.date >= "2021-02-10") & (day.date <= "2021-02-20")
    res += summ(day[~uri], "excl Winter Storm Uri")
    res = pd.DataFrame(res)

    # evening premium in summer (Jun-Sep): avg price HE19-22 minus avg HE10-15 (solar hours)
    s = p[p.date.dt.month.between(6, 9)]
    eve = s[s.hour.between(19, 22)].groupby(["year", "sp"]).price.mean()
    mid = s[s.hour.between(10, 15)].groupby(["year", "sp"]).price.mean()
    prem = (eve - mid).round(1).rename("summer_eve_minus_midday").reset_index()
    prem = prem.merge(eve.round(1).rename("summer_eve_avg").reset_index())
    prem = prem.merge(mid.round(1).rename("summer_midday_avg").reset_index())
    res = res.merge(prem, left_on=["year", "zone"], right_on=["year", "sp"], how="left").drop(columns="sp")
    os.makedirs(RESULTS, exist_ok=True)
    res.to_csv(os.path.join(RESULTS, "prices_years_2021_2026.csv"), index=False)
    pd.set_option("display.width", 200)
    print(res.to_string(index=False))


if __name__ == "__main__":
    main()
