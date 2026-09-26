"""Perfect-foresight daily energy arbitrage for one Base Power home battery, per ERCOT load zone and day.

One command:  python arb.py      (after fetch.py; solves about 20,000 small LPs, 5 to 15 minutes)
Output:       data/arb_rt.pkl (15-min real-time, 13061) and data/arb_da.pkl (hourly day-ahead, 13060), per interval:
              net_kwh (discharge minus charge) and rev ($). years.py, h5.py and h7.py read these.

Battery [Assumed, per Base Power unit]: 39.2 kWh usable, 11 kW, 20% backup floor (7.84 kWh) never crossed,
88% round-trip efficiency applied on charge, at most 1 floor-to-full cycle per day (31.36 kWh discharged),
state of charge ends the day where it started (level chosen freely). No retail load, no ancillary services,
no degradation cost. Dollar values are upper bounds; shares and ratios are more robust than the levels.
"""

import argparse
import os

import numpy as np
import pandas as pd
from scipy.optimize import linprog
from scipy.sparse import lil_matrix

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("OPENGRID_PRICE_DATA", os.path.join(HERE, "data"))
RESULTS = os.path.join(HERE, "results")
CAP = 39.2  # kWh usable [Assumed]
FLOOR = 0.2 * CAP  # backup floor [Assumed]
KW = 11.0  # inverter power [Assumed]
RTE = 0.88  # round-trip efficiency [Assumed]
CYC = CAP - FLOOR  # max kWh discharged per day: 1 cycle [Assumed]
LZ = ["LZ_HOUSTON", "LZ_NORTH", "LZ_SOUTH", "LZ_WEST", "LZ_AEN", "LZ_CPS", "LZ_LCRA", "LZ_RAYBN"]


def solve_day(p, dt, block=None):
    """p: $/MWh per step, dt: hours per step, block: step indexes where the battery is unavailable.

    Returns net kWh per step (discharge minus charge) and revenue $ per step.
    Variables: charge c[0..T-1], discharge d[0..T-1], state of charge s[0..T-1] at the end of each step.
    """
    p = np.asarray(p, dtype=float)
    T = len(p)
    e = KW * dt
    n = 3 * T
    cost = np.concatenate([p / 1000.0, -p / 1000.0, np.zeros(T)])  # minimize p*c - p*d
    A = lil_matrix((T, n))
    for t in range(T):  # s_t - s_{t-1} - RTE*c_t + d_t = 0, cyclic (s_{-1} = s_{T-1})
        A[t, 2 * T + t] = 1
        A[t, 2 * T + (t - 1) % T] = -1
        A[t, t] = -RTE
        A[t, T + t] = 1
    a_ub = lil_matrix((1, n))
    a_ub[0, T : 2 * T] = 1  # total discharge <= 1 cycle
    ub_c = np.full(T, e)
    ub_d = np.full(T, e)
    if block is not None:
        ub_c[block] = 0
        ub_d[block] = 0
    bounds = [(0, x) for x in ub_c] + [(0, x) for x in ub_d] + [(FLOOR, CAP)] * T
    res = linprog(cost, A_ub=a_ub.tocsr(), b_ub=[CYC], A_eq=A.tocsr(), b_eq=np.zeros(T), bounds=bounds, method="highs")
    x = res.x
    net = x[T : 2 * T] - x[:T]
    return net, net * p / 1000.0


def run(prices, dt, key_cols, blockmask=None):
    """Solve every (key_cols) group, e.g. every zone-day. Returns the input rows plus net_kwh and rev."""
    out = []
    for _, g in prices.groupby(key_cols, sort=False):
        g = g.sort_values("pos")
        blk = None if blockmask is None else np.where(g[blockmask].values)[0]
        net, rev = solve_day(g.price.values, dt, blk)
        gg = g.copy()
        gg["net_kwh"] = net
        gg["rev"] = rev
        out.append(gg)
    return pd.concat(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.parse_args()
    rt = pd.read_pickle(os.path.join(DATA, "rt_tidy.pkl"))
    da = pd.read_pickle(os.path.join(DATA, "da_tidy.pkl"))
    rtl = rt[(rt.type == "LZ") & rt.sp.isin(LZ)]  # type LZ, not LZEW: the same zone name appears twice
    res = run(rtl[["sp", "date", "pos", "hour", "interval", "rep", "price"]], 0.25, ["sp", "date"])
    res.to_pickle(os.path.join(DATA, "arb_rt.pkl"))
    print("real-time done", len(res))
    dal = da[da.sp.isin(LZ)]
    resd = run(dal[["sp", "date", "pos", "hour", "rep", "price"]], 1.0, ["sp", "date"])
    resd.to_pickle(os.path.join(DATA, "arb_da.pkl"))
    print("day-ahead done", len(resd))


if __name__ == "__main__":
    main()
