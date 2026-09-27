"""Capture rate by battery duration (hours of storage), summer 2025 and summer 2026.

One command:  python by_duration.py          (after run.py and pre_rtcb.py; no download)
Reads:        output/capture_by_battery_day.csv, output/summer2025_capture_by_battery_day.csv.gz
Output:       output/capture_by_duration.csv

Duration = MWh / MW for each battery-day (2026: ERCOT's own SOC range; 2025: assumed, see pre_rtcb.py).
Fleet capture = total actual $ / total perfect-foresight $ within the band; median = the median of each battery's
own ratio over its battery-days in the band.
"""

import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "output")
FILES = {"summer 2025": "summer2025_capture_by_battery_day.csv.gz", "summer 2026": "capture_by_battery_day.csv"}
BANDS = [(0.0, 1.2, "under 1.2 h"), (1.2, 1.8, "1.2 to 1.8 h"), (1.8, 99.0, "1.8 h or more")]


def main():
    rows = []
    for season, f in FILES.items():
        r = pd.read_csv(os.path.join(OUT, f))
        r["hours"] = r.mwh / r.mw
        for lo, hi, label in BANDS:
            s = r[(r.hours >= lo) & (r.hours < hi)]
            pb = s.groupby("battery").agg(a=("actual_usd", "sum"), p=("perfect_usd", "sum"))
            pb = pb[pb.p > 0]
            rows.append(
                dict(
                    season=season,
                    duration=label,
                    battery_days=len(s),
                    batteries=s.battery.nunique(),
                    fleet_capture=round(s.actual_usd.sum() / s.perfect_usd.sum(), 3),
                    median_battery_capture=round((pb.a / pb.p).median(), 3),
                )
            )
    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(OUT, "capture_by_duration.csv"), index=False)
    print(out.to_string(index=False))


if __name__ == "__main__":
    main()
