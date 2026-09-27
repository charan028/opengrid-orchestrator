"""Day-ahead reserve (ancillary service) prices before and after RTC+B: which product pays a battery most, and when.

One command:  python nonspin.py
Needs:        Python 3.10+, then  pip install -r ../requirements.txt. No ERCOT login or API key.
Data:         ERCOT public MIS report 13091, Historical DAM Clearing Prices for Capacity (hourly MCPC, $/MW-h),
              1 zipped CSV per year, listed at https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=13091
Output:       output/as_value_by_regime.csv   $/kW-yr by product and regime
              output/nspin_by_hour_post.csv    post-RTC+B Non-Spin value by hour ending
              output/summary.md

Method: $/kW-yr = mean hourly day-ahead MCPC ($/MW-h) x 8,760 h / 1,000, i.e. the payment for 1 kW sold in every
hour of the year [Modeled]. Regimes: 2024; 2025-01-01 to 2025-12-04 ("2025 pre"); 2025-12-05 to --through ("post",
after the RTC+B go-live).
--raw-dir DIR reads already-downloaded 13091 files (*DAMASMCPC_<year>.csv or .zip) instead of downloading.
"""

import argparse
import glob
import io
import json
import os
import time
import urllib.request
import zipfile

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DL, OUT = os.path.join(HERE, "downloads"), os.path.join(HERE, "output")
UA = {"User-Agent": "opengrid-analysis-reserves (research; low volume)"}
LIST_URL = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={}"
DOWNLOAD_URL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={}"
PRODUCTS = ["REGUP", "REGDN", "RRS", "ECRS", "NSPIN"]
RTCB = pd.Timestamp("2025-12-05")
THROUGH = "2026-09-19"  # last day in the 2026 file used by the Friday reserves dossier
EVENT = (pd.Timestamp("2026-01-24"), pd.Timestamp("2026-01-27"))  # January 2026 winter event


def get(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=600).read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5)


def read_year(year, raw_dir=None):
    if raw_dir:
        f = [x for x in glob.glob(os.path.join(raw_dir, "**", f"*DAMASMCPC_{year}.*"), recursive=True)]
        if not f:
            raise SystemExit(f"no 13091 file for {year} under {raw_dir}")
        src = f[0]
    else:
        src = os.path.join(DL, f"DAMASMCPC_{year}.zip")
        if not os.path.exists(src) or year == max(YEARS):  # the current year keeps growing: refresh it
            j = json.loads(get(LIST_URL.format(13091)))
            doc = next(
                d["Document"]
                for d in j["ListDocsByRptTypeRes"]["DocumentList"]
                if d["Document"]["FriendlyName"] == f"DAMASMCPC_{year}"
            )
            print(f"  downloading {doc['ConstructedName']} ...", flush=True)
            os.makedirs(DL, exist_ok=True)
            with open(src, "wb") as fh:
                fh.write(get(DOWNLOAD_URL.format(doc["DocID"])))
    if src.endswith(".zip"):
        z = zipfile.ZipFile(src)
        d = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    else:
        d = pd.read_csv(src)
    d.columns = [c.strip() for c in d.columns]
    d["date"] = pd.to_datetime(d["Delivery Date"], format="%m/%d/%Y")
    d["he"] = d["Hour Ending"].str[:2].astype(int)
    return d


YEARS = (2024, 2025, 2026)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", help="read already-downloaded 13091 files under this folder")
    ap.add_argument("--through", default=THROUGH)
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    d = pd.concat([read_year(y, a.raw_dir) for y in YEARS], ignore_index=True)
    d = d[d.date <= pd.Timestamp(a.through)]
    d["regime"] = "2024"
    d.loc[(d.date.dt.year == 2025) & (d.date < RTCB), "regime"] = "2025 pre"
    d.loc[d.date >= RTCB, "regime"] = "post"
    post = d[d.regime == "post"]
    in_event = post.date.between(*EVENT)

    def kw_yr(x):
        return (x[PRODUCTS].mean() * 8.76).round(1)

    v = d.groupby("regime")[PRODUCTS].mean().mul(8.76).round(1)
    v.loc["post, excluding Jan 24 to 27 2026"] = kw_yr(post[~in_event])
    v["hours"] = d.groupby("regime").size()
    v.loc["post, excluding Jan 24 to 27 2026", "hours"] = int((~in_event).sum())
    v = v.loc[["2024", "2025 pre", "post", "post, excluding Jan 24 to 27 2026"]]
    v.index.name = "regime"
    v.to_csv(os.path.join(a.out, "as_value_by_regime.csv"))

    tot = post.NSPIN.sum()
    h = post.groupby("he").NSPIN.agg(["mean", "sum"]).rename(columns={"mean": "mcpc_mean", "sum": "mcpc_sum"})
    h["share_of_value"] = (h.mcpc_sum / tot).round(3)
    summer = post[post.date.dt.month.between(6, 9)].groupby("he").NSPIN.mean().round(1)
    h["mcpc_mean_jun_sep"] = summer
    h = h.round(2).reset_index().rename(columns={"he": "hour_ending"})
    h.to_csv(os.path.join(a.out, "nspin_by_hour_post.csv"), index=False)

    eve = post[post.he.between(19, 24)].NSPIN.sum() / tot
    aft = post[post.he.between(14, 18)].NSPIN.sum() / tot
    ev_share = post[in_event].NSPIN.sum() / tot
    lines = [
        "# Day-ahead reserve prices before and after RTC+B (ERCOT 13091)",
        "",
        f"Hours: {len(d):,} ({d.date.min().date()} to {d.date.max().date()}); post-RTC+B {len(post):,} "
        f"from {RTCB.date()}. Prices [Sourced 13091]; $/kW-yr = mean MCPC x 8.76 [Modeled].",
        "",
        "| Regime | " + " | ".join(PRODUCTS) + " | Hours |",
        "|---|" + "---|" * (len(PRODUCTS) + 1),
    ]
    for r, x in v.iterrows():
        lines.append(f"| {r} | " + " | ".join(f"{x[p]:.1f}" for p in PRODUCTS) + f" | {int(x.hours):,} |")
    lines += [
        "",
        f"- Post-RTC+B Non-Spin value in HE19 to HE24: {eve:.0%}; in HE14 to HE18: {aft:.0%}; "
        f"Jan 24 to 27, 2026 holds {ev_share:.0%} [Modeled].",
        f"- Jun to Sep 2026 Non-Spin mean MCPC: HE17 ${summer.get(17, float('nan')):.1f}, "
        f"HE21 ${summer.get(21, float('nan')):.1f}, HE22 ${summer.get(22, float('nan')):.1f}, "
        f"HE23 ${summer.get(23, float('nan')):.1f} per MW-h [Sourced 13091].",
        "",
        "Caveat: $/kW-yr assumes 1 kW is sold in every hour; a home battery can hold only as many kW as its "
        "energy above the backup floor supports, and reserve awards also require qualification.",
    ]
    s = "\n".join(lines)
    with open(os.path.join(a.out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(s + "\n")
    print(s)


if __name__ == "__main__":
    main()
