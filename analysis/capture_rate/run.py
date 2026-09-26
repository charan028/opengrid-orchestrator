"""Capture rate of Texas grid batteries: what they actually earned trading energy vs the most they could have.

One command:  python run.py --days 30
Needs:        Python 3.10+, then  pip install -r ../requirements.txt
Data:         ERCOT public reports, no login or API key.
  13052  60-Day SCED Disclosure (one zip per operating day, about 55 MB; published 60 days after the day)
  13061  Real-time 15-min settlement point prices (one zip per year)
Output:       output/capture_by_battery_day.csv, output/summary.md

Method (see ../README.md for caveats):
  actual  = sum over SCED runs of telemetered net output (MW, + = discharge) x hours to next run x hub price
  perfect = best possible energy trading that day for a battery of the same size, same prices, perfect foresight
  capture = actual / perfect
"""

import argparse
import io
import json
import os
import sys
import time
import urllib.request
import zipfile

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import lil_matrix

HERE = os.path.dirname(os.path.abspath(__file__))
DL, OUT = os.path.join(HERE, "downloads"), os.path.join(HERE, "output")
UA = {"User-Agent": "opengrid-analysis-capture-rate (research; low volume)"}
PRICE_POINT = "HB_HUBAVG"  # proxy: batteries really settle at their own node price
RTE = 0.85  # [Assumed] round-trip efficiency of a grid battery
USE = [
    "SCED Time Stamp",
    "Resource Name",
    "QSE",
    "HSL",
    "LSL",
    "Telemetered Resource Status",
    "Telemetered Net Output",
    "State of Charge",
    "Minimum SOC",
    "Maximum SOC",
    "AS Awards NSPIN",
    "AS Awards RRSFFR",
    "AS Awards RRSPFR",
    "AS Awards RRSUFR",
    "AS Awards ECRS",
    "AS Awards REGUP",
    "AS Awards REGDN",
]
AS_COLS = [c for c in USE if c.startswith("AS Awards")]
LIST_URL = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={}"
DOWNLOAD_URL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={}"


def get(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=600).read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5)


def list_docs(report_id):
    j = json.loads(get(LIST_URL.format(report_id)))
    return [d["Document"] for d in j["ListDocsByRptTypeRes"]["DocumentList"]]


def download(doc):
    return get(DOWNLOAD_URL.format(doc["DocID"]))


def fetch_esr_days(n_days):
    """Download the newest n SCED disclosure zips; keep only the battery (ESR) table as a small CSV."""
    os.makedirs(DL, exist_ok=True)
    docs = [d for d in list_docs(13052) if d["ConstructedName"].endswith(".zip")][:n_days]
    files = []
    for i, d in enumerate(docs, 1):
        csv = os.path.join(DL, d["ConstructedName"].replace(".zip", "_ESR.csv"))
        if not os.path.exists(csv):
            print(f"  [{i}/{len(docs)}] downloading {d['ConstructedName']} ...", flush=True)
            z = zipfile.ZipFile(io.BytesIO(download(d)))
            names = [x for x in z.namelist() if "ESR_Data_in_SCED" in x]
            if not names:
                print("    no battery table in this file (before Dec 5, 2025); skipped")
                continue
            pd.read_csv(z.open(names[0]), usecols=USE, low_memory=False).to_csv(csv, index=False)
        files.append(csv)
    return files


def fetch_prices(years):
    frames = []
    os.makedirs(DL, exist_ok=True)
    docs = list_docs(13061)
    for y in sorted(years):
        path = os.path.join(DL, f"rt_prices_{y}.csv")
        if not os.path.exists(path) or y == max(years):  # current year keeps growing: refresh it
            doc = next(d for d in docs if d["FriendlyName"].endswith(f"_{y}"))
            z = zipfile.ZipFile(io.BytesIO(download(doc)))
            f = z.namelist()[0]
            if f.endswith("xlsx"):
                raw = pd.concat(pd.read_excel(z.open(f), sheet_name=None).values())
            else:
                raw = pd.read_csv(z.open(f))
            raw = raw[raw["Settlement Point Name"] == PRICE_POINT]
            raw.to_csv(path, index=False)
        frames.append(pd.read_csv(path))
    p = pd.concat(frames)
    p["date"] = pd.to_datetime(p["Delivery Date"], format="%m/%d/%Y")
    p = p[p["Repeated Hour Flag"] == "N"]
    return p.set_index(["date", "Delivery Hour", "Delivery Interval"])["Settlement Point Price"]


def perfect_foresight(prices, p_dis, p_chg, e_mwh, dt=0.25):
    """Best energy-trading revenue ($) for one day. SoC ends where it started (level chosen freely)."""
    T = len(prices)
    n = 3 * T
    cost = np.concatenate([prices * dt, -prices * dt, np.zeros(T)])  # minimize charge cost - discharge revenue
    A = lil_matrix((T, n))
    for t in range(T):  # s_t - s_{t-1} - RTE*c_t*dt + d_t*dt = 0
        A[t, 2 * T + t] = 1
        A[t, 2 * T + (t - 1) % T] = -1
        A[t, t] = -RTE * dt
        A[t, T + t] = dt
    bounds = [(0, p_chg)] * T + [(0, p_dis)] * T + [(0, e_mwh)] * T
    r = linprog(cost, A_eq=A.tocsr(), b_eq=np.zeros(T), bounds=bounds, method="highs")
    return -r.fun if r.success else np.nan


def analyse(files, prices):
    rows = []
    for f in files:
        d = pd.read_csv(f)
        d["ts"] = pd.to_datetime(d["SCED Time Stamp"], format="%m/%d/%Y %H:%M:%S")
        day = d.ts.dt.normalize().mode()[0]
        d = d[d.ts.dt.normalize() == day].sort_values(["Resource Name", "ts"])
        try:
            p = prices.loc[day]
        except KeyError:
            print(f"  no prices yet for {day.date()}; skipped")
            continue
        p = p[~p.index.duplicated()]
        if len(p) < 92:
            print(f"  incomplete prices for {day.date()}; skipped")
            continue
        d["price"] = [p.get((h, i), np.nan) for h, i in zip(d.ts.dt.hour + 1, d.ts.dt.minute // 15 + 1, strict=True)]
        nxt = d.groupby("Resource Name").ts.shift(-1)
        d["hours"] = ((nxt - d.ts).dt.total_seconds() / 3600).clip(upper=10 / 60).fillna(5 / 60)
        d["mwh"] = d["Telemetered Net Output"] * d.hours
        d["rev"] = d.mwh * d.price
        d["as_mwh"] = d[AS_COLS].fillna(0).sum(axis=1) * d.hours
        pvec = p.sort_index().values.astype(float)
        for name, g in d.groupby("Resource Name"):
            p_dis = g.HSL.clip(lower=0).max()
            p_chg = (-g.LSL).clip(lower=0).max()
            e = (g["Maximum SOC"] - g["Minimum SOC"]).median()
            if not (p_dis >= 1 and p_chg >= 1 and e >= 0.5):
                continue
            online = g["Telemetered Resource Status"].isin(["ON", "ONTEST"])
            rows.append(
                dict(
                    date=day.date(),
                    battery=name,
                    qse=g.QSE.iloc[0],
                    mw=round(p_dis, 1),
                    mwh=round(e, 1),
                    hours_online=round(g.loc[online, "hours"].sum(), 1),
                    actual_usd=round(g.rev.sum(), 0),
                    perfect_usd=round(perfect_foresight(pvec, p_dis, p_chg, e), 0),
                    discharged_mwh=round(g.mwh.clip(lower=0).sum(), 1),
                    charged_mwh=round(-g.mwh.clip(upper=0).sum(), 1),
                    soc_change_mwh=round(g["State of Charge"].iloc[-1] - g["State of Charge"].iloc[0], 1),
                    as_awarded_mwh=round(g.as_mwh.sum(), 1),
                    day_max_price=round(pvec.max(), 1),
                    day_spread=round(pvec.max() - pvec.min(), 1),
                )
            )
        print(f"  {day.date()}: {d['Resource Name'].nunique()} batteries", flush=True)
    return pd.DataFrame(rows)


def summarise(r):
    ndays = r.date.nunique()
    fleet_cap = r.actual_usd.sum() / r.perfect_usd.sum()
    per_bat = r.groupby("battery").agg(a=("actual_usd", "sum"), p=("perfect_usd", "sum"), mw=("mw", "max"))
    per_bat = per_bat[per_bat.p > 0]
    per_bat["cap"] = per_bat.a / per_bat.p
    per_bat["kw_yr"] = per_bat.a / (per_bat.mw * 1000) / ndays * 365
    top = r.groupby("date").day_max_price.first().nlargest(max(1, ndays // 10)).index
    cap_top = r[r.date.isin(top)]
    cap_rest = r[~r.date.isin(top)]
    lines = [
        "# Capture rate of Texas grid batteries",
        "",
        f"Days analysed: {ndays} ({r.date.min()} to {r.date.max()}), batteries: {r.battery.nunique()}.",
        f"Price: real-time {PRICE_POINT} (ERCOT 13061). Round-trip efficiency {RTE:.0%} [Assumed]. "
        "Energy trading only.",
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Fleet capture rate, energy trading (actual / perfect foresight) [Modeled] | {fleet_cap:.0%} |",
        f"| Median battery capture rate [Modeled] | {per_bat.cap.median():.0%} |",
        f"| Capture on the top 10% price days [Modeled] | {cap_top.actual_usd.sum() / cap_top.perfect_usd.sum():.0%} |",
        f"| Capture on all other days [Modeled] | {cap_rest.actual_usd.sum() / cap_rest.perfect_usd.sum():.0%} |",
        f"| Median actual energy earnings, annualised, $/kW-yr [Modeled] | {per_bat.kw_yr.median():.0f} |",
        f"| Top-quartile battery, $/kW-yr [Modeled] | {per_bat.kw_yr.quantile(0.75):.0f} |",
        f"| Share of perfect-foresight value earned on top 10% price days | "
        f"{cap_top.perfect_usd.sum() / r.perfect_usd.sum():.0%} |",
        "",
        "Caveats: excludes reserve (ancillary service) payments, which many batteries earn instead of trading;",
        "uses the hub price, not each battery's own node; annualising a short window overstates or understates",
        "seasonal effects. Compare with the Austin Energy toll of $102/kW-yr [Sourced] and the home-battery",
        "perfect-foresight value of $54 to $89/kW-yr in 2025 and 2026 [Modeled, prices_years_2021_2026.csv].",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=30, help="how many of the newest operating days to use (default 30)")
    ap.add_argument("--files", nargs="*", help="use existing ESR CSVs instead of downloading (for testing)")
    ap.add_argument("--out", default=OUT, help="output folder (default: output/ next to this script)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    print("1/3 battery data (ERCOT 60-day SCED disclosure)")
    files = a.files or fetch_esr_days(a.days)
    if not files:
        sys.exit("no battery data found")
    years = {int(pd.read_csv(f, nrows=1)["SCED Time Stamp"].str[6:10].iloc[0]) for f in files}
    print("2/3 prices (ERCOT 13061)")
    prices = fetch_prices(years)
    print("3/3 analysis")
    r = analyse(files, prices)
    r.to_csv(os.path.join(a.out, "capture_by_battery_day.csv"), index=False)
    s = summarise(r)
    with open(os.path.join(a.out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(s + "\n")
    print("\n" + s)


if __name__ == "__main__":
    main()
