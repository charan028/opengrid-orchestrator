"""Two-market LP prototype (09-optimizer-dispatcher-update.md, 2026-09-26). Standalone: highspy + numpy only.

Toy instance: 4 home banks + 1 substation asset x 96 x 15-min intervals x 3 scenarios (P10/P50/P90).
  B1, B2  home banks, Austin Energy territory (REG), no ERCOT access (K15 default)
  B3      home bank, Oncor / LZ_NORTH (FREE)
  B4      home bank, CenterPoint / LZ_HOUSTON (FREE)
  S1      substation BESS 20 MW, Austin Energy territory (REG), 2 h (default) or 4 h (--duration 4)

Lexicographic, two stages (spec section 2.4):
  stage R  max  regulated capacity value (the REG candidate q_o, fixed basis, 15:00-18:00 window)
  stage F  max  net value (ERCOT energy + Non-Spin + REG energy - charging - M1 delivery - wear)
           s.t. stage-R value >= (1 - eps) * optimum

Every number is a planning assumption (spec section 3 and addendum 08 section 3c), not a measured value.
Run:  python two_market_lp.py [--duration 2|4] [--ae-free-access] [--scale]
"""

from __future__ import annotations

import argparse
import math
import time
from dataclasses import dataclass, field

import highspy
import numpy as np

T = 96
DT = 0.25  # h
SCEN = (("P10", 0.25, 0.1), ("P50", 0.50, 0.4), ("P90", 0.25, 0.9))  # name, prob, cloud cover
HOURS = np.arange(T) * DT + DT / 2
DAYS_PER_YEAR = 300  # cycling days per year, as addendum section 3c
REG_WINDOW = range(60, 72)  # 15:00-18:00, Austin Energy on-peak
CAP_USD_PER_KW_YR = 75.0  # Power Partner-like planning value
REG_ENERGY_USD_PER_KWH = 0.1288  # addendum section 3c "energy value out" (Austin VoS Oct 2026): an assumption
SOLAR_USD_PER_KWH = 0.040  # utility solar PPA-like cost inside the AE contract (addendum section 3c)
SOLAR_FLOOR = 0.30
M_SOLAR = 0.50  # $/kWh shortfall below the 30 % solar floor (soft, priced, reported)
EPS_LEX = 0.001
H_NSPIN = 4.0  # h of energy per kW of Non-Spin held (NPRR1282)
TDSP = {"ONCOR": 0.060295, "CENTERPOINT": 0.064130}  # $/kWh, M1, full charge on grid charging


def solar_shape(h: np.ndarray) -> np.ndarray:
    return np.clip(np.sin(np.pi * (h - 7.0) / 12.0), 0.0, None) ** 1.2


def clear_factor(cloud: float) -> float:
    return 1.0 - 0.75 * cloud**3.4  # Kasten-Czeplak cloud attenuation


def ae_tou(h: float) -> float:
    """Austin Energy residential TOU pilot, power-supply component, weekday ($/kWh)."""
    if h >= 22 or h < 7:
        return 0.02677
    if 15 <= h < 18:
        return 0.08442
    return 0.04118


def ercot_price(h: np.ndarray, scen: str, zone: str) -> np.ndarray:
    """Synthetic duck curve, $/MWh: solar-depressed midday, evening-ramp scarcity."""
    ramp = {"P10": 45.0, "P50": 95.0, "P90": 650.0}[scen]
    mid = {"P10": -15.0, "P50": 0.0, "P90": 12.0}[scen]
    base = 30.0 + 8.0 * np.cos(2 * np.pi * (h - 3.0) / 24.0)
    p = base - (base - mid) * solar_shape(h) + ramp * np.exp(-(((h - 19.5) / 1.3) ** 2))
    return p * (1.05 if zone == "LZ_HOUSTON" else 1.0) + (2.0 if zone == "LZ_HOUSTON" else 0.0)


def nspin_mcpc(h: np.ndarray) -> np.ndarray:
    return np.where((h >= 17) & (h < 22), 25.0, 8.0)  # $/MW-h


def home_net_load_kw(h: np.ndarray, homes: int, pv_share: float) -> np.ndarray:
    """Per bank: diurnal load (ogsim household model) + midday AC, minus rooftop PV (7 kW on pv_share)."""
    bump = np.maximum(np.cos((h - 19.0) / 24.0 * 2 * np.pi), 0.0) ** 2
    load = (0.6 + 1.8 * bump + 1.0 * solar_shape(h)) * homes
    return load - homes * pv_share * 7.0 * solar_shape(h) * clear_factor(0.4)


def cell_temp_c(h: np.ndarray) -> np.ndarray:
    return 27.0 + 11.0 * np.sin(np.pi * (h - 9.0) / 12.0) + 3.0  # September ambient + garage offset


def derate_temp(tc: np.ndarray) -> np.ndarray:
    """Default discharge derating vs cell temperature (spec section 1.9, to confirm with Base)."""
    return np.interp(tc, [-10, 0, 10, 15, 35, 45, 50, 55], [0.0, 0.3, 0.8, 1.0, 1.0, 0.7, 0.4, 0.0])


@dataclass
class Asset:
    name: str
    kind: str  # HOME_BANK | SUBSTATION
    market: str  # REG_AE | FREE
    zone: str
    tdsp: str | None
    e_nom: float  # kWh nameplate
    p_cont: float  # kW continuous
    eta: float  # one-way
    c_deg: float  # $/kWh discharged (wear rule)
    capex: float  # $
    free_access: bool
    homes: int = 0
    pv_share: float = 0.0
    xfmr_kva: float = 0.0  # bank kVA (home) or POI (substation)
    export_kw_per_home: float = 10.0
    substation: str = ""
    floor: float = 0.20
    e0_frac: float = 0.50

    @property
    def e_use(self) -> float:
        return self.e_nom * (1.0 - self.floor)


def home_bank(name: str, market: str, zone: str, tdsp: str | None, sub: str) -> Asset:
    # 50 homes: 40 single (39.2 kWh / 11 kW, $7k) + 10 dual (78.4 kWh / 20 kW, $14k)
    return Asset(name, "HOME_BANK", market, zone, tdsp, 40 * 39.2 + 10 * 78.4, 40 * 11 + 10 * 20, 0.9487,
                 0.03, 40 * 7000 + 10 * 14000, market == "FREE", homes=50, pv_share=0.3, xfmr_kva=600.0,
                 substation=sub)


def substation_asset(name: str, duration_h: float, sub: str, capex_per_kw: float) -> Asset:
    return Asset(name, "SUBSTATION", "REG_AE", "LZ_AEN", None, 20_000 * duration_h, 20_000, math.sqrt(0.88),
                 0.015, capex_per_kw * 20_000, False, xfmr_kva=20_000, substation=sub)


@dataclass
class LP:
    lb: list[float] = field(default_factory=list)
    ub: list[float] = field(default_factory=list)
    cost: list[float] = field(default_factory=list)
    rows: list[tuple[list[int], list[float], float, float]] = field(default_factory=list)

    def var(self, lb: float = 0.0, ub: float = math.inf, cost: float = 0.0) -> int:
        self.lb.append(lb)
        self.ub.append(ub)
        self.cost.append(cost)
        return len(self.lb) - 1

    def row(self, coefs: dict[int, float], lb: float = -math.inf, ub: float = math.inf) -> int:
        self.rows.append((list(coefs), list(coefs.values()), lb, ub))
        return len(self.rows) - 1


def build(assets: list[Asset], ae_free_access: bool) -> tuple[LP, dict]:
    lp = LP()
    ix: dict = {"e": {}, "gs": {}, "gg": {}, "dF": {}, "yb": {}, "r": {}, "bal": {}, "ssol": {}}
    tc = cell_temp_c(HOURS)
    fT = derate_temp(tc)
    subs_load = {"SUB_AE": (18_000 + 10_000 * np.exp(-(((HOURS - 17.5) / 3.0) ** 2))) * 0.85}  # P10, kW
    q = lp.var(0.0, math.inf)  # REG capacity candidate q_o (kW, first stage)
    ix["q"] = q
    reg_assets = [a for a in assets if a.market == "REG_AE"]
    yrow: dict[int, dict[int, float]] = {t: {q: -1.0} for t in REG_WINDOW}
    for a in assets:
        free = a.market == "FREE" or ae_free_access
        for t in REG_WINDOW if a.market == "REG_AE" else ():
            ix["yb"][a.name, t] = v = lp.var()
            yrow[t][v] = 1.0
        for t in range(T):
            ix["r"][a.name, t] = lp.var(0.0, a.p_cont if free and a.kind == "HOME_BANK" else 0.0)
    for t, coefs in yrow.items():
        lp.row(coefs, 0.0, 0.0)  # C12/C24 shape: sum_a ybar = q (fixed basis)
    for s, (sn, pr, cloud) in enumerate(SCEN):
        for a in assets:
            free = a.market == "FREE" or ae_free_access
            e_prev = lp.var(a.e0_frac * a.e_nom - a.floor * a.e_nom, a.e0_frac * a.e_nom - a.floor * a.e_nom)
            ix["e"][a.name, 0, s] = e_prev
            lnet = home_net_load_kw(HOURS, a.homes, a.pv_share) if a.kind == "HOME_BANK" else np.zeros(T)
            if a.market == "REG_AE":
                sol_av = (a.p_cont * 0.6 if a.kind == "SUBSTATION" else a.p_cont * 0.5) * solar_shape(HOURS)
                sol_av = sol_av * clear_factor(cloud)
            else:  # rooftop PV surplus behind the meter
                pv = a.homes * a.pv_share * 7.0 * solar_shape(HOURS) * clear_factor(cloud)
                sol_av = np.minimum(pv, np.maximum(-home_net_load_kw(HOURS, a.homes, 0.0) + pv, 0.0))
            for t in range(T):
                e = lp.var(0.0, a.e_use)
                ix["e"][a.name, t + 1, s] = e
                # C7(b): no charging inside the window of a REG obligation the asset serves -- delivery is
                # measured NET at the meter/POI, so charge-while-delivering would be a wash trade
                no_ch = a.market == "REG_AE" and t in REG_WINDOW
                gs = ix["gs"][a.name, t, s] = lp.var(0.0, 0.0 if no_ch else float(sol_av[t]))
                gg = ix["gg"][a.name, t, s] = lp.var(0.0, 0.0 if no_ch else math.inf)
                dF = ix["dF"][a.name, t, s] = lp.var(0.0, math.inf if free else 0.0)
                yb = ix["yb"].get((a.name, t))
                r = ix["r"][a.name, t]
                disc = {dF: 1.0}
                if yb is not None:
                    disc[yb] = 1.0
                # C1 energy balance (dual = water value)
                bal = {e: 1.0, e_prev: -1.0, gs: -a.eta * DT, gg: -a.eta * DT}
                for v in disc:
                    bal[v] = bal.get(v, 0.0) + DT / a.eta
                ix["bal"][a.name, t, s] = lp.row(bal, -0.0005 * a.homes * DT, -0.0005 * a.homes * DT)
                # C5' continuous power, temperature derated (flow limit 1)
                p_t = a.p_cont * (float(fT[t]) if a.kind == "HOME_BANK" else 1.0)
                lp.row({**disc, r: 1.0}, ub=p_t)
                # C5'' SoC derating, concave piecewise-linear: P <= P(0.3 + 7 (e/E_nom)) near the 20 % floor
                lp.row({**disc, r: 1.0, e_prev: -7.0 * a.p_cont / a.e_nom}, ub=0.3 * a.p_cont)
                # charge limits and taper in the top 10 %
                lp.row({gs: 1.0, gg: 1.0}, ub=a.p_cont)
                lp.row({gs: 1.0, gg: 1.0, e_prev: a.p_cont / (0.1 * a.e_nom)}, ub=a.p_cont * a.e_use / (0.1 * a.e_nom))
                # C3 AS energy hold (Frank #6): e >= H_k r / eta_d at both ends of the interval
                lp.row({e_prev: 1.0, r: -H_NSPIN / a.eta}, lb=0.0)
                lp.row({e: 1.0, r: -H_NSPIN / a.eta}, lb=0.0)
                if a.kind == "HOME_BANK":
                    # flow limit 2: meter export (per-home limit aggregated) net of home load (P10 low load)
                    x_cap = a.homes * a.export_kw_per_home
                    lp.row({**disc, r: 1.0, gs: -1.0, gg: -1.0}, ub=float(0.8 * lnet[t]) + x_cap)
                    # flow limit 3: bank kVA both directions (kW ~ kVA)
                    lp.row({**disc, r: 1.0, gs: -1.0, gg: -1.0}, ub=a.xfmr_kva + float(0.8 * lnet[t]))
                    lp.row({gs: 1.0, gg: 1.0, **{v: -1.0 for v in disc}}, ub=a.xfmr_kva - float(1.2 * max(lnet[t], 0)))
                else:
                    # substation POI both directions (flow limit 3)
                    lp.row({**disc, r: 1.0, gs: -1.0, gg: -1.0}, ub=a.xfmr_kva)
                    lp.row({gs: 1.0, gg: 1.0, **{v: -1.0 for v in disc}}, ub=a.xfmr_kva)
                e_prev = e
            lp.row({e_prev: 1.0}, lb=a.e0_frac * a.e_nom - a.floor * a.e_nom)  # cyclic representative day
        # flow limit 3/4: no reverse flow through the AE substation transformer (energy stays in territory)
        for t in range(T):
            coefs: dict[int, float] = {}
            for a in assets:
                if a.substation != "SUB_AE":
                    continue
                for key in ("dF",):
                    coefs[ix[key][a.name, t, s]] = 1.0
                if (a.name, t) in ix["yb"]:
                    coefs[ix["yb"][a.name, t]] = 1.0
                coefs[ix["r"][a.name, t]] = 1.0
                coefs[ix["gs"][a.name, t, s]] = -1.0
                coefs[ix["gg"][a.name, t, s]] = -1.0
            if coefs:
                lp.row(coefs, ub=float(subs_load["SUB_AE"][t]))
        # 30 % solar floor per regulated territory per day (soft, priced)
        ss = ix["ssol"][s] = lp.var()
        coefs = {ss: 1.0}
        for a in reg_assets:
            for t in range(T):
                coefs[ix["gs"][a.name, t, s]] = coefs.get(ix["gs"][a.name, t, s], 0.0) + (1 - SOLAR_FLOOR)
                coefs[ix["gg"][a.name, t, s]] = -SOLAR_FLOOR
        lp.row(coefs, lb=0.0)
    return lp, ix


def objective_vectors(lp: LP, ix: dict, assets: list[Asset]) -> tuple[np.ndarray, np.ndarray]:
    n = len(lp.lb)
    c_r = np.zeros(n)
    c_r[ix["q"]] = CAP_USD_PER_KW_YR / 365.0  # stage R: regulated capacity value (per day)
    c_f = c_r.copy()
    mc = nspin_mcpc(HOURS)
    for a in assets:
        for t in range(T):
            c_f[ix["r"][a.name, t]] += mc[t] / 1000 * DT
            if (a.name, t) in ix["yb"]:
                c_f[ix["yb"][a.name, t]] += (REG_ENERGY_USD_PER_KWH - a.c_deg) * DT
        for s, (sn, pr, _cl) in enumerate(SCEN):
            lam = ercot_price(HOURS, sn, a.zone) / 1000.0
            for t in range(T):
                c_f[ix["dF"][a.name, t, s]] += pr * (lam[t] - a.c_deg) * DT
                if a.market == "REG_AE":
                    c_f[ix["gg"][a.name, t, s]] -= pr * ae_tou(HOURS[t]) * DT  # no TDSP (K15 tariff rule)
                    c_f[ix["gs"][a.name, t, s]] -= pr * SOLAR_USD_PER_KWH * DT
                else:
                    c_f[ix["gg"][a.name, t, s]] -= pr * (lam[t] + TDSP[a.tdsp]) * DT  # M1 on grid kWh
                    c_f[ix["gs"][a.name, t, s]] -= pr * lam[t] * DT  # PV surplus: forgone buyback, no M1
            c_f[ix["ssol"][s]] -= pr * M_SOLAR
    return c_r, c_f


def to_highs(lp: LP) -> highspy.Highs:
    h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    n = len(lp.lb)
    h.addVars(n, np.array(lp.lb), np.array(lp.ub))
    starts, idx, val, lo, up = [], [], [], [], []
    for cols, coefs, lb, ub in lp.rows:
        starts.append(len(idx))
        idx.extend(cols)
        val.extend(coefs)
        lo.append(lb)
        up.append(ub)
    h.addRows(len(lo), np.array(lo), np.array(up), len(idx), np.array(starts, dtype=np.int32),
              np.array(idx, dtype=np.int32), np.array(val))
    h.changeObjectiveSense(highspy.ObjSense.kMaximize)
    return h


def solve_lexicographic(lp: LP, ix: dict, assets: list[Asset]) -> tuple[highspy.Highs, dict]:
    c_r, c_f = objective_vectors(lp, ix, assets)
    t0 = time.perf_counter()
    h = to_highs(lp)
    n = len(lp.lb)
    t_build = time.perf_counter() - t0
    h.changeColsCost(n, np.arange(n, dtype=np.int32), c_r)
    h.run()
    z_r = h.getInfo().objective_function_value
    t1 = time.perf_counter()
    nz = np.nonzero(c_r)[0]
    h.addRow((1 - EPS_LEX) * z_r, math.inf, len(nz), nz.astype(np.int32), c_r[nz])  # fix stage R within eps
    h.changeColsCost(n, np.arange(n, dtype=np.int32), c_f)
    h.run()
    t2 = time.perf_counter()
    stats = {"vars": n, "rows": len(lp.rows) + 1, "build_s": t_build, "stageR_s": t1 - t0 - t_build,
             "stageF_s": t2 - t1, "status": h.modelStatusToString(h.getModelStatus()), "z_r": z_r}
    return h, stats


def economics(h: highspy.Highs, ix: dict, assets: list[Asset]) -> None:
    x = np.array(h.getSolution().col_value)
    q = x[ix["q"]]
    mc = nspin_mcpc(HOURS)
    rows = {}
    for a in assets:
        cap_rev = sum(x[ix["yb"][a.name, t]] for t in REG_WINDOW if (a.name, t) in ix["yb"])
        cap_rev_yr = CAP_USD_PER_KW_YR * cap_rev / len(REG_WINDOW)  # share of committed kW
        day = {"in": 0.0, "m1": 0.0, "reg_e": 0.0, "free_e": 0.0, "as": 0.0, "wear": 0.0, "kwh_out": 0.0,
               "kwh_sol": 0.0, "kwh_grid": 0.0}
        for t in range(T):
            day["as"] += x[ix["r"][a.name, t]] * mc[t] / 1000 * DT
            if (a.name, t) in ix["yb"]:
                yb = x[ix["yb"][a.name, t]]
                day["reg_e"] += yb * REG_ENERGY_USD_PER_KWH * DT
                day["wear"] += yb * a.c_deg * DT
                day["kwh_out"] += yb * DT
        for s, (sn, pr, _cl) in enumerate(SCEN):
            lam = ercot_price(HOURS, sn, a.zone) / 1000.0
            for t in range(T):
                dF, gs, gg = (x[ix[k][a.name, t, s]] for k in ("dF", "gs", "gg"))
                day["free_e"] += pr * dF * lam[t] * DT
                day["wear"] += pr * dF * a.c_deg * DT
                day["kwh_out"] += pr * dF * DT
                day["kwh_sol"] += pr * gs * DT
                day["kwh_grid"] += pr * gg * DT
                if a.market == "REG_AE":
                    day["in"] += pr * (gg * ae_tou(HOURS[t]) + gs * SOLAR_USD_PER_KWH) * DT
                else:
                    day["in"] += pr * (gg * lam[t] + gs * lam[t]) * DT
                    day["m1"] += pr * gg * TDSP[a.tdsp] * DT
        kw = a.p_cont
        yr_in = (day["in"] + day["m1"]) * DAYS_PER_YEAR
        yr_out = cap_rev_yr + (day["reg_e"] + day["free_e"] + day["as"]) * DAYS_PER_YEAR
        yr_wear = day["wear"] * DAYS_PER_YEAR
        om = 0.01 * a.capex  # fixed O&M, 1 % of capex (assumption)
        net = yr_out - yr_in - yr_wear - om
        sol_share = day["kwh_sol"] / max(day["kwh_sol"] + day["kwh_grid"], 1e-9)
        rows[a.name] = (a, kw, yr_in, yr_out, yr_wear, om, net, cap_rev, sol_share, day["kwh_out"])
    print(f"\nREG capacity selected (stage R): q = {q:,.0f} kW over 15:00-18:00 (fixed basis)")
    hdr = f"{'asset':6} {'mkt':6} {'kW':>7} {'$/kW-in':>8} {'$/kW-out':>9} {'wear':>6} {'O&M':>5} {'net':>7} " \
          f"{'capex/kW':>8} {'payback':>7} {'solar%':>6} {'kWh out/d':>9}"
    print(hdr)
    tot: dict[str, list[float]] = {}
    for name, (a, kw, yi, yo, yw, om, net, _c, ss, ko) in rows.items():
        pb = (a.capex / kw) / (net / kw) if net > 0 else math.inf
        print(f"{name:6} {a.market:6} {kw:7,.0f} {yi / kw:8.1f} {yo / kw:9.1f} {yw / kw:6.1f} {om / kw:5.1f} "
              f"{net / kw:7.1f} {a.capex / kw:8.0f} {pb:7.1f} {100 * ss:6.1f} {ko:9,.0f}")
        for key in (a.market, "FLEET"):
            acc = tot.setdefault(key, [0.0] * 6)
            for i, v in enumerate((kw, yi, yo, yw, om, net)):
                acc[i] += v
    print("-- per market / fleet ($/kW-yr of installed kW) --")
    for key, acc in tot.items():
        kw, yi, yo, yw, om, net = acc[:6]
        capex = sum(r[0].capex for r in rows.values() if key == "FLEET" or r[0].market == key)
        pb = capex / net if net > 0 else math.inf
        print(f"{key:13} kW {kw:8,.0f}  in {yi / kw:7.1f}  out {yo / kw:7.1f}  wear {yw / kw:5.1f}  "
              f"O&M {om / kw:5.1f}  net {net / kw:7.1f}  capex/kW {capex / kw:6.0f}  payback {pb:5.1f} y")


def water_values(h: highspy.Highs, ix: dict, name: str) -> None:
    dual = np.array(h.getSolution().row_dual)
    wv = [sum(abs(dual[ix["bal"][name, t, s]]) for s in range(3)) * 1000 for t in range(T)]
    pick = {"03:00": 12, "12:00": 48, "16:00": 64, "19:30": 78, "23:00": 92}
    print(f"water value nu (sum over scenarios, $/MWh DC) for {name}: "
          + ", ".join(f"{k} {wv[t]:.0f}" for k, t in pick.items()))


def toy(duration: float, ae_free_access: bool) -> None:
    capex_sub = 1000.0 if duration == 2 else 1500.0  # $/kW, assumption (Brattle BESS $928/kW, 2 h)
    assets = [home_bank("B1", "REG_AE", "LZ_AEN", None, "SUB_AE"), home_bank("B2", "REG_AE", "LZ_AEN", None, "SUB_AE"),
              home_bank("B3", "FREE", "LZ_NORTH", "ONCOR", "SUB_N"),
              home_bank("B4", "FREE", "LZ_HOUSTON", "CENTERPOINT", "SUB_H"),
              substation_asset("S1", duration, "SUB_AE", capex_sub)]
    lp, ix = build(assets, ae_free_access)
    h, st = solve_lexicographic(lp, ix, assets)
    print(f"toy: {st['vars']:,} vars, {st['rows']:,} rows, status {st['status']}, build {st['build_s']:.2f}s, "
          f"stage R {st['stageR_s']:.2f}s, stage F {st['stageF_s']:.2f}s; S1 duration {duration:g} h; "
          f"AE ERCOT access {ae_free_access}")
    economics(h, ix, assets)
    water_values(h, ix, "B3")
    water_values(h, ix, "S1")


def scale() -> None:
    zones = [("LZ_NORTH", "ONCOR"), ("LZ_HOUSTON", "CENTERPOINT"), ("LZ_NORTH", "ONCOR"), ("LZ_HOUSTON", "CENTERPOINT")]
    assets = [home_bank(f"A{i:02d}", "REG_AE", "LZ_AEN", None, "SUB_AE") for i in range(10)]
    assets += [home_bank(f"F{i:02d}", "FREE", *zones[i % 4], f"SUB_{i % 4}") for i in range(30)]
    assets += [substation_asset(f"S{i}", 2.0, "SUB_AE" if i == 0 else f"SUB_X{i}", 1000.0) for i in range(4)]
    for a in assets[-3:]:
        a.market, a.free_access = "REG_AE", False
    lp, ix = build(assets, False)
    _h, st = solve_lexicographic(lp, ix, assets)
    print(f"scale: 40 banks + 4 substation assets x 96 x 3: {st['vars']:,} vars, {st['rows']:,} rows, "
          f"{st['status']}; build {st['build_s']:.1f}s, stage R {st['stageR_s']:.1f}s, stage F {st['stageF_s']:.1f}s")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=float, default=2.0)
    ap.add_argument("--ae-free-access", action="store_true")
    ap.add_argument("--scale", action="store_true")
    args = ap.parse_args()
    if args.scale:
        scale()
    else:
        toy(args.duration, args.ae_free_access)
