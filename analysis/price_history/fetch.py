"""Download ERCOT historical load-zone and hub prices and tidy them into 2 local pickles.

One command:  python fetch.py            (years 2021 to 2026, about 15 MB of zips per year, 10 to 20 minutes)
Needs:        pip install -r ../requirements.txt. No ERCOT login or API key.
Data:         ERCOT public MIS, listed at https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=<id>
  13061  Historical RTM Load Zone and Hub Prices (15-min, 1 zipped xlsx per year, 1 sheet per month)
  13060  Historical DAM Load Zone and Hub Prices (hourly, 1 zipped xlsx per year)
Output:       data/rt_tidy.pkl and data/da_tidy.pkl (git-ignored). Every other script in this folder reads these.

Tidy format: one row per settlement point x interval, with `pos` = position of the interval within its day
(0 to 95 on normal days, 91 on the spring DST day, 99 on the fall DST day, where the repeated hour is flagged "Y").
"""

import argparse
import io
import json
import os
import time
import urllib.request
import zipfile

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("OPENGRID_PRICE_DATA", os.path.join(HERE, "data"))
UA = {"User-Agent": "opengrid-analysis-price-history (research; low volume)"}
LIST_URL = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId={}"
DOWNLOAD_URL = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={}"
RT_COLS = ["date", "hour", "interval", "rep", "sp", "type", "price"]
DA_COLS = ["date", "he", "rep", "sp", "price"]


def get(url):
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            return urllib.request.urlopen(req, timeout=600).read()
        except Exception:
            if attempt == 3:
                raise
            time.sleep(5)


def year_doc(report_id, year):
    j = json.loads(get(LIST_URL.format(report_id)))
    docs = [d["Document"] for d in j["ListDocsByRptTypeRes"]["DocumentList"]]
    return next(d for d in docs if d["FriendlyName"].endswith(f"_{year}"))


def read_year(report_id, year, xlsx_dir=None):
    """All monthly sheets of one yearly file, as strings. Uses a local copy of the xlsx if one is given."""
    if xlsx_dir:
        tag = "RTMLZHBSPP" if report_id == 13061 else "DAMLZHBSPP"
        path = next(
            os.path.join(root, f)
            for root, _, files in os.walk(xlsx_dir)
            for f in files
            if f.endswith(f"{tag}_{year}.xlsx")
        )
        src = path
    else:
        doc = year_doc(report_id, year)
        print(f"  downloading {doc['ConstructedName']} ...", flush=True)
        z = zipfile.ZipFile(io.BytesIO(get(DOWNLOAD_URL.format(doc["DocID"]))))
        src = io.BytesIO(z.read(z.namelist()[0]))
    sheets = pd.read_excel(src, sheet_name=None, dtype=str)
    df = pd.concat([s for s in sheets.values() if len(s)], ignore_index=True)
    # Some yearly files carry a stray metadata row (e.g. the 2021 real-time file, sheet Oct, holds a publish
    # timestamp with no hour and no price). Keep only rows with an hour and a price.
    n0 = len(df)
    df = df.dropna(subset=[df.columns[1], df.columns[-1]])
    if len(df) < n0:
        print(f"  {report_id} {year}: dropped {n0 - len(df)} row(s) with no hour or price", flush=True)
    col = df.columns[0]
    df[col] = pd.to_datetime(df[col], format="%m/%d/%Y")
    return df


def tidy_rt(r):
    r.columns = RT_COLS
    r["price"] = r.price.astype(float)
    r["date"] = pd.to_datetime(r.date)
    r["hour"] = r.hour.astype(int)
    r["interval"] = r.interval.astype(int)
    r["repn"] = (r.rep == "Y").astype(int)  # order within a day: hour, then repeated hour (DST), then interval
    r = r.sort_values(["sp", "type", "date", "hour", "repn", "interval"])
    r["pos"] = r.groupby(["sp", "type", "date"]).cumcount()
    return r.reset_index(drop=True)


def tidy_da(d):
    d.columns = DA_COLS
    d["price"] = d.price.astype(float)
    d["date"] = pd.to_datetime(d.date)
    d["hour"] = d.he.str[:2].astype(int)
    d["repn"] = (d.rep == "Y").astype(int)
    d = d.sort_values(["sp", "date", "hour", "repn"])
    d["pos"] = d.groupby(["sp", "date"]).cumcount()
    return d.reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--years", type=int, nargs="*", default=list(range(2021, 2027)))
    ap.add_argument("--xlsx-dir", help="use already-downloaded 13061/13060 xlsx files under this folder")
    a = ap.parse_args()
    os.makedirs(DATA, exist_ok=True)
    rt, da = [], []
    for y in a.years:
        print(f"{y}: real-time (13061)")
        rt.append(read_year(13061, y, a.xlsx_dir))
        print(f"{y}: day-ahead (13060)")
        da.append(read_year(13060, y, a.xlsx_dir))
    r = tidy_rt(pd.concat(rt, ignore_index=True))
    d = tidy_da(pd.concat(da, ignore_index=True))
    n = r.groupby(["sp", "type", "date"]).size()
    odd = n[~n.isin([92, 96, 100])]  # 92 and 100 are the DST days
    if len(odd):
        print(f"  warning: {len(odd)} point-days without 92, 96 or 100 intervals, e.g. {odd.head(3).to_dict()}")
    r.to_pickle(os.path.join(DATA, "rt_tidy.pkl"))
    d.to_pickle(os.path.join(DATA, "da_tidy.pkl"))
    print(f"real-time rows {len(r):,} ({r.date.min().date()} to {r.date.max().date()}); day-ahead rows {len(d):,}")


if __name__ == "__main__":
    main()
