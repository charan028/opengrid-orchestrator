"""How charged was the Texas grid battery fleet when 2026 evening prices peaked?

One command:  python run.py
Needs:        Python 3.10+, then  pip install -r ../requirements.txt. No ERCOT login or API key.
Data:         ERCOT public MIS, listed at https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=<id>
  13052  60-Day SCED Disclosure (NP3-965-ER), table 60d_ESR_Data_in_SCED: every grid battery (ESR) at every
         5-minute SCED run. 1 zip per operating day, about 55 MB, published about 60 days after the day.
  13061  Historical RTM Load Zone and Hub Prices, 2026 file: 15-minute real-time prices (1 zipped xlsx).
Output:       output/spike_days.csv            1 row per spike day: price peak, fleet SoC and discharge at the peak
              output/fleet_5min.csv            fleet totals at every SCED run on those days, joined to prices
              output/fleet_hourly_evening.csv  HE17 to HE24 hourly averages on those days
              output/day_ranking_2026.csv      every 2026 day ranked by its highest 15-minute hub price
              output/houston_over_300_by_hour_2026.csv  LZ_HOUSTON 15-minute intervals above $300, by hour
              output/summary.md

Days: by default, every 2026 day through --through (the newest SCED day on MIS when this was run) whose highest
15-minute HB_HUBAVG price was at least $300 and fell in the evening (HE17 to HE24). The 4 days in the Friday
supply dossier (03/23, 04/24, 04/27, 07/22) are flagged. --days picks days explicitly.

Method, per day (the same as the dossier's h6.py):
  spike          the SCED run with the highest HB_HUBAVG price. Each SCED run gets the price of the 15-minute
                 interval its timestamp falls in; the first run in the top interval is the spike.
  fleet SoC %    sum of State of Charge / sum of Maximum SOC, over all ESRs in that SCED run (MWh / MWh)
  discharge %    sum of positive Telemetered Net Output / sum of HSL over ESRs with status ON or ONTEST
  peak SoC       highest fleet State of Charge sum that day; max discharge = highest positive output sum

--raw-dir DIR  reads already-downloaded ERCOT files instead of downloading: 13052 zips
(ext.00013052.*.60_Day_SCED_Disclosure.zip, or pre-extracted esr_sced_YYYYMMDD.csv) and the 13061 2026 zip or xlsx.
"""

import argparse
import glob
import io
import json
import os
import time
import urllib.request
import zipfile

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DL, OUT = os.path.join(HERE, "downloads"), os.path.join(HERE, "output")
UA = {"User-Agent": "opengrid-analysis-spike-soc (research; low volume)"}
LIST_URL = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={}"
DOWNLOAD_URL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={}"
HUB = "HB_HUBAVG"
POINTS = [HUB, "LZ_HOUSTON", "LZ_NORTH", "LZ_SOUTH"]
DOSSIER_DAYS = ["2026-03-23", "2026-04-24", "2026-04-27", "2026-07-22"]
THROUGH = "2026-07-28"  # newest operating day in the 60-day SCED disclosure on Sep 26, 2026
PRICE_THROUGH = "2026-09-19"  # last day in the 13061 2026 file used by the dossier
MIN_SPIKE = 300.0  # $/MWh
EVENING_HE = range(17, 25)
ESR_USE = [
    "SCED Time Stamp",
    "Resource Name",
    "Telemetered Resource Status",
    "HSL",
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
]
AS_UP = [c for c in ESR_USE if c.startswith("AS Awards")]  # every upward reserve award (Reg-Down excluded)
ONLINE = ["ON", "ONTEST"]


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


# ---------- prices (13061) ----------
def read_price_file(src):
    """src: a 13061 zip or xlsx path, or bytes of the zip. Returns the POINTS rows, all months."""
    if isinstance(src, bytes) or str(src).endswith(".zip"):
        z = zipfile.ZipFile(io.BytesIO(src) if isinstance(src, bytes) else src)
        src = io.BytesIO(z.read(z.namelist()[0]))
    sheets = pd.read_excel(src, sheet_name=None)
    p = pd.concat([s for s in sheets.values() if len(s)], ignore_index=True)
    return p[p["Settlement Point Name"].isin(POINTS)]


def load_prices(raw_dir=None):
    cache = os.path.join(DL, "rt_prices_2026.csv")
    if raw_dir:
        found = sorted(glob.glob(os.path.join(raw_dir, "**", "*RTMLZHBSPP_2026.*"), recursive=True))
        found = [f for f in found if f.endswith((".zip", ".xlsx"))]
        if not found:
            raise SystemExit(f"no 13061 2026 file (*RTMLZHBSPP_2026.zip or .xlsx) under {raw_dir}")
        print(f"  reading {os.path.basename(found[0])}", flush=True)
        p = read_price_file(found[0])
    elif os.path.exists(cache):
        p = pd.read_csv(cache)
    else:
        doc = next(d for d in list_docs(13061) if d["FriendlyName"].endswith("_2026"))
        print(f"  downloading {doc['ConstructedName']} ...", flush=True)
        p = read_price_file(get(DOWNLOAD_URL.format(doc["DocID"])))
        os.makedirs(DL, exist_ok=True)
        p.to_csv(cache, index=False)
    p = p.copy()
    p["date"] = pd.to_datetime(p["Delivery Date"], format="%m/%d/%Y")
    return p


def rank_days(p, through, sced_through):
    """Every day's highest 15-minute hub price, its hour ending and interval."""
    h = p[(p["Settlement Point Name"] == HUB) & (p.date <= through)]
    h = h.sort_values(["date", "Delivery Hour", "Repeated Hour Flag", "Delivery Interval"])
    top = h.loc[h.groupby("date")["Settlement Point Price"].idxmax()]
    r = pd.DataFrame(
        {
            "date": top.date.dt.date.astype(str),
            "max_price_hubavg": top["Settlement Point Price"].values,
            "hour_ending": top["Delivery Hour"].values,
            "interval": top["Delivery Interval"].values,
        }
    )
    r["evening"] = r.hour_ending.isin(EVENING_HE)
    r["sced_available"] = pd.to_datetime(r.date) <= sced_through
    r = r.sort_values("max_price_hubavg", ascending=False).reset_index(drop=True)
    r.insert(0, "rank", r.index + 1)
    r["evening_rank"] = np.where(r.evening, r.evening.cumsum(), np.nan)
    return r


def houston_over_300(p, through):
    h = p[(p["Settlement Point Name"] == "LZ_HOUSTON") & (p.date <= through) & (p["Settlement Point Price"] > 300)]
    n = h.groupby("Delivery Hour").size().rename("n_15min_over_300").reset_index()
    n = n.rename(columns={"Delivery Hour": "hour_ending"})
    n["share"] = (n.n_15min_over_300 / n.n_15min_over_300.sum()).round(3)
    return n, h.date.nunique()


# ---------- battery data (13052) ----------
def esr_from_zip(src, day):
    z = zipfile.ZipFile(src)
    name = next((x for x in z.namelist() if "ESR_Data_in_SCED" in x), None)
    if name is None:
        return None
    d = pd.read_csv(z.open(name), usecols=ESR_USE, low_memory=False)
    first = pd.to_datetime(d["SCED Time Stamp"].iloc[0], format="%m/%d/%Y %H:%M:%S")
    return d if first.normalize() == day else None


def load_esr(day, raw_dir=None, docs=None):
    """The ESR table for one operating day, from a local file or ERCOT's MIS."""
    tag = day.strftime("%Y%m%d")
    offsets = (60, 59, 61, 58, 62)  # the zip is published about 60 days after the operating day
    pubs = [(day + pd.Timedelta(days=o)).strftime("%Y%m%d") for o in offsets]
    if raw_dir:
        for pub in pubs:
            for f in glob.glob(os.path.join(raw_dir, "**", f"*00013052*.{pub}.*.zip"), recursive=True):
                d = esr_from_zip(f, day)
                if d is not None:
                    print(f"  {day.date()}: {os.path.basename(f)}", flush=True)
                    return d
        f = glob.glob(os.path.join(raw_dir, "**", f"esr_sced_{tag}.csv"), recursive=True)
        if f:
            print(f"  {day.date()}: {os.path.basename(f[0])}", flush=True)
            return pd.read_csv(f[0], usecols=ESR_USE, low_memory=False)
        raise SystemExit(f"no 13052 file for {day.date()} under {raw_dir}")
    cache = os.path.join(DL, f"esr_{tag}.csv")
    if os.path.exists(cache):
        return pd.read_csv(cache, low_memory=False)
    for pub in pubs:
        doc = next(
            (x for x in docs if f".{pub}." in x["ConstructedName"] and x["ConstructedName"].endswith(".zip")), None
        )
        if doc is None:
            continue
        print(f"  {day.date()}: downloading {doc['ConstructedName']} ...", flush=True)
        d = esr_from_zip(io.BytesIO(get(DOWNLOAD_URL.format(doc["DocID"]))), day)
        if d is not None:
            os.makedirs(DL, exist_ok=True)
            d.to_csv(cache, index=False)
            return d
    raise SystemExit(f"no 13052 file on MIS for {day.date()} (is it within the last 60 days?)")


# ---------- analysis ----------
def fleet(d, p, day):
    """Fleet totals at each SCED run, joined to that 15-minute interval's prices."""
    d = d.copy()
    d["ts"] = pd.to_datetime(d["SCED Time Stamp"], format="%m/%d/%Y %H:%M:%S")
    d = d[d.ts.dt.normalize() == day]
    d[AS_UP] = d[AS_UP].fillna(0)
    on = d["Telemetered Resource Status"].isin(ONLINE)
    d["on"] = on.astype(int)
    d["dis"] = d["Telemetered Net Output"].clip(lower=0)
    d["chg"] = (-d["Telemetered Net Output"]).clip(lower=0)
    d["hsl_on"] = np.where(on, d.HSL.clip(lower=0), 0)
    d["as_up"] = d[AS_UP].sum(axis=1)
    g = (
        d.groupby("ts")
        .agg(
            esrs=("Resource Name", "nunique"),
            n_online=("on", "sum"),
            dis_mw=("dis", "sum"),
            chg_mw=("chg", "sum"),
            online_hsl_mw=("hsl_on", "sum"),
            soc_mwh=("State of Charge", "sum"),
            max_soc_mwh=("Maximum SOC", "sum"),
            as_up_mw=("as_up", "sum"),
        )
        .reset_index()
    )
    pr = p[p.date == day].pivot_table(
        index=["Delivery Hour", "Delivery Interval"], columns="Settlement Point Name", values="Settlement Point Price"
    )
    g["hour_ending"] = g.ts.dt.hour + 1
    g["interval"] = g.ts.dt.minute // 15 + 1
    g = g.merge(pr, left_on=["hour_ending", "interval"], right_index=True, how="left")
    g["soc_pct"] = g.soc_mwh / g.max_soc_mwh
    g.insert(0, "date", str(day.date()))
    return g, d.groupby("Resource Name").ngroups


def day_row(g, n_esr):
    k = g.loc[g[HUB].idxmax()]  # first SCED run in the highest-price interval
    m = g.loc[g.dis_mw.idxmax()]
    s = g.loc[g.soc_mwh.idxmax()]
    return dict(
        date=k.date,
        in_dossier=k.date in DOSSIER_DAYS,
        esrs_in_table=n_esr,
        online_at_spike=int(k.n_online),
        spike_sced_time=k.ts.strftime("%H:%M:%S"),
        spike_price_hubavg=round(k[HUB], 2),
        soc_mwh_at_spike=round(k.soc_mwh),
        max_soc_mwh_at_spike=round(k.max_soc_mwh),
        soc_pct_at_spike=round(k.soc_pct, 3),
        dis_mw_at_spike=round(k.dis_mw),
        online_hsl_mw_at_spike=round(k.online_hsl_mw),
        dis_pct_of_online_hsl=round(k.dis_mw / k.online_hsl_mw, 3),
        as_up_mw_at_spike=round(k.as_up_mw),
        max_dis_time=m.ts.strftime("%H:%M:%S"),
        max_dis_mw=round(m.dis_mw),
        price_at_max_dis=round(m[HUB], 2),
        hours_max_dis_to_spike=round((k.ts - m.ts).total_seconds() / 3600, 2),
        peak_soc_time=s.ts.strftime("%H:%M:%S"),
        peak_soc_mwh=round(s.soc_mwh),
        peak_soc_pct=round(s.soc_pct, 3),
        hours_peak_soc_to_spike=round((k.ts - s.ts).total_seconds() / 3600, 2),
        fleet_duration_h=round(g.max_soc_mwh.max() / g.online_hsl_mw.max(), 2),
    )


def hourly(g):
    e = g[g.hour_ending.isin(EVENING_HE)]
    h = e.groupby(["date", "hour_ending"]).agg(
        price_avg=(HUB, "mean"),
        price_max=(HUB, "max"),
        dis_mw_avg=("dis_mw", "mean"),
        soc_mwh_avg=("soc_mwh", "mean"),
        soc_pct_avg=("soc_pct", "mean"),
        as_up_mw_avg=("as_up_mw", "mean"),
    )
    return h.round(3).reset_index()


def pct(x):
    return f"{x * 100:.0f}%"


def summarise(rows, rank, hou, hou_days, through, price_through):
    r = rows.merge(rank[["date", "rank", "evening_rank"]], on="date", how="left").sort_values("date")
    dz = r[r.in_dossier]
    low = r[r.soc_pct_at_spike < 0.35]
    ev = rank[rank.evening]
    lines = [
        "# Grid battery fleet at 2026 evening price spikes",
        "",
        "Sources: ERCOT 60-Day SCED Disclosure, ESR table (13052, NP3-965-ER) and 2026 real-time hub prices (13061) "
        "[Sourced]. Ratios and day selection [Modeled].",
        "",
        "**Which days.** The Friday supply dossier examined 4 large 2026 evening spike days: "
        + ", ".join(DOSSIER_DAYS)
        + ". They are not the 4 biggest 2026 spikes. Ranked by each day's highest 15-minute HB_HUBAVG price "
        f"(2026-01-01 to {price_through}, `day_ranking_2026.csv`), the biggest day was "
        f"{rank.date.iloc[0]} (${rank.max_price_hubavg.iloc[0]:,.0f}, HE{rank.hour_ending.iloc[0]}, a morning), "
        "and the top 5 evening days were "
        + ", ".join(f"{d} (${v:,.0f})" for d, v in zip(ev.date.head(5), ev.max_price_hubavg.head(5), strict=True))
        + f". Battery data (60-day SCED) only reaches {through}. This run covers every evening day (spike in HE17 "
        f"to HE24) at or above ${MIN_SPIKE:,.0f} with battery data, or the days given with --days.",
        "",
        "**Spike** = the first SCED run in the 15-minute interval with the day's highest HB_HUBAVG price. "
        "**Fleet SoC %** = total State of Charge / total Maximum SOC. **Fleet hours** = total Maximum SOC / the day's "
        "highest online HSL. **Reserve awards** = all upward ancillary service awards (Reg-Up, RRS, ECRS, Non-Spin).",
        "",
        "| Day | Dossier | Rank (all days / evening) | Spike, $/MWh | SCED time | Fleet SoC at spike, MWh (% of max) | "
        "Discharge at spike, MW (% of online HSL) | Reserve awards at spike, MW | Max discharge (time, MW) | "
        "Fleet peak SoC (time, MWh, % full) | Hours from peak SoC to spike | Fleet hours |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for _, x in r.iterrows():
        er = "" if pd.isna(x.evening_rank) else f" / {int(x.evening_rank)}"
        lines.append(
            f"| {x.date} | {'yes' if x.in_dossier else 'no'} | {int(x['rank'])}{er} | {x.spike_price_hubavg:,.0f} | "
            f"{x.spike_sced_time[:5]} | {x.soc_mwh_at_spike:,} ({pct(x.soc_pct_at_spike)}) | "
            f"{x.dis_mw_at_spike:,} ({pct(x.dis_pct_of_online_hsl)}) | {x.as_up_mw_at_spike:,} | "
            f"{x.max_dis_time[:5]}, {x.max_dis_mw:,} | {x.peak_soc_time[:5]}, {x.peak_soc_mwh:,}, "
            f"{pct(x.peak_soc_pct)} | {x.hours_peak_soc_to_spike:.1f} | {x.fleet_duration_h:.2f} |"
        )
    lines += ["", "## Headline checks", ""]
    if len(dz):
        dl = dz[dz.soc_pct_at_spike < 0.35]
        lines.append(
            f"- The dossier's 4 days: fleet SoC at the spike was {', '.join(pct(v) for v in dz.soc_pct_at_spike)} "
            f"of max SoC, so {len(dl)} of {len(dz)} were at {pct(dl.soc_pct_at_spike.min())} to "
            f"{pct(dl.soc_pct_at_spike.max())}. The fleet peaked {pct(dz.peak_soc_pct.min())} to "
            f"{pct(dz.peak_soc_pct.max())} full at {min(dz.peak_soc_time)[:5]} to {max(dz.peak_soc_time)[:5]}; the "
            f"spikes came {dz.hours_peak_soc_to_spike.min():.1f} to {dz.hours_peak_soc_to_spike.max():.1f} hours "
            f"later, while {dz.as_up_mw_at_spike.min() / 1000:.1f} to {dz.as_up_mw_at_spike.max() / 1000:.1f} GW of "
            f"reserve awards were held. Fleet energy: {dz.fleet_duration_h.min():.2f} to "
            f"{dz.fleet_duration_h.max():.2f} hours at full online output [Sourced 13052, 13061; Modeled ratios]."
        )
    high = r[r.soc_pct_at_spike >= 0.35]
    lines.append(
        f"- All {len(r)} days in this run: {len(low)} of {len(r)} had fleet SoC below 35% at the spike "
        f"({pct(low.soc_pct_at_spike.min())} to {pct(low.soc_pct_at_spike.max())}); the median was "
        f"{pct(r.soc_pct_at_spike.median())}. The exceptions: "
        + "; ".join(
            f"{x.date} at {pct(x.soc_pct_at_spike)} (spike {x.spike_sced_time[:5]}, discharging "
            f"{pct(x.dis_pct_of_online_hsl)} of online HSL, {x.as_up_mw_at_spike / 1000:.1f} GW of reserve awards)"
            for _, x in high.iterrows()
        )
        + "."
    )
    e = hou[hou.hour_ending.between(20, 23)].n_15min_over_300.sum()
    lines.append(
        f"- LZ_HOUSTON, 2026-01-01 to {price_through}: {hou.n_15min_over_300.sum()} 15-minute intervals above $300 on "
        f"{hou_days} days; {e} ({pct(e / hou.n_15min_over_300.sum())}) in HE20 to HE23 [Sourced 13061]."
    )
    lines += [
        "",
        "Caveats: fleet SoC % sums every ESR in the SCED run, online or not; each 5-minute SCED run takes its "
        "15-minute interval's price, so timing is good to about 20 seconds; hub price, not each battery's node.",
    ]
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--raw-dir", help="read already-downloaded 13052 and 13061 files under this folder")
    ap.add_argument("--days", nargs="*", help="operating days (YYYY-MM-DD); default: every evening spike day")
    ap.add_argument("--dossier-days", action="store_true", help="only the 4 days in the Friday supply dossier")
    ap.add_argument("--through", default=THROUGH, help=f"last operating day for day selection (default {THROUGH})")
    ap.add_argument("--price-through", default=PRICE_THROUGH, help=f"last day for price stats ({PRICE_THROUGH})")
    ap.add_argument("--out", default=OUT)
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    through, price_through = pd.Timestamp(a.through), pd.Timestamp(a.price_through)

    print("1/3 prices (ERCOT 13061, 2026)")
    p = load_prices(a.raw_dir)
    rank = rank_days(p, price_through, through)
    rank.to_csv(os.path.join(a.out, "day_ranking_2026.csv"), index=False)
    hou, hou_days = houston_over_300(p, price_through)
    hou.to_csv(os.path.join(a.out, "houston_over_300_by_hour_2026.csv"), index=False)
    if a.dossier_days:
        days = DOSSIER_DAYS
    elif a.days:
        days = a.days
    else:
        days = sorted(rank[rank.evening & rank.sced_available & (rank.max_price_hubavg >= MIN_SPIKE)].date)
    print(f"  days: {', '.join(days)}")

    print("2/3 battery data (ERCOT 13052, ESR table)")
    docs = None if a.raw_dir else list_docs(13052)
    rows, five = [], []
    for day in pd.to_datetime(days):
        g, n_esr = fleet(load_esr(day, a.raw_dir, docs), p, day)
        five.append(g)
        rows.append(day_row(g, n_esr))

    print("3/3 tables")
    rows = pd.DataFrame(rows)
    five = pd.concat(five, ignore_index=True)
    rows.to_csv(os.path.join(a.out, "spike_days.csv"), index=False)
    five.assign(ts=five.ts.dt.strftime("%Y-%m-%d %H:%M:%S")).round(3).to_csv(
        os.path.join(a.out, "fleet_5min.csv"), index=False
    )
    hourly(five).to_csv(os.path.join(a.out, "fleet_hourly_evening.csv"), index=False)
    s = summarise(rows, rank, hou, hou_days, a.through, a.price_through)
    with open(os.path.join(a.out, "summary.md"), "w", encoding="utf-8") as fh:
        fh.write(s + "\n")
    print("\n" + s)


if __name__ == "__main__":
    main()
