"""A few hours pay for the battery: how much home-battery arbitrage value sits in the top 1% of intervals?

One command:  python h5.py      (after fetch.py and arb.py; re-solves the LP on blocked days, a few minutes)
Output:       results/prices_h5_top1pct_value_by_zone.csv

Per period and load zone [Modeled on ERCOT 13061 / 13060 prices]:
  share_value_in_top1pct_price_intervals  share of perfect-foresight value earned in the top 1% priced 15-min intervals
  share_value_top10_days                  share earned on the 10 best days
  lost_share_if_unavailable_top1pct       value lost if the battery is unavailable in exactly those intervals,
                                          after the LP re-optimizes the rest of the day around the outage
  rt_over_da                              15-min real-time value vs hourly day-ahead value
"""

import argparse
import os

import numpy as np
import pandas as pd

from arb import DATA, RESULTS, run

# Windows. Data ran through 2026-09-19 when this was built. PRE_matched and POST_RTCB cover the same calendar
# days on either side of RTC+B go-live (2025-12-05).
PERIODS = {
    "CY2024": ("2024-01-01", "2024-12-31"),
    "CY2025": ("2025-01-01", "2025-12-31"),
    "YTD2026": ("2026-01-01", "2026-09-19"),
    "PRE_matched": ("2024-12-05", "2025-09-19"),
    "POST_RTCB": ("2025-12-05", "2026-09-19"),
}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    rt = pd.read_pickle(os.path.join(DATA, "arb_rt.pkl"))
    da = pd.read_pickle(os.path.join(DATA, "arb_da.pkl"))
    rows = []
    for pn, (a, b) in PERIODS.items():
        for z, g in rt[(rt.date >= a) & (rt.date <= b)].groupby("sp"):
            V = g.rev.sum()
            k = int(np.ceil(0.01 * len(g)))
            top = g.nlargest(k, "price")
            topc = g.nlargest(k, "rev")
            daily = g.groupby("date").rev.sum().sort_values(ascending=False)
            gd = da[(da.sp == z) & (da.date >= a) & (da.date <= b)]
            VD = gd.rev.sum()
            kd = int(np.ceil(0.01 * len(gd)))
            # blocking: battery unavailable in the top-1% price intervals; perfect foresight re-optimizes around it
            thr = top.price.min()
            gg = g.copy()
            gg["blk"] = gg.price >= thr
            days = gg[gg.blk].date.unique()
            sub = gg[gg.date.isin(days)]
            rb = run(sub[["sp", "date", "pos", "price", "blk"]], 0.25, ["sp", "date"], blockmask="blk")
            Vb = V - sub.rev.sum() + rb.rev.sum()
            n_top_days = max(1, int(np.ceil(0.01 * len(daily))))
            rows.append(
                dict(
                    period=pn,
                    zone=z,
                    days=g.date.nunique(),
                    rt_value_usd=round(V, 2),
                    da_value_usd=round(VD, 2),
                    rt_over_da=round(V / VD, 2),
                    top1pct_price_threshold=round(thr, 2),
                    share_value_in_top1pct_price_intervals=round(top.rev.sum() / V, 3),
                    share_value_in_top1pct_contrib_intervals=round(topc.rev.sum() / V, 3),
                    share_value_top10_days=round(daily.head(10).sum() / V, 3),
                    share_value_top1pct_days=round(daily.head(n_top_days).sum() / V, 3),
                    median_day_value=round(daily.median(), 3),
                    da_share_value_top1pct_hours=round(gd.nlargest(kd, "price").rev.sum() / VD, 3),
                    value_if_unavailable_top1pct=round(Vb, 2),
                    lost_share_if_unavailable_top1pct=round(1 - Vb / V, 3),
                )
            )
            print(rows[-1], flush=True)
    os.makedirs(RESULTS, exist_ok=True)
    pd.DataFrame(rows).to_csv(os.path.join(RESULTS, "prices_h5_top1pct_value_by_zone.csv"), index=False)


if __name__ == "__main__":
    main()
