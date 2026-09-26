"""Day-ahead vs real-time prices before and after RTC+B (Dec 5, 2025): normal hours converge, stress hours diverge.

One command:  python h7.py      (after fetch.py and arb.py; no LP, under a minute)
Output:       results/prices_h7_da_rt_spread_by_zone.csv     spread stats per period and zone
              results/prices_h7_spread_by_hour_regime.csv    spread by hour of day, pre vs post RTC+B
              results/prices_h7_spread_by_month_regime.csv   spread by month, pre vs post RTC+B
              results/prices_h7_jan2026_event.csv            the Jan 25-26, 2026 day-ahead spike

Spread = hourly mean of the 4 real-time 15-min prices (13061, type LZ) minus the day-ahead price (13060), $/MWh
[Sourced]. The Jan 2026 table adds home-battery dollars [Modeled, battery as in arb.py].
"""

import argparse
import os

import numpy as np
import pandas as pd

from arb import DATA, KW, LZ, RESULTS
from h5 import PERIODS

EVENT = ("2026-01-25", "2026-01-26")


def spread_table(m):
    rows = []
    for pn, (a, b) in PERIODS.items():
        for z, g in m[(m.date >= a) & (m.date <= b)].groupby("sp"):
            s = g.spread
            top = s.abs().nlargest(int(np.ceil(0.01 * len(s))))
            rows.append(
                dict(
                    period=pn,
                    zone=z,
                    hours=len(g),
                    mean_da=round(g.da.mean(), 2),
                    mean_rt=round(g.rt.mean(), 2),
                    mean_spread_rt_minus_da=round(s.mean(), 2),
                    median_spread=round(s.median(), 2),
                    mean_abs_spread=round(s.abs().mean(), 2),
                    std_spread=round(s.std(), 2),
                    p01=round(s.quantile(0.01), 2),
                    p99=round(s.quantile(0.99), 2),
                    pct_hours_abs_gt_20=round((s.abs() > 20).mean() * 100, 2),
                    pct_hours_rt_gt_da_plus_100=round((s > 100).mean() * 100, 2),
                    corr_da_rt=round(g.da.corr(g.rt), 3),
                    share_abs_spread_from_top1pct_hours=round(top.sum() / s.abs().sum(), 3),
                    # name kept for continuity: this is the MEAN spread with the top 1% |spread| hours removed
                    median_spread_excl_top1pct=round(s[s.abs() < s.abs().quantile(0.99)].mean(), 2),
                )
            )
    return pd.DataFrame(rows)


def jan_event(m, arb_da):
    a, b = EVENT
    post_a, post_b = PERIODS["POST_RTCB"]
    rows = []
    for z in LZ:
        e = m[(m.sp == z) & (m.date >= a) & (m.date <= b)]
        da_sum = -e.spread.sum()  # sum over 48 h of DA minus RT, $/MWh-h
        d = arb_da[arb_da.sp == z]
        v2 = d[(d.date >= a) & (d.date <= b)].rev.sum()
        vpost = d[(d.date >= post_a) & (d.date <= post_b)].rev.sum()
        rows.append(
            dict(
                zone=z,
                max_da_hourly=round(e.da.max(), 2),
                max_rt_hourly_avg=round(e.rt.max(), 2),
                sum_da_minus_rt_48h=round(da_sum, 0),
                usd_per_home_11kw_da_sell_rt_buyback_48h=round(da_sum * KW / 1000, 2),
                da_lp_value_2days=round(v2, 2),
                da_lp_value_post_total=round(vpost, 2),
                da_2day_share_of_post=round(v2 / vpost, 3),
            )
        )
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    rt = pd.read_pickle(os.path.join(DATA, "rt_tidy.pkl"))
    da = pd.read_pickle(os.path.join(DATA, "da_tidy.pkl"))
    rh = (
        rt[(rt.type == "LZ") & rt.sp.isin(LZ)]
        .groupby(["sp", "date", "hour", "repn"])
        .price.agg(["mean", "max"])
        .reset_index()
        .rename(columns={"mean": "rt", "max": "rt_max15"})
    )
    dah = da[da.sp.isin(LZ)][["sp", "date", "hour", "repn", "price"]].rename(columns={"price": "da"})
    m = rh.merge(dah, on=["sp", "date", "hour", "repn"], how="inner")
    m["spread"] = m.rt - m.da
    os.makedirs(RESULTS, exist_ok=True)

    out = spread_table(m)
    out.to_csv(os.path.join(RESULTS, "prices_h7_da_rt_spread_by_zone.csv"), index=False)
    pd.set_option("display.width", 300)
    pd.set_option("display.max_columns", 30)
    print(out.to_string())

    m["reg"] = np.where(
        m.date >= "2025-12-05", "POST_RTCB", np.where(m.date >= PERIODS["PRE_matched"][0], "PRE_matched", "other")
    )
    m = m[m.reg != "other"]  # here PRE_matched is the full year before go-live (2024-12-05 to 2025-12-04)
    hp = m.groupby(["reg", "sp", "hour"]).spread.agg(["mean", "median"]).round(2).reset_index()
    hp.to_csv(os.path.join(RESULTS, "prices_h7_spread_by_hour_regime.csv"), index=False)
    mm = m.assign(mon=m.date.dt.month).groupby(["sp", "reg", "mon"]).spread.mean().round(2).unstack("reg")
    mm.to_csv(os.path.join(RESULTS, "prices_h7_spread_by_month_regime.csv"))

    ev = jan_event(m, pd.read_pickle(os.path.join(DATA, "arb_da.pkl")))
    ev.to_csv(os.path.join(RESULTS, "prices_h7_jan2026_event.csv"), index=False)
    print(ev.to_string(index=False))


if __name__ == "__main__":
    main()
