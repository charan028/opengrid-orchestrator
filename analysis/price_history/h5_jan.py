"""How much of the post-RTC+B top-1% value comes from the late-January 2026 winter event?

One command:  python h5_jan.py      (after fetch.py and arb.py; no download, seconds)
Output:       results/prices_h5_post_rtcb_jan2026_share.csv

Per load zone, over Dec 5, 2025 to Sep 19, 2026 [Modeled on ERCOT 13061 prices, home-battery LP from arb.py]:
  top1pct_share        share of perfect-foresight value in the top 1% priced 15-min intervals (same as h5.py)
  jan_share_of_top1pct share of that top-1% value earned Jan 24 to 31, 2026
  jan_share_of_all     share of all value in the window earned Jan 24 to 31, 2026
"""

import os

import pandas as pd

from arb import DATA, RESULTS

WINDOW = ("2025-12-05", "2026-09-19")
EVENT = ("2026-01-24", "2026-01-31")


def main():
    a = pd.read_pickle(os.path.join(DATA, "arb_rt.pkl"))
    a = a[(a.date >= WINDOW[0]) & (a.date <= WINDOW[1])]
    rows = []
    for zone, g in a.groupby("sp"):
        top = g[g.price >= g.price.quantile(0.99)]
        jan = top[(top.date >= EVENT[0]) & (top.date <= EVENT[1])]
        all_jan = g[(g.date >= EVENT[0]) & (g.date <= EVENT[1])]
        rows.append(
            dict(
                zone=zone,
                value_usd=round(g.rev.sum(), 2),
                top1pct_share=round(top.rev.sum() / g.rev.sum(), 3),
                jan_share_of_top1pct=round(jan.rev.sum() / top.rev.sum(), 3),
                jan_share_of_all=round(all_jan.rev.sum() / g.rev.sum(), 3),
            )
        )
    out = pd.DataFrame(rows)
    os.makedirs(RESULTS, exist_ok=True)
    out.to_csv(os.path.join(RESULTS, "prices_h5_post_rtcb_jan2026_share.csv"), index=False)
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
