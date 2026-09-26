"""Capture rate of Texas grid batteries BEFORE the Dec 5, 2025 market change (RTC+B).

One command:  python pre_rtcb.py --start 2025-07-01 --end 2025-08-31
Needs:        same as run.py (pandas, scipy, openpyxl); reuses run.py's download code and perfect-foresight LP.
              Run run.py first: this script reads output/capture_by_battery_day.csv for 2026 battery durations.
Output:       output/summer2025_capture_by_battery_day.csv, output/summer2025_pairing.csv,
              output/summer2025_2026_sensitivity.csv, output/summer2025_summary.md

Why a separate path: before RTC+B the 60-day SCED zip has no ESR table. Each battery was 2 resources:
  - a generation resource (Gen_Resource_Data, Resource Type "PWRSTR"): discharge, "Telemetered Net Output " (>= 0)
  - a controllable load resource (Load_Resource_Data_in_SCED): charging, "Real Power Consumption" (>= 0)
There is no state of charge, so energy size (MWh) is not in the file. Rows are one snapshot per 15 minutes
(post-RTC+B files have every 5-minute SCED run), so each snapshot is weighted as 15 minutes.

Pairing gen to load (same QSE, and the same name prefix before the last "_", else the same first "_" token):
  tier 1  same trailing number        ANCHOR_BESS2 <-> ANCHOR_LD2
  tier 2  only one candidate left     FTDUNCAN_BESS_GEN <-> FTDUNCAN_LD1
  tier 3  closest charge MW to discharge MW (within 30%) among several candidates
Unpaired batteries are dropped from the per-battery numbers and counted in the pairing rate.

Energy size [Assumed]: MWh = duration x max HSL. Duration comes from the same battery's 2026 ERCOT state-of-charge
range (Max SOC - Min SOC, from the post-RTC+B ESR table, matched by site prefix and unit number), or 1.5 h
(the 2026 fleet median) when no 2026 match exists. Sensitivity: every battery at 1 h, and at 2 h.
"""

import argparse
import glob
import io
import os
import re
import zipfile
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pandas as pd

import run
from run import PRICE_POINT, RTE, perfect_foresight

HERE = os.path.dirname(os.path.abspath(__file__))
DL, OUT = os.path.join(HERE, "downloads", "pre_rtcb"), os.path.join(HERE, "output")
DEFAULT_H = 1.5  # [Assumed] hours of storage when the battery has no 2026 SOC data (2026 fleet median is 1.49 h)
SENS_H = (1.0, 2.0)
GEN_USE = [
    "SCED Time Stamp",
    "QSE",
    "Resource Name",
    "Resource Type",
    "HSL",
    "LSL",
    "Telemetered Resource Status",
    "Telemetered Net Output ",
    "Ancillary Service REGUP",
    "Ancillary Service REGDN",
    "Ancillary Service RRS",
    "Ancillary Service RRSFFR",
    "Ancillary Service NSRS",
    "Ancillary Service ECRS",
]
LOAD_USE = [
    "SCED Time Stamp",
    "QSE",
    "Resource Name",
    "Telemetered Resource Status",
    "Max Power Consumption",
    "Low Power Consumption",
    "Real Power Consumption",
    "AS Responsibility for RRS",
    "AS Responsibility for RRSFFR",
    "AS Responsibility for NonSpin",
    "AS Responsibility for RegUp",
    "AS Responsibility for RegDown",
    "AS Responsibility for ECRS",
]


def site(n):
    return n.split("_")[0]


def prefix(n):
    return n.rsplit("_", 1)[0]


def unit(n):
    return (re.findall(r"(\d+)$", n) or ["1"])[0].lstrip("0") or "1"


def pct(x):
    return f"{x:.0%}"


# ---------- 1. download and extract ----------
def fetch_one(op_day, docs):
    """Zip published ~60 days after the operating day. Keep storage gen rows + candidate load rows in memory only."""
    tag = op_day.strftime("%Y%m%d")
    gpath, lpath = os.path.join(DL, f"gen_{tag}.csv"), os.path.join(DL, f"load_{tag}.csv")
    if os.path.exists(gpath) and os.path.exists(lpath):
        return tag, "cached"
    for off in (60, 59, 61, 58, 62):
        pub = (op_day + pd.Timedelta(days=off)).strftime("%Y%m%d")
        doc = next((d for d in docs if f".{pub}." in d["ConstructedName"]), None)
        if doc is None:
            continue
        z = zipfile.ZipFile(io.BytesIO(run.download(doc)))
        gname = next(x for x in z.namelist() if "Gen_Resource_Data" in x)
        lname = next(x for x in z.namelist() if "Load_Resource_Data" in x)
        gen = pd.read_csv(z.open(gname), usecols=GEN_USE, low_memory=False)
        first = pd.to_datetime(gen["SCED Time Stamp"].iloc[0], format="%m/%d/%Y %H:%M:%S")
        if first.normalize() != op_day:
            continue
        gen = gen[gen["Resource Type"] == "PWRSTR"]
        load = pd.read_csv(z.open(lname), usecols=LOAD_USE, low_memory=False)
        keys = set(zip(gen.QSE, gen["Resource Name"].map(site), strict=True))
        load = load[[k in keys for k in zip(load.QSE, load["Resource Name"].map(site), strict=True)]]
        gen.to_csv(gpath, index=False)
        load.to_csv(lpath, index=False)
        return tag, f"downloaded {doc['ConstructedName']}"
    return tag, "not found"


def fetch_days(start, end, workers=4):
    os.makedirs(DL, exist_ok=True)
    docs = [d for d in run.list_docs(13052) if d["ConstructedName"].endswith(".zip")]
    days = pd.date_range(start, end)
    with ThreadPoolExecutor(workers) as ex:
        for tag, msg in ex.map(lambda d: fetch_one(d, docs), days):
            print(f"  {tag}: {msg}", flush=True)
    return [d.strftime("%Y%m%d") for d in days if os.path.exists(os.path.join(DL, f"gen_{d:%Y%m%d}.csv"))]


def load_prices(year):
    """Same series as run.fetch_prices, but reuse the cached year file (a past year never changes)."""
    path = os.path.join(run.DL, f"rt_prices_{year}.csv")
    if not os.path.exists(path):
        return run.fetch_prices({year})
    p = pd.read_csv(path)
    p["date"] = pd.to_datetime(p["Delivery Date"], format="%m/%d/%Y")
    p = p[p["Repeated Hour Flag"] == "N"]
    return p.set_index(["date", "Delivery Hour", "Delivery Interval"])["Settlement Point Price"]


def day_prices(prices, day):
    try:
        p = prices.loc[day]
    except KeyError:
        return None
    p = p[~p.index.duplicated()]
    return p if len(p) >= 92 else None


# ---------- 2. pairing ----------
def pair(gen, load):
    """Return {gen name: (load name, tier)} using QSE + site prefix + unit number + MW size."""
    gi = gen.groupby("Resource Name").agg(qse=("QSE", "first"), mw=("HSL", "max"))
    li = load.groupby("Resource Name").agg(qse=("QSE", "first"), mw=("Max Power Consumption", "max"))
    used, out = set(), {}

    def cands_for(n):  # prefer the full prefix (BRP_PBL1_UNIT1 -> BRP_PBL1), fall back to the first token
        same = [m for m in li.index if li.qse[m] == gi.qse[n]]
        full = [m for m in same if prefix(m) == prefix(n)]
        return full or [m for m in same if site(m) == site(n)]

    cands = {n: cands_for(n) for n in gi.index}
    for n in gi.index:  # tier 1: same unit number
        m = [x for x in cands[n] if unit(x) == unit(n) and x not in used]
        if len(m) == 1:
            out[n] = (m[0], 1)
            used.add(m[0])
    for n in gi.index:  # tier 2: a single candidate left
        if n in out:
            continue
        m = [x for x in cands[n] if x not in used]
        if len(m) == 1:
            out[n] = (m[0], 2)
            used.add(m[0])
    for n in gi.index:  # tier 3: closest MW within 30%
        if n in out:
            continue
        m = [x for x in cands[n] if x not in used and gi.mw[n] > 0 and abs(li.mw[x] / gi.mw[n] - 1) <= 0.3]
        if m:
            best = min(m, key=lambda x: abs(li.mw[x] - gi.mw[n]))
            out[n] = (best, 3)
            used.add(best)
    return out


# ---------- 3. durations from 2026 ERCOT SOC data ----------
def durations_2026():
    r = pd.read_csv(os.path.join(OUT, "capture_by_battery_day.csv"))
    b = r.groupby("battery").agg(mw=("mw", "max"), mwh=("mwh", "median"))
    b = b[(b.mw >= 1) & (b.mwh > 0)]
    h = (b.mwh / b.mw).clip(0.5, 4.0)
    return {(site(n), unit(n)): v for n, v in h.items()}


# ---------- 4. analysis ----------
def analyse_2025(tags, prices, dur26):
    rows, prow = [], []
    for tag in tags:
        gen = pd.read_csv(os.path.join(DL, f"gen_{tag}.csv"))
        load = pd.read_csv(os.path.join(DL, f"load_{tag}.csv"))
        for d in (gen, load):
            d["ts"] = pd.to_datetime(d["SCED Time Stamp"], format="%m/%d/%Y %H:%M:%S")
        day = gen.ts.dt.normalize().mode()[0]
        gen = gen[gen.ts.dt.normalize() == day]
        load = load[load.ts.dt.normalize() == day]
        p = day_prices(prices, day)
        if p is None:
            print(f"  no complete prices for {day.date()}; skipped")
            continue
        pvec = p.sort_index().values.astype(float)
        pairs = pair(gen, load)
        active = gen.groupby("Resource Name").HSL.max()
        active = set(active[active >= 1].index)
        for n in sorted(gen["Resource Name"].unique()):
            prow.append(
                dict(
                    date=day.date(),
                    gen=n,
                    load=pairs.get(n, (None,))[0],
                    tier=pairs.get(n, (None, 0))[1],
                    active=n in active,
                )
            )
        for n, (m, tier) in pairs.items():
            if n not in active:
                continue
            gg = gen[gen["Resource Name"] == n].set_index("ts")
            ll = load[load["Resource Name"] == m].set_index("ts")
            ll = ll[~ll.index.duplicated()]
            gg = gg[~gg.index.duplicated()]
            d = gg.join(ll, how="outer", rsuffix="_ld").sort_index()
            d["dis"] = d["Telemetered Net Output "].fillna(0).clip(lower=0)
            d["chg"] = d["Real Power Consumption"].fillna(0).clip(lower=0)
            d["net"] = d.dis - d.chg
            ts = d.index.to_series()
            # pre-RTC+B disclosure is one snapshot per 15 min (post-RTC+B is every 5-min SCED run)
            d["hours"] = ((ts.shift(-1) - ts).dt.total_seconds() / 3600).clip(upper=20 / 60).fillna(0.25).values
            d["price"] = [p.get((t.hour + 1, t.minute // 15 + 1), np.nan) for t in d.index]
            d["mwh"] = d.net * d.hours
            as_cols = [c for c in d.columns if c.startswith("Ancillary Service") or c.startswith("AS Responsibility")]
            as_mwh = (d[as_cols].fillna(0).sum(axis=1) * d.hours).sum()
            p_dis = d.HSL.max()
            p_chg = d["Max Power Consumption"].max()
            if not (p_dis >= 1 and p_chg >= 1):
                continue
            h26 = dur26.get((site(n), unit(n)))
            h = h26 if h26 else DEFAULT_H
            online = (d["Telemetered Resource Status"].fillna("OUT") != "OUT") | (
                d["Telemetered Resource Status_ld"].fillna("OUTL") != "OUTL"
            )
            rows.append(
                dict(
                    date=day.date(),
                    battery=n,
                    load_resource=m,
                    pair_tier=tier,
                    qse=gg.QSE.iloc[0],
                    mw=round(p_dis, 1),
                    charge_mw=round(p_chg, 1),
                    duration_h=round(h, 2),
                    duration_source="2026 ERCOT SOC range" if h26 else f"assumed {DEFAULT_H} h",
                    mwh=round(h * p_dis, 1),
                    hours_online=round(d.hours[online].sum(), 1),
                    actual_usd=round((d.mwh * d.price).sum(), 0),
                    perfect_usd=round(perfect_foresight(pvec, p_dis, p_chg, h * p_dis), 0),
                    perfect_1h_usd=round(perfect_foresight(pvec, p_dis, p_chg, 1.0 * p_dis), 0),
                    perfect_2h_usd=round(perfect_foresight(pvec, p_dis, p_chg, 2.0 * p_dis), 0),
                    discharged_mwh=round(d.mwh.clip(lower=0).sum(), 1),
                    charged_mwh=round(-d.mwh.clip(upper=0).sum(), 1),
                    both_on_share=round(((d.dis > 1) & (d.chg > 1)).mean(), 3),
                    as_awarded_mwh=round(as_mwh, 1),
                    day_max_price=round(pvec.max(), 1),
                    day_spread=round(pvec.max() - pvec.min(), 1),
                )
            )
        n_paired = sum(n in active for n in pairs)
        print(f"  {day.date()}: {len(active)} active storage gens, {n_paired} paired", flush=True)
    return pd.DataFrame(rows), pd.DataFrame(prow)


def sensitivity_2026(prices26):
    """Re-solve the 2026 perfect-foresight value at 1 h and 2 h so both summers are measured the same way."""
    path = os.path.join(OUT, "summer2025_2026_sensitivity.csv")
    if os.path.exists(path):
        return pd.read_csv(path)
    base = pd.read_csv(os.path.join(OUT, "capture_by_battery_day.csv"))
    rows = []
    for f in sorted(glob.glob(os.path.join(run.DL, "*_ESR.csv"))):
        d = pd.read_csv(f, usecols=["SCED Time Stamp", "Resource Name", "HSL", "LSL", "Minimum SOC", "Maximum SOC"])
        d["ts"] = pd.to_datetime(d["SCED Time Stamp"], format="%m/%d/%Y %H:%M:%S")
        day = d.ts.dt.normalize().mode()[0]
        d = d[d.ts.dt.normalize() == day]
        p = day_prices(prices26, day)
        if p is None:
            continue
        pvec = p.sort_index().values.astype(float)
        for n, g in d.groupby("Resource Name"):
            p_dis = g.HSL.clip(lower=0).max()
            p_chg = (-g.LSL).clip(lower=0).max()
            e = (g["Maximum SOC"] - g["Minimum SOC"]).median()
            if not (p_dis >= 1 and p_chg >= 1 and e >= 0.5):  # same filter as run.py
                continue
            rows.append(
                dict(
                    date=str(day.date()),
                    battery=n,
                    perfect_1h_usd=round(perfect_foresight(pvec, p_dis, p_chg, 1.0 * p_dis), 0),
                    perfect_2h_usd=round(perfect_foresight(pvec, p_dis, p_chg, 2.0 * p_dis), 0),
                )
            )
        print(f"  2026 {day.date()} re-solved at 1 h and 2 h", flush=True)
    s = base.merge(pd.DataFrame(rows), on=["date", "battery"], how="inner")
    s.to_csv(path, index=False)
    return s


# ---------- 5. summary ----------
def metrics(r, pcol="perfect_usd"):
    """Same measures as run.summarise, for any perfect-foresight column."""
    r = r[r[pcol].notna()]
    ndays = r.date.nunique()
    pb = r.groupby("battery").agg(a=("actual_usd", "sum"), p=(pcol, "sum"), mw=("mw", "max"))
    pb = pb[pb.p > 0]
    pb["cap"] = pb.a / pb.p
    pb["kw_yr"] = pb.a / (pb.mw * 1000) / ndays * 365
    pb["pf_kw_yr"] = pb.p / (pb.mw * 1000) / ndays * 365
    top = r.groupby("date").day_max_price.first().nlargest(max(1, ndays // 10)).index
    t, o = r[r.date.isin(top)], r[~r.date.isin(top)]
    return {
        "fleet": r.actual_usd.sum() / r[pcol].sum(),
        "median": pb.cap.median(),
        "top": t.actual_usd.sum() / t[pcol].sum(),
        "rest": o.actual_usd.sum() / o[pcol].sum(),
        "kw": pb.kw_yr.median(),
        "kw75": pb.kw_yr.quantile(0.75),
        "pfkw": pb.pf_kw_yr.median(),
        "share_top": t[pcol].sum() / r[pcol].sum(),
        "days": ndays,
        "bats": r.battery.nunique(),
        "maxp": r.groupby("date").day_max_price.first().max(),
    }


def summarise(r25, pr25, s26):
    m25, m26 = metrics(r25), metrics(s26)
    m25_1, m25_2 = metrics(r25, "perfect_1h_usd"), metrics(r25, "perfect_2h_usd")
    m26_1, m26_2 = metrics(s26, "perfect_1h_usd"), metrics(s26, "perfect_2h_usd")
    act = pr25[pr25.active]
    rate = act.load.notna().mean()
    tiers = act.tier.value_counts(normalize=True)
    bat = act.drop_duplicates("gen")
    own_size = (r25.duration_source != f"assumed {DEFAULT_H} h").mean()
    lines = [
        "# Capture rate: summer 2025 (before RTC+B) vs summer 2026",
        "",
        f"2025: {m25['days']} operating days ({r25.date.min()} to {r25.date.max()}), {m25['bats']} paired batteries "
        f"[Sourced: ERCOT 13052]. 2026: {m26['days']} days ({s26.date.min()} to {s26.date.max()}), "
        f"{m26['bats']} batteries.",
        f"Price: real-time {PRICE_POINT} (ERCOT 13061) [Sourced]. Round-trip efficiency {RTE:.0%} [Assumed]. "
        "Energy trading only.",
        f"Pairing (2025): {pct(rate)} of active storage battery-days paired to a charging load "
        f"(tier 1 unit number {pct(tiers.get(1, 0))}, tier 2 single candidate {pct(tiers.get(2, 0))}, "
        f"tier 3 MW match {pct(tiers.get(3, 0))}); {bat.gen.nunique()} distinct active storage gens.",
        f"2025 energy size [Assumed]: {pct(own_size)} of battery-days "
        f"use the same battery's 2026 SOC range; the rest assume {DEFAULT_H} h at max HSL.",
        "",
        "| Measure | Summer 2025 | Summer 2026 |",
        "|---|---|---|",
        f"| Fleet capture rate [Modeled] | {pct(m25['fleet'])} | {pct(m26['fleet'])} |",
        f"| Median battery capture rate [Modeled] | {pct(m25['median'])} | {pct(m26['median'])} |",
        f"| Capture on top 10% price days [Modeled] | {pct(m25['top'])} | {pct(m26['top'])} |",
        f"| Capture on all other days [Modeled] | {pct(m25['rest'])} | {pct(m26['rest'])} |",
        f"| Median actual energy earnings, $/kW-yr annualized [Modeled] | {m25['kw']:.0f} | {m26['kw']:.0f} |",
        f"| Top-quartile battery, $/kW-yr [Modeled] | {m25['kw75']:.0f} | {m26['kw75']:.0f} |",
        f"| Median perfect-foresight value, $/kW-yr [Modeled] | {m25['pfkw']:.0f} | {m26['pfkw']:.0f} |",
        f"| Share of perfect-foresight value on top 10% days [Modeled] | {pct(m25['share_top'])} | "
        f"{pct(m26['share_top'])} |",
        f"| Highest 15-min hub price in window, $/MWh [Sourced] | {m25['maxp']:.0f} | {m26['maxp']:.0f} |",
        "",
        "Sensitivity to energy size (every battery set to the same duration) [Assumed size, Modeled result]:",
        "",
        "| Fleet capture rate | Summer 2025 | Summer 2026 |",
        "|---|---|---|",
        f"| 1 h at max MW | {pct(m25_1['fleet'])} | {pct(m26_1['fleet'])} |",
        f"| Main case (2025: 2026 SOC or {DEFAULT_H} h; 2026: actual SOC range) | {pct(m25['fleet'])} | "
        f"{pct(m26['fleet'])} |",
        f"| 2 h at max MW | {pct(m25_2['fleet'])} | {pct(m26_2['fleet'])} |",
        f"| Median battery, 1 h / 2 h | {pct(m25_1['median'])} / {pct(m25_2['median'])} | "
        f"{pct(m26_1['median'])} / {pct(m26_2['median'])} |",
        "",
        "Caveats: hub price, not each battery's node; energy trading only, excludes ancillary service revenue;",
        "2025 batteries are gen + load pairs matched by name, and their MWh is estimated, not reported.",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--start", default="2025-07-01")
    ap.add_argument("--end", default="2025-08-31")
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    if pd.Timestamp(a.end) >= pd.Timestamp("2025-12-05"):
        raise SystemExit("pre_rtcb.py is for operating days before 2025-12-05; use run.py after that")
    os.makedirs(OUT, exist_ok=True)
    print("1/4 storage gen + load data (ERCOT 13052)")
    tags = fetch_days(a.start, a.end, a.workers)
    print("2/4 prices (ERCOT 13061)")
    p25 = load_prices(pd.Timestamp(a.start).year)
    p26 = load_prices(2026)
    print("3/4 summer 2025 analysis")
    r25, pr25 = analyse_2025(tags, p25, durations_2026())
    r25.to_csv(os.path.join(OUT, "summer2025_capture_by_battery_day.csv"), index=False)
    pr25.to_csv(os.path.join(OUT, "summer2025_pairing.csv"), index=False)
    print("4/4 summer 2026, same measures and size sensitivity")
    s26 = sensitivity_2026(p26)
    s = summarise(r25, pr25, s26)
    with open(os.path.join(OUT, "summer2025_summary.md"), "w", encoding="utf-8") as fh:
        fh.write(s + "\n")
    print("\n" + s)


if __name__ == "__main__":
    main()
