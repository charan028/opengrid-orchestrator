# 09 — Optimizer and dispatcher update for the two-market model

Date: 2026-09-26 · Status: **design and scoping spec for owner review**. No production code is changed by this
document. A standalone prototype backs the formulation: `prototypes/two_market_lp.py` (§9).

Scope: the **selector** (DA/ID MILP and gate selection, `orchestrator/src/opengrid/selector/`) and the
**dispatcher** (allocator: SCED LP, RT lexicographic LP and water-fill, PI loops, `allocator/`), plus the guardian
checks, data, settlement and invariants they touch. It folds in:

- the two-market direction (`08-market-model-two-markets.md`);
- the K13 additions of 2026-09-26 (`00-invariants.md`);
- Frank's items #5 (per-bank zone pricing), #6 (AS energy holds) and #7 (a consistent wear rule);
- the owner's requirement on **maximum discharge-flow limits** at every level, each enforced independently by the
  guardian (§1.9, §2.6).

Read first: `06-reviews/06-first-principles-review.md` (P1–P8, §5.1 canonical LP, C24),
`02-architecture/03-decision-engine.md` §6 (Mode O, C1–C23), `02a-mvp-s-spec-engine.md` §3, §5 and §7.4, and
`06-service-profiles-and-power-quality.md` (K14, DATA_CENTER).

---

## 0. Decisions and findings at a glance

### 0.1 Decisions proposed by this spec

| # | Topic | Decision | Principle |
|---|---|---|---|
| D1 | Market dimension | Each obligation carries $m(o)\in\{\mathrm{REG}(u),\mathrm{FREE}\}$. Each asset carries a territory $u(a)$ (a regulated utility, or the ERCOT competitive area) | P2, P3 |
| D2 | Territory | This is new invariant **K15**, with four parts. (a) A REG(u) obligation uses only assets inside $u$. (b) An asset inside $u$ takes FREE opportunities only if the utility contract grants wholesale access (default **no**). (c) There is no net reverse flow across $u$'s boundary substations. (d) Charging is priced under the asset's own territory tariff | P2 |
| D3 | Priority REG vs FREE | **Lexicographic, two stages** at every selector gate: stage R (commitments, then new regulated capacity), then stage F (net value on the residual). There is no premium weight | P4, P5, P7; review §5.4-1 |
| D4 | Charging sources | $g=g^{sol}+g^{grid}$. The solar share is at least 30% per regulated contract and accounting period. It is a **soft floor** (priced slack, reported), limited by the forecast solar availability | P7 |
| D5 | Delivery charge (M1) | M1 applies **only** to kWh drawn from the grid in the ERCOT competitive area (TDSP, flat per kWh, `tdsp_tariffs.toml`). It does not apply in AE/CPS territory, where the utility's own charging terms carry every adder, or to behind-the-meter PV surplus | P7 |
| D6 | AS energy hold (Frank #6) | While an award is held, keep $e\ge\sum_kH_kr/\eta_d$ above the 20% floor: $H_{NSPIN}=4$ h and $H_{ECRS}=1$ h (NPRR1282, 2025-12-05). This goes into the ONE floor C3, is enforced by the selector and the engine (S6 hold floor), and is measured by the invariants checker (`CHECK_AS_HOLD`). It is **not** a guardian check | P1, P4 |
| D7 | Evening ramp | There is **no separate hard hold**. The value emerges from duck-curve scenarios. The plan publishes a water value $\nu_{a,t}$ and a hold floor. RT spends headroom energy below the plan only when $\lambda^{RT}\ge\nu+$ hysteresis. This replaces the fixed $30/MWh threshold | P7 (no rule duplicating a price) |
| D8 | Wear (Frank #7) | **Wear is charged on AC kWh actually discharged, at the asset-class rate, for every purpose.** It is never charged on capacity held, on reservations, or on charging. One function (`core.economics.wear_cost`) is used by both the selector and settle | P1, P7, P8 |
| D9 | Commitment basis | FIXED: $y=\bar y$ (schedule). NEED: $\bar y$ is a reserved maximum. It is locked in power, and its energy hold is $\bar y\,h^{need}$. Delivery $y\le\bar y$ follows the need scenario | P4 |
| D10 | Best effort after a shortfall | The target stays $Q$ and is never reduced. The shortfall slack is priced at $\beta_o$ from the first short interval. Recovery charging for the obligation's assets is allowed in otherwise-barred windows | P4 (K13 addition) |
| D11 | Substation assets | A new asset class `SUBSTATION_BESS` has its own SoC, PCS rating, POI and transformer limits, ramp and wear. The default is **20 MW / 2 h (40 MWh)**, RTE 0.88 and a 20% floor, with 4 h as a sensitivity | P1 |
| D12 | Flow limits | Seven families (F1–F7, §1.9) at the selector, SCED, RT and PI layers, each re-checked by the guardian on its own reads (G-02 changed, G-26…G-32 new; G-33 territory), except F7 (the energy hold), which the selector and engine enforce and the invariants checker measures. K4 is extended | P1, P2 |
| D13 | No wash trades | There is no charging inside the delivery window of a REG obligation the asset serves (C7(b) generalised). Delivery is measured net at the meter or POI (prototype finding, §9) | P7, P8 |

### 0.2 Findings in the code as built (relevant gaps; file:line)

| # | Finding | Where | Consequence |
|---|---|---|---|
| G1 | **Frank #5, selector.** `load_scenarios` ignores `ScenarioPoint.series_key`/`kind`. Every (scenario, t) keeps the last row appended. Rows are ordered by `series_key, kind, interval` (`forecast/pg_backend.py:33`), so every bank is priced at the **LZ_WEST price**, and load rows are also folded into the same dict | `selector/gate.py:257-273` | Wrong $v^E$ for 3 of 4 zones. It must be fixed before any per-zone economics |
| G2 | **Frank #5, RT.** One latest price row (any series) is applied to every bank | `engine/gateways.py:238-247` | Same issue at RT |
| G3 | **Frank #7.** The selector charges $c_{deg}$ on MARKET candidates' $\bar y$ and on committed $\bar y$. It does not charge the spot headroom $h$, nor FIRM/AS candidates. Settle charges it on every obligation's delivered kWh. Arbitrage wear is therefore free in the selector | `selector/model.py:262-281`; `settle/profitability.py:49` | The selector over-cycles and the P&L disagrees with the plan |
| G4 | Settle energy cost = wholesale price **at the discharge interval** / $\eta_d$. That is not what was paid to charge, and it has no M1 or regulated tariff | `settle/profitability.py:48` | It overstates the cost of evening deliveries and ignores M1. §4 replaces it with a stored-energy average cost |
| G5 | There is no $w_b$ (M1) in the objective, and charging is priced at the same scenario price | `selector/model.py:276-281` | FREE charging is under-costed by about $60/MWh (prototype: the water value for an Oncor bank is ~$82/MWh at night, not ~$35) |
| G6 | There are no AS variables $r$ and no C3 hold in the model. AS candidates are plain $\bar y$ | `selector/model.py` (no `r`) | Frank #6: a Non-Spin award can be sold without 4 h of energy |
| G7 | Every bank is eligible for every obligation, so there is no locality or territory | `selector/gate.py:299, 337` | K15 cannot hold. DIST_DEFERRAL locality (FR-DE-034) is not enforced either |
| G8 | The capability is one live-now value copied to all 96 intervals. There is no temperature or SoC derating over the horizon | `selector/gate.py:238-240` | The afternoon heat derating is invisible to the plan |
| G9 | The RT headroom threshold is a fixed **$30/MWh**, not the water value | `allocator/cycle.py:45`, `allocator/price_response.py:19-51` | With M1, discharging at $30 destroys ~$50–60/MWh of replacement value (§9) |
| G10 | `fleet.capability` treats missing bank SCADA as 0 kVA load, which is optimistic for charge headroom. G-03 fails closed at `guardian/checks.py:75-80`, so it is safe but produces vetoes | `fleet/__init__.py:503-506` | Planning optimism leads to avoidable vetoes |
| G11 | Hub telemetry has no meter net power, PV, cell temperature, BMS limits or peak budget. The sim computes the home net load but does not publish it | `integration-sims/src/ogsim/fleet/runtime.py:340-367`, `household.py:39-56` | F1, F2 and F5 cannot be enforced or checked yet |

---

## 1. Updated canonical formulation (Mode O, two markets)

Notation follows `03` §6.2–6.6 and `02a` §3.2–3.4. The home **banks** $b$ are generalised to **assets** $a$ so that
substation assets fit without a second model family. Power is in kW and energy in kWh; prices are in $/MWh
($/1000 → $/kWh) unless marked $/kWh. $\Delta_t$ is in hours.

### 1.1 Sets and indices

| Symbol | Set | Notes |
|---|---|---|
| $t\in\mathcal T$ | 15-min intervals, 96 per 24 h (hourly after 6 h optional, §5) | as `02a` §3.2 |
| $\omega\in\Omega$ | {P10, P50, P90}, $\pi=(0.25,0.5,0.25)$ | Scenarios are **per load zone and jointly** price × solar × load (duck curve: high-price scenario ↔ cloudy) |
| $a\in\mathcal A=\mathcal B\cup\mathcal S$ | Assets: home banks $\mathcal B$ (aggregated partitions, never hubs) and substation assets $\mathcal S$ | new: $\mathcal S$ |
| $u\in\mathcal U$ | Regulated utilities {AE, CPS}; $\mathcal A^u$ = assets in $u$'s territory; $\mathcal A^{C}$ = assets in the ERCOT competitive area | new |
| $m\in\mathcal M=\{\mathrm{REG}(u):u\in\mathcal U\}\cup\{\mathrm{FREE}\}$ | Markets | new |
| $o\in\mathcal O=\mathcal O^c\cup\mathcal O^{new}$ | Obligations; $m(o)$ market, $\beta(o)\in\{\mathrm{FIXED},\mathrm{NEED}\}$ basis, $W_o$ window, $\mathcal A_o$ eligible assets | $m$, $\beta$ new |
| $k\in\{\mathrm{NSPIN},\mathrm{ECRS}\}$ | AS products | `02a` §3.2 |
| $z(a)$, $\tau(a)$, $f(a)$, $\sigma(a)$ | ERCOT load zone, TDSP, feeder, substation of $a$ | $z$ includes **LZ_AEN, LZ_CPS** for NOIE sites |
| $x\in\mathcal X_a$ | Service transformers under bank $a$ (RT and guardian level; the selector sees only their aggregate) | new |
| $\mathcal F$, $\Sigma$, $\Sigma^u\subseteq\Sigma$ | Feeders; substations; the boundary substations of territory $u$ | new |

### 1.2 Parameters (new or changed)

| Symbol | Meaning | Default (label) | Source |
|---|---|---|---|
| $E^{nom}_a$, $\phi^{fl}_a$ | Nameplate kWh; discharge floor fraction | homes 39.2/78.4 kWh per hub; **floor 20% (confirmed by Base)**; substation 40 MWh (2 h, assumption), floor 20% (assumption, OQ-2) | fleet, asset registry |
| $E^{use}_a=(1-\phi^{fl}_a)E^{nom}_a$ | Energy above floor | derived | — |
| $P^{cont}_a$, $P^{pk}_a$, $\tau^{pk}_a$ | Continuous and peak power; peak duration | homes 11/20 kW per hub, **peak = continuous** until Base confirms (OQ-10); substation 20 MW | Base |
| $f^{dis}_{SoC}$, $f^{dis}_T$, $f^{ch}_{SoC}$, $f^{ch}_T$ | Derating curves (§1.9 F1) | piecewise-linear defaults, **to confirm with Base** | Base / BMS datasheet |
| $\hat T_{a,t}$ | Forecast cell temperature | NWS hourly ambient + garage offset (3 °C, calibrate) | `feeds/nws.py` |
| $\eta^c_a,\eta^d_a$ | One-way efficiencies | homes 0.9487; substation $\sqrt{0.88}=0.938$ | `core/physics.py:15-16` |
| $c^{deg}_a$ | Wear rate per AC kWh discharged (D8) | homes $0.03 (A-DE-16); substation $0.015 (assumption, OQ-15) | Base warranty curve |
| $\lambda_{z,t,\omega}$ | ERCOT load-zone price **per bank's zone** (Frank #5) | forecast per `series_key` | NP6-905-CD / NP4-190-CD |
| $\mu_{k,t}$ | AS MCPC | forecast | NP4-188-CD |
| $H_k$ | Energy per kW of AS held | NSPIN 4 h, ECRS 1 h | NPRR1282 (`03` §6.3) |
| $w_{\tau}$ | M1 TDSP delivery charge, $/kWh on grid-drawn kWh | Oncor 0.060295, CNP 0.064130, AEP-C 0.058, AEP-N 0.057, TNMP 0.074022 | `config/tdsp_tariffs.toml` |
| $c^{u,grid}_t$ | Regulated charging price (contract terms) | AE: TOU pilot 2.677/4.118/8.442 ¢ + adder $\alpha^u$ (0 per §3c; 1.28–1.5 ¢ all-in sensitivity) | contract; `regulated-utilities-austin-cps-2026-09.md` §2.2 |
| $c^{sol}_{a,t}$ | Solar-sourced charging cost | REG utility solar 4.0 ¢ (§3c); FREE rooftop PV surplus = forgone export credit (≈ $\lambda$); AE rooftop = VoS 12.88 ¢ opportunity cost | contract; REP; AE VoS |
| $S^{sol}_{a,t,\omega}$ | Solar availability for charging (kW) | clear-sky × $(1-0.75C^{3.4})$ (Kasten–Czeplak) with NWS sky cover, scaled to ERCOT PV forecast | §3 |
| $\phi^{sol}_c$ | Solar share floor per REG contract | 0.30, soft, slack price $M^{sol}$ = the contract's green premium, default $0.50/kWh | addendum §3c |
| $V^{cap}_o$ | REG capacity price | $75/kW-yr (planning), pro-rated to the horizon | contract |
| $v^{E}_{o,t}$ | Energy price paid by the counterparty for delivered kWh | contract. The prototype uses 12.88 ¢ to reproduce §3c (an assumption) | contract |
| $\varphi^{acc}_u$ | Wholesale (ERCOT) access for assets in $u$ | **0** | contract (OQ-5) |
| $h^{need}_o$ | Sustain duration of a need-basis reservation | `service_profile.sustain_duration_s` | `06` §1.3 |
| $X^{exp}_i$, $S^{svc}_i$ | Per-home meter export limit; service import rating | Base install records; defaults min($P^{cont}$, 10 kW), 38.4 kW (OQ-8) | interconnection agreement |
| $L^{net,q}_{a,t}$ | Net home load (load − PV) quantile $q$ | from telemetry `meter_kw − p_kw` (new) | §3 |
| $S_x$, $S_b$, $F_f$, $S^{xf}_\sigma$ | Transformer, bank, feeder and substation ratings | utility | §3 |
| $R^{rev}_f$, $R^{rev}_\sigma$ | Permitted reverse flow at a feeder head or substation | **0** unless the utility confirms bidirectional settings (`03` §8.10) | utility |
| $P^{POI,exp}_s$, $P^{POI,imp}_s$ | POI export/import limits | 20 MW / 20 MW | interconnection agreement |
| $\rho_s$ | Substation asset ramp | 10 MW/min non-firm (OQ-18) | agreement |
| $\varepsilon^{lex}$ | Lexicographic tolerance | 0.1% of the stage-R optimum | config |

### 1.3 Decision variables

Second stage (per $\omega$): $e_{a,t,\omega}$ energy above floor (kWh DC) · $g^{sol}_{a,t,\omega}$, $g^{grid}_{a,t,\omega}$ charge by
source · $d^F_{a,t,\omega}$ FREE energy discharge (ERCOT) · $y_{o,a,t,\omega}$ delivery to $o$ · $\sigma_{a,t,\omega}$ self-serve
(HOME) · $z_{o,t,\omega}$ shortfall · $s^{sol}_{c,\omega}$, $s^{hold}_{a,t,\omega}$, $\zeta_{a,\omega}$ slacks · $u_{s,t,\omega}\in\{0,1\}$
charge/discharge exclusivity (substation assets only, and only where $\lambda<0$).

First stage (no $\omega$, C16 by construction as in `02a` §3.5): $x_o$/$q_o$ selection · $\bar y_{o,a,t}$ reserved kW ·
$r_{a,k,t}$ AS held · $D_a$ billing demand (regulated primary-voltage assets).

Shorthand: total discharge $d^{tot}_{a,t,\omega}=d^F_{a,t,\omega}+\sum_{o}y_{o,a,t,\omega}+\sum_k\psi_{k,t,\omega}r_{a,k,t}$, where $\psi$
is the deployed share. Total charge is $g=g^{sol}+g^{grid}$. The discharge-side power claim is
$p^{claim}_{a,t,\omega}=d^F_{a,t,\omega}+\sum_o\bar y_{o,a,t}+\sum_kr_{a,k,t}$: **reserved kW count in full, even when not drawn**
(K13, need basis).

### 1.4 Constraints

The existing families C1–C24 (`03` §6.5, `02a` §3.3) carry over with $b\to a$. What changes or is new:

**C1 — energy balance** (substation assets add auxiliary load $\ell^{aux}_s$):

$$e_{a,t,\omega}=e_{a,t-1,\omega}+\eta^c_a\,(g^{sol}+g^{grid})_{a,t,\omega}\Delta_t-\frac{\Delta_t}{\eta^d_a}\,d^{tot}_{a,t,\omega}-(\ell^{sb}_aN_a+\ell^{aux}_a)\Delta_t,\qquad e_{a,0,\omega}=\hat e_a$$

Its dual in stage F is the **water value** $\nu_{a,t,\omega}$ ($/kWh DC), which is published to RT (D7).

**C2 — floor (K1).** $0\le e_{a,t,\omega}\le E^{use}_a$. The zero of $e$ is the 20% floor. Missing or stale SoC gives $\hat e_a:=0$ for
discharge (K1 rule, as `gate.py:193-222`).

**C3′ — the ONE additive floor, extended (Frank #6 and need basis):**

$$e_{a,t,\omega}+s^{hold}_{a,t,\omega}\ \ge\ \underbrace{\sum_k\frac{H_k}{\eta^d_a}r_{a,k,t}}_{\text{AS hold, both ends of }t}+\underbrace{\sum_{o\in\mathcal O^{FIX}_a}\frac{R^{rob}_{o,a,t}}{\eta^d_a}}_{\text{firm energy still owed}}+\underbrace{\sum_{o\in\mathcal O^{NEED}_a}\frac{\bar y_{o,a,t}\,h^{need}_o}{\eta^d_a}\mathbb 1[t\in W_o]}_{\text{need-basis reserved energy}}$$

It is written for $e_{a,t-1,\omega}$ and $e_{a,t,\omega}$, so the hold covers the whole interval. $M^{hold}\ge\max_o\beta_o/\eta_d$ (as `03` §6.6)
keeps $s^{hold}$ at zero unless the plan is infeasible.

**C5′ — continuous power, derated (F1).** $p^{claim}_{a,t,\omega}\le P^{cont}_a\,\bar f^{dis}_T(\hat T_{a,t})\,(1-\beta^{EV}_{a,t})$ and, for each
segment $j$ of the concave SoC curve,
$p^{claim}_{a,t,\omega}\le\kappa^{disp}_a P^{cont}_a\big(\alpha_j+\beta_j\,e_{a,t-1,\omega}/E^{nom}_a\big)$.
Charge mirrors this: $g_{a,t,\omega}\le P^{cont}_a\bar f^{ch}_T(\hat T)$ and the top-of-charge taper
$g\le P^{cont}_a(E^{use}_a-e_{a,t-1})/(0.1E^{nom}_a)$.

**C7(b)′ — no wash trade (D13).** $g_{a,t,\omega}=g^{rec}_{a,t,\omega}$ for $t\in W_o$, $a\in\mathcal A_o$, $m(o)=\mathrm{REG}$ or `DIST_DEFERRAL`.
Deliveries are settled net at the meter/POI.

**C12′ — fixed vs need basis (D9) and C24 lock:**

$$\text{FIXED:}\quad \sum_{a\in\mathcal A_o}\bar y_{o,a,t}=Q_{o,t}\,x_o\ \ (\text{or }q_o),\qquad y_{o,a,t,\omega}=\bar y_{o,a,t}$$
$$\text{NEED:}\quad \sum_{a\in\mathcal A_o}\bar y_{o,a,t}=K_o\,x_o,\qquad y_{o,a,t,\omega}\le\bar y_{o,a,t},\qquad \sum_a y_{o,a,t,\omega}+z_{o,t,\omega}\ge q^{need}_{o,t,\omega}\ (\le K_o)$$

For $o\in\mathcal O^c$: $x_o\equiv1$, and $Q$ or $K$ are parameters (C24, equality on totals, `selector/model.py:132-143`). Bank
substitution is allowed, but a reduction is not.

**C12″ — best effort after a shortfall (D10).** For $o\in\mathcal O^c$ in `AT_RISK`/`SHORTFALL`, $Q_{o,t}$ stays the full
committed kW. $z_{o,t,\omega}$ is priced at $\beta_o$ from the first short interval, so the LP restores delivery at the earliest
feasible interval. The obligation's assets may take recovery charge $g^{rec}$ inside C7(b)/C19 windows (C17 analogue).
Delivered energy, not planned energy, is settled.

**C25 — territory (K15):**

$$\text{(a)}\ \ \bar y_{o,a,t}=y_{o,a,t,\omega}=0\quad\forall a\notin\mathcal A^{u},\ m(o)=\mathrm{REG}(u)$$
$$\text{(b)}\ \ d^F_{a,t,\omega}=r_{a,k,t}=0\ \text{and}\ \bar y_{o,a,t}=0\ \ \forall a\in\mathcal A^{u},\ m(o)=\mathrm{FREE},\ \text{unless}\ \varphi^{acc}_u=1$$
$$\text{(c)}\ \ \sum_{a:\,\sigma(a)=\sigma}\big(d^{tot}_{a,t,\omega}+\textstyle\sum_kr_{a,k,t}(1-\psi)-g_{a,t,\omega}\big)\le L^{P10}_{\sigma,t}+R^{rev}_\sigma\quad\forall\sigma\in\Sigma^u\qquad(R^{rev}_\sigma=0)$$

(d) Charging of $a\in\mathcal A^u$ is priced at $c^{u,grid}$ and $c^{u,sol}$ only. Charging of $a\in\mathcal A^C$ is priced at
$\lambda_{z(a)}+w_{\tau(a)}$ (C27).

(c) is the physical form of "energy can't leave the territory". Base's net injection at every boundary substation
is absorbed by native load. It is the same inequality as the substation reverse-flow limit F3.

**C26 — FREE only on uncommitted headroom (K13 unchanged).** This follows from C5′ with $\bar y$ counted in full and from C3′.
No FREE variable appears in the stage-R objective.

**C27 — charging sources and the solar floor (D4, D5):**

$$0\le g^{sol}_{a,t,\omega}\le S^{sol}_{a,t,\omega},\qquad \sum_{a\in\mathcal A_c}\sum_{t\in P}g^{sol}\Delta_t+G^{sol,mtd}_c+s^{sol}_{c,\omega}\ \ge\ \phi^{sol}_c\Big(\sum_{a\in\mathcal A_c}\sum_{t\in P}g\,\Delta_t+G^{mtd}_c\Big)$$

The accounting period is the contract's (default: calendar month). $G^{mtd}$ carries month-to-date energy into each gate,
so the floor is a running constraint, not a daily one. Billing demand for primary-voltage regulated assets is
$D_a\ge g^{grid}_{a,t,\omega}\ \forall t,\omega$ and $D_a\ge D^{mtd}_a$.

**C28 — substation assets (D11).** Each $s\in\mathcal S$ has its own C1/C2/C3′/C5′. It also has the POI and transformer rows of
F3 and a per-asset ramp $|d^{tot}-g|_{s,t}-|d^{tot}-g|_{s,t-1}\le60\rho_s\Delta_t$ (linearised with the net-power variable). C14
exclusivity is binary only for FREE-eligible substation assets at $\lambda<0$: a single PCS cannot split, whereas a bank can
charge some hubs while discharging others. DATA_CENTER locality: $\mathcal A_o$ = the substation assets on the DC's
substation, plus home banks on that substation's feeders. The nearest asset is preferred by a tie-break cost
$\delta_{o,a}=10^{-4}\,\$/\text{kWh}\times\text{rank}$ and by the water-fill order at RT.

**C15 — terminal energy** is kept, as the penalised target already built (`selector/model.py:226-237`).

### 1.5 Objective: lexicographic, two stages (D3)

**Stage R (regulated first).**

$$z^*_R=\max\ \sum_{o\in\mathcal O^{new}_{\mathrm{REG}}}V^{cap}_o\,\frac{|\mathcal T|\Delta}{8760}\,q_o\;-\;\sum_\omega\pi_\omega\sum_{o\in\mathcal O^c,t}\mathrm{Pen}_o(z_{o,t,\omega})$$

**Stage F (net value on the residual).**

$$\begin{aligned}\max\;&\sum_\omega\pi_\omega\sum_{a,t}\Delta_t\Big[\big(\tfrac{\lambda_{z(a),t,\omega}}{1000}-c^{deg}_a\big)d^F_{a,t,\omega}+\sum_o\big(v^E_{o,t}-c^{deg}_a\big)y_{o,a,t,\omega}-c^{sol}_{a,t}g^{sol}_{a,t,\omega}-c^{ch}_{a,t,\omega}g^{grid}_{a,t,\omega}\Big]\\&+\sum_{a,k,t}\Delta_t\tfrac{\mu_{k,t}}{1000}r_{a,k,t}+\sum_{o\in\mathcal O^{new}}V_oq_o-\sum_ac^{dem}_aD_a-\sum_\omega\pi_\omega\Big[\sum_cM^{sol}s^{sol}_{c,\omega}+\sum_{o,t}\mathrm{Pen}_o(z)+\sum M^{hold}s^{hold}+\sum M^{end}\zeta\Big]\\&-\sum_{o,a}\delta_{o,a}\bar y_{o,a,t}\Delta_t+\sum_\omega\pi_\omega\sum_aV_a(e_{a,T,\omega})\end{aligned}$$

$$\text{s.t. all constraints, and}\quad \text{(stage-R objective)}\ \ge\ z^*_R-\varepsilon^{lex}|z^*_R|$$

with $c^{ch}_{a,t,\omega}=\lambda_{z(a),t,\omega}/1000+w_{\tau(a)}$ for $a\in\mathcal A^C$ and $c^{ch}=c^{u,grid}_t$ for $a\in\mathcal A^u$ (D5).
The wear term applies to every discharged kWh: $d^F$, $y$ and the expected AS deployment $\psi r$. It never applies to $r$,
$\bar y$ or $g$ (D8).

**Why lexicographic, not a premium weight.**

- **P4 / P5.** A regulated capacity contract is a multi-month or multi-year commitment. A 24 h gate compares its pro-rated
  value (about $0.21/kW-day at $75/kW-yr) with one day's scarcity. A single P90 evening at $650/MWh is worth $0.65/kW-h.
  Weighting would let one forecast spike decline or shrink a long-term regulated tranche. The gate cannot price the
  contract term (non-anticipativity across the contract), so the business rule has to be structural.
- **Review §5.4-1 and finding #3.** Priority must mean the same thing at every layer. RT is already lexicographic
  (`allocator/lexicographic.py:41-80`). A weight would need a dominance proof against RTSWCAP/VOLL ($5,000/MWh). That
  implies coefficients around $10^6$ and poor conditioning.
- **P7 is kept inside each stage.** Among regulated options, stage R still maximises value. Stage F maximises the full net
  value, including charging, M1 and wear.
- **The cost is measured, not assumed.** It is the forgone FREE upside from REG priority. It is reported as a separate
  KPI-22 term (`pnl.forgone_upside`, `settle/profitability.py:61-76`).
- **Cheaper than it looks.** Stage R depends only on REG-territory assets and the constraints coupling them (C25(c), shared
  feeders and substations). It can be solved on that sub-model (§5). Stage F reuses the basis.
- **Commitments first, whatever the market.** Committed FREE obligations' penalties sit in stage R too, so K13 holds
  across markets: committed > new REG > new FREE and headroom.

### 1.6 Charging cost and the M1 rule (D5), stated once

| Asset location | Source | Cost per kWh drawn | M1? |
|---|---|---|---|
| ERCOT competitive area (TDSP) | Grid | $\lambda_{z(a),t}$ + $w_{\tau(a)}$ (full volumetric, flat) | **Yes** (owner decision 2026-09-26: full charge; no WSL/ADER exemption for BTM) |
| ERCOT competitive area | Rooftop PV surplus behind the meter | forgone export credit (REP buyback, default $\lambda$) | No (it never crosses the meter) |
| AE / CPS territory | Grid | contract charging terms $c^{u,grid}_t$ (default AE TOU pilot power supply; CPS per contract) + contract adders + demand charge | **No.** AE and CPS are not TDSPs; their delivery cost is inside the utility rate |
| AE / CPS territory | Utility solar (contract) | $c^{u,sol}$ (default 4.0 ¢) | No |
| AE territory | Rooftop PV | VoS 12.88 ¢ opportunity cost (Oct 2026, secondary source) | No |
| Substation asset, competitive area | Grid | $\lambda$ + the TDSP's applicable charge. The default is the full volumetric $w$ until the WSL or DESR treatment is confirmed (OQ-12) | Yes (default) |

### 1.7 Wear rule (Frank #7): decision and justification (D8)

**Rule.** Wear is charged per **AC kWh actually discharged at the asset terminals**, at $c^{deg}_{class}$, for every purpose:
firm delivery, REG capacity delivery, FREE energy, AS deployment and HOME self-serve (attributed to HOME). It is not
charged on held capacity ($r$, $\bar y$, the unused need-basis reserve), and not on charging.

The selector uses the expected discharged kWh. Settle uses metered discharged kWh (`meter_interval.delivered_kwh` for
obligations, plus the new headroom meter rows). Both call one function, `core.economics.wear_cost(kwh_out,
asset_class)`, per the build-team no-duplication rule.

**Why.**

- **P1.** Cycle wear scales with throughput. One full cycle is one charge plus one discharge, so charging on discharge
  only counts each cycle once; charging both sides would double it.
- **P8.** Discharged kWh is the metered, billable quantity, so it is the only basis both layers can measure identically.
- **Holds don't cycle.** This agrees with the code's own reasoning at `selector/model.py:250-262`.
- **Calendar aging is a fixed cost.** It goes in O&M, not in the dispatch objective. C18's dual (R2) stays the marginal
  signal for the cycle budget.

**Consistency check to report.** At $0.03/kWh, a home unit cycling daily (35.3 kWh × 300 days) accrues **$318/yr** of wear.
The addendum §3c assumes $210/yr for degradation and O&M together. One of the two numbers needs Base's warranty
throughput curve (OQ-15).

### 1.8 Evening-ramp energy and the water value (D7)

- **Selector.** Duck-curve scenarios per zone carry the evening scarcity (P90) and the midday dip (P10, which can be
  negative). The LP holds energy for the ramp only as far as the scenarios pay for it. There is no extra hard hold,
  because it would duplicate a price (review finding #17, P7).
- **The plan publishes, per asset and interval:**
  - the **hard hold floor** $e^{hold}_{a,t}$ = the right-hand side of C3′;
  - the **planned floor** $e^{plan}_{a,t}=\min_\omega e^*_{a,t,\omega}$ (the non-anticipative part);
  - the **water value** $\nu_{a,t}=\sum_\omega|\text{dual C1}|$.
- **RT (S6).** Headroom is discharged only if $\lambda^{RT}_{z(a)}\ge\nu_{a,t}+\theta^{hyst}$, and only down to $e^{plan}$. Going below
  $e^{plan}$ needs $\lambda^{RT}\ge\nu_{a,t^{ramp}}$, the ramp-hour water value. It never goes below $e^{hold}$ (engine S6; measured by `CHECK_AS_HOLD`). This
  replaces the fixed $30/MWh threshold (G9).
- **Prototype evidence (§9).** The water value of an Oncor bank is about $82/MWh at 03:00, because night charging costs
  $\lambda$ plus the $60/MWh M1 charge. The 2 h substation asset's water value rises to $171/MWh inside the REG window.

### 1.9 Maximum discharge-flow limits at every level (owner requirement)

Each limit appears in the selector (planning form, aggregated), the SCED (5-min, next hour), RT (per hub, per cycle) and,
where relevant, a PI loop. The guardian re-checks each one on its own reads (§2.6). Sign convention follows the code:
$p>0$ is charge and $p<0$ is discharge (`core/physics.py:44-60`). $M_i$ is meter net import (+ = import).

**Already enforced today** (the owner asked for file:line):

| Limit | Allocator / fleet | Guardian | Checker |
|---|---|---|---|
| Hub rated power (11/20 kW, dual-unit fix) | `fleet/__init__.py:497` via `core/physics.py:137-145` (full $P$ whenever energy is above the floor, **no derating**) | `core/limits.py:65-72`, `guardian/checks.py:60-64`, `guardian/service.py:240` | none |
| Energy sustainable over the lease | `core/physics.py:63-81`, `allocator/cycle.py:259-284` | `core/limits.py:39-62`, `guardian/checks.py:46-57`, `guardian/service.py:235-239` | K1 (`invariants/checks.py:35`) |
| Stale SoC means zero discharge | `allocator/cycle.py:274-277` | `guardian/service.py:224-228` | — |
| Hub ramp | `core/physics.py:35-41` | `core/limits.py:94-105`, `guardian/checks.py:105-110`, `guardian/service.py:244-248` | none |
| Bank kVA, both directions | discharge ceiling `core/physics.py:154-160` (`fleet/__init__.py:501`); charge `fleet/__init__.py:503-506` | magnitude `guardian/checks.py:83-102`, fresh-SCADA veto `:75-80`, `guardian/service.py:286-319` | none |
| Fleet ramp cap | — | `core/limits.py:108-122`, `guardian/service.py:261-270` | none |
| Feeder ramp (**firm events only**) | — | `core/limits.py:125-138`, `guardian/service.py:272-282` | none |
| L2 LIMIT/BLOCK | `fleet/__init__.py:508-511`, `allocator/cycle.py:317-343` | G-15 `guardian/checks.py:181-193` | none |
| **Not enforced anywhere** | SoC and temperature derating; home load and the meter export limit; service transformer; feeder thermal and feeder-head reverse flow; substation POI and transformer; territory export; peak vs sustained | | |

The seven families follow. Each one lists the selector (planning) form, the other layers, the guardian check, the data
source and the tests.

**F1. SoC- and temperature-dependent derating** — $P^{dis}_{max}(SoC,T)$ and $P^{ch}_{max}(SoC,T)$.

Default curves are piecewise-linear. **Confirm them with Base**; an LFP chemistry is assumed. SoC is the fraction of
nameplate; the floor is 0.20.

| $f^{dis}_{SoC}$ | 0.30 at SoC 0.20, rising linearly to 1.0 at SoC 0.30; 1.0 above (K1 stops discharge at 0.20). As an LP row: $P\le P^{cont}(0.3+7\,e/E^{nom})$ with $e$ above the floor |
|---|---|
| $f^{ch}_{SoC}$ | 1.0 up to 0.90, then linear to 0 at 1.00 (taper) |
| $f^{dis}_T$ (cell °C) | −10→0 · 0→0.3 · 10→0.8 · 15–35→1.0 · 45→0.7 · 50→0.4 · 55→0 |
| $f^{ch}_T$ (cell °C) | <0→0 · 0–10→0.3 · 10–15→0.6 · 15–40→1.0 · 45→0.5 · 50→0 |

Where the hub reports its BMS limit, it binds: $P^{dis}_{max,i}=\min\big(P^{cont}_if^{dis}_{SoC}(s_i)f^{dis}_T(T_i),\ P^{BMS,dis}_i\big)$.

- **Selector:** C5′. The SoC curve is concave, so an LP with ≤ 2 segments suffices. Temperature uses the forecast
  $\hat T$. The aggregation haircut $\kappa^{disp}_a=\sum_iP^{dis}_{max,i}(t_0)/(N_aP\,f(\bar s))$ from the twin corrects
  Jensen's bias (dispersed hub SoCs derate more than the mean SoC suggests).
- **SCED:** the same, with the latest measured temperatures.
- **RT:** per-hub cap `free_discharge_kw = min(P_max(soc,T), sustainable-over-lease)`. This extends
  `core.physics.hub_capability` (single owner).
- **PI:** — (a capability input only).

**F2. Home load first, then the meter export limit.** Discharge first serves the home. Only the excess exports:
$M_i=L^{net}_i+p_i\ge-X^{exp}_i$ and $M_i\le S^{svc}_i$.

- **Selector:** per bank, $d^{tot}+\sum r-g\le L^{net,P10}_{a,t}+X^{exp}_a$, with $X^{exp}_a=\min(\sum_iX^{exp}_i,\sum_{x}S_x)$, and
  $g-d^{tot}\le\sum_iS^{svc}_i-L^{net,P90}_{a,t}$. The self-serve split $\sigma\le[L^{net}]^+$ values the HOME savings (C8, `03`
  §6.5).
- **SCED:** the same, with measured $L^{net}$.
- **RT:** per hub, $-p_i\le X^{exp}_i+\hat L^{net}_i-\Delta L$ (water-fill cap).
- **PI:** — (the hub firmware export limiter, UL 1741 SB PCS, is the L0 fail-safe).

**F3. Aggregate flows, both directions.** These cover the service transformer $x$, bank $b$ (built), feeder $f$ and substation
$\sigma$, plus the substation asset's POI and transformer:
$|L_x+\sum_{i\in x}p_i|\le S_x$ · $|L_b+\sum p|\le S_b$ · $-R^{rev}_f\le L_f+\sum_{a\in f}(g-d)\le\rho^{th}F_f$ · the same at $\sigma$ ·
$-P^{POI,exp}_s\le p_s\le P^{POI,imp}_s$.

- **Selector:** bank rows as built. For feeders and substations, the sum over assets is bounded with $L^{P10}$ on the reverse
  side and $L^{P90}$ on the forward side. $S_x$ enters only through $X^{exp}_a$.
- **SCED:** feeder and substation budgets for the next hour.
- **RT:**
  - water-fill with **group caps** per transformer (nested caps, redistributing within the obligation, K13-safe);
  - bank cap (built);
  - a **per-cycle feeder budget**, split across the banks on the feeder in proportion to their capability. It runs before
    each bank's S3, because the allocator runs per bank (`allocator/cycle.py:95`).
- **PI:**
  - the bank kVA PI (DIST_DEFERRAL) is unchanged (K9);
  - there is a new **substation POI limiter**, one loop per quantity (the substation transformer kVA);
  - the feeder is a clamp (saturation) with no integrator. This keeps K9: one integrating loop per quantity.

**F4. Territory export (K15).** C25(c): no net reverse flow at territory $u$'s boundary substations. This is F3 at $\sigma\in\Sigma^u$
with $R^{rev}_\sigma=0$, plus C25(a/b) market segregation.

- **Selector:** C25.
- **SCED:** the same.
- **RT:**
  - an eligible-hub filter by territory for REG obligations;
  - no FREE headroom for territory assets unless $\varphi^{acc}_u=1$;
  - the substation budget.
- **PI:** — (the POI limiter, as F3).

**F5. Sustained vs peak.** Continuous $P^{cont}$ is used for any interval ≥ 5 min. The peak $P^{pk}$ is allowed only for
$\le\tau^{pk}$ and within the hub's reported peak budget $B_i$ (kW·s).

- **Selector:** $P^{cont}$ only (Δ = 15 min ≫ $\tau^{pk}$).
- **SCED:** $P^{cont}$.
- **RT:** above-continuous grants go only to event profiles with `sustain_duration_s` $\le\tau^{pk}$. Their **lease TTL is
  $\le\tau^{pk}$**, because K7's hold-the-last-setpoint-until-lease-expiry would otherwise sustain a peak past its limit. The
  ramp to peak uses G-04.
- **PI:** the DATA_CENTER fast loop (P-only) may use the peak inside the bridge.

**F6. Ramps.** These are built (hub, fleet, feeder for firm events). Additions:

- the per-asset ramp $\rho_s$ for substation assets;
- the feeder ramp for **non-firm** steps too, because a 20 MW asset alone exceeds the 10 MW/min non-firm fleet cap.

- **Selector:** C28 ramp.
- **SCED:** ramp across 5-min steps.
- **RT:** ramp-limited setpoints (`core/physics.py:172-199`).
- **PI:** ramps inside the PI (`allocator/dist_deferral_pi.py`).

**F7. Energy holds** (AS 4 h/1 h, firm energy owed, need basis) as a limit on *headroom* discharge.

- **Selector:** C3′.
- **SCED:** C3′.
- **RT:** S6 discharges headroom only down to $e^{hold}$ (and to $e^{plan}$ unless the price is above $\nu$).
- **PI:** —

**Guardian check, data source and tests for each family:**

| # | Guardian (§2.6) | Data source | Tests |
|---|---|---|---|
| F1 | **G-02 changed** | Base BMS datasheet, hub telemetry `cell_temp_c` and `p_dis_max_kw` (new), NWS | TS-19-20, 21, 22 |
| F2 | **G-26 new** | Base install records (interconnection agreement per premise), telemetry `meter_kw`/`pv_kw` (new) | TS-19-23, 24, 25 |
| F3 | G-03 (built) · **G-27 transformer** · **G-28 feeder** · **G-29 substation** | Utility GIS/AMI (transformer to meter), feeder and substation SCADA, interconnection agreements | TS-19-26 … 30 |
| F4 | **G-30 new** (territory export) + **G-33 new** (market segregation, §11.6) | Territory polygons (§3), contract market | TS-19-31, 32 |
| F5 | **G-31 new** | Base (inverter datasheet), telemetry `peak_budget_kws` (new) | TS-19-33, 34 |
| F6 | G-04/05/06 changed; **G-32 new** (feeder ramp, non-firm steps) | Asset registry, utility agreement | TS-19-35 |
| F7 | **none** (not a guardian check): selector C3′ + engine S6; checker `CHECK_AS_HOLD` | Ledger (AS awards, reservations), telemetry SoC | TS-19-36, 37 |

---

## 2. Layer split

### 2.1 DA/ID MILP and gate selection (`selector/`)

| Change | Detail |
|---|---|
| Inputs | `BankSnapshot` becomes `AssetSnapshot` (class, zone, territory, TDSP, feeder, substation, $E^{nom}$, floor, $P^{cont}$, derating curves, $\kappa^{disp}$, export and import caps, POI). **Horizon-varying capability**: $P_{a,t}$ uses the forecast temperature (fixes G8). Scenarios keyed by `series_key` per zone (fixes G1), plus solar availability and net home load quantiles |
| Eligibility | $\mathcal A_o$ from contract locality ∩ territory (C25a/b; fixes G7). `load_committed`/`load_candidates` stop passing all banks |
| Model | C1, C2, C3′ (with $r$ variables, fixes G6), C5′, C7(b)′, C12′/C12″, C24, C25, C27, C28, F2/F3 rows; the objective of §1.5 with M1 and the wear rule (fixes G3, G5) |
| Solve | Stage R (sub-model), fix within $\varepsilon^{lex}$, then stage F with a basis or MIP-start warm start. Rule fallback F2 keeps "firm first, then AS, then market", now read as "committed, then REG, then FREE" |
| Validator | `selector/validate.py` re-derives C25 (territory), C3′ holds, the solar floor slack, and F2/F3 rows from the solution values |
| Outputs | The plan artifact adds $e^{hold}_{a,t}$, $e^{plan}_{a,t}$, $\nu_{a,t}$, the charging schedule by source and caps, the market tag on every reservation, and stage-R/stage-F objectives (KPI-22 forgone upside) |
| Admission | `contracts.admit` rejects an obligation whose eligible assets lie outside its market's territory (`R-TERRITORY-INELIGIBLE`) |

### 2.2 SCED (5-min)

MVP-S folds SCED into the allocator's per-cycle lookup (`02a` §3.1). For the regulated pilot this stays folded, with three
additions computed every 5 minutes by the allocator adapter from the latest plan and measurements:

- the per-feeder and per-substation flow budgets for F3/F4;
- the re-read water value $\nu$ at the current interval;
- the charging split (solar-following vs grid), recomputed from measured PV surplus and the TOU period.

**Later:** a separate SCED LP over the next 1–2 h per asset (≤ 44 assets × 12 steps, < 0.2 s), repricing headroom
at RT prices, with commitments as parameters (review §5.2).

### 2.3 RT lexicographic LP and water-fill (`allocator/`)

| Step | Change |
|---|---|
| S1/S2 | Subtract all committed $\hat y$ across **both** markets (unchanged rule; the market tag is carried) |
| Eligibility | `_index_hubs_by_obligation` filters hubs by territory for REG obligations (K15). FREE headroom is skipped for territory assets without access |
| S3 | Tiers are unchanged in shape. REG capacity is T1 (as DIST_DEFERRAL/PARTNER_CAPACITY), FREE contracts T2/T3, headroom T4/S6 |
| S4 caps | Per hub: $\min(P^{dis}_{max}(SoC,T)$ [F1], export headroom [F2], energy over lease [built], peak rule [F5]$)$. Group caps per service transformer [F3]. Bank cap [built]. Feeder and substation budget [F3/F4] |
| S5 | Substitution is unchanged, including the best-effort SHORTFALL codes (`allocator/cycle.py:217-226`) |
| S6 | Threshold = water value $\nu_{a,t}$ + hysteresis, not $30. Floor = $\max(e^{hold},e^{plan})$ (F7/D7) |
| Charging | New S6c: the charging setpoint follows the plan's source split. Solar-following charge is feed-forward from measured PV surplus (no integrator). Grid charging only in allowed windows, never inside C7(b)′ windows except recovery |
| Substation assets | One setpoint per asset (no water-fill); ramp $\rho_s$; POI limiter |
| Prices | Per bank from the bank's zone series (fixes G2, `engine/gateways.py:238-247`) |

### 2.4 PI loops

| Loop | Quantity | Change |
|---|---|---|
| DIST_DEFERRAL PI | Bank kVA | Unchanged (K9) |
| **Substation POI limiter** (new) | Substation transformer kVA / POI flow | PI with anti-windup on the measured substation flow. It is the single integrator for that quantity. The allocator's substation budget is feed-forward |
| DATA_CENTER fast loop | Site meter kW | P-only (unchanged, `06` §4.b). It now draws on the substation asset first; home banks take the residual as feed-forward |
| Feeder limiter | Feeder-head kW | A clamp only (no integrator), so K9 holds |
| Solar-following charge | PV surplus | Feed-forward only |

### 2.5 Guardian: new and changed rules (summary)

**Numbering.** `00-invariants.md` fixes G-21…G-25 for K14. `02a` §6.1's "supplemental" G-21…G-31 table already
collided with those numbers and was never built (`guardian/checks.py` has G-01…G-25 only). This spec assigns **G-26…G-33**
canonically, **as built by SAFETY on 2026-09-26** (`guardian/flow_checks.py`), and asks `02a` §6.1 to retire its
supplemental table (§10).

| ID | Rule | Kind | Invariant |
|---|---|---|---|
| G-01 | Floor generalised to asset classes (substation floor fraction per asset) | item | K1 |
| **G-02 (changed)** | Derated power $P_{max}(SoC,T)$, charge and discharge; continuous rating (the peak is only via G-31) | item | K4 |
| G-03 | Bank kVA (built); generalised to the substation-asset transformer | batch | K4 |
| G-04/05/06 | Per-asset ramp; the substation asset has its own G-05 budget (the non-firm feeder ramp is G-32) | item/batch | K4 |
| G-09 | Unchanged (ledger version) | batch | K2 |
| G-19 | Also covers need-basis REG, and FREE grants never using REG-reserved kW | obligation | K13 |
| **G-26** | Home meter export and import limit, net of home load | item | K4 |
| **G-27** | Service-transformer loading, both directions | item (every item under the offending transformer) | K4 |
| **G-28** | Feeder thermal and feeder-head reverse flow | batch | K4 |
| **G-29** | Substation asset POI and substation-transformer limit, both directions | batch | K4 |
| **G-30** | Territory export: no net reverse flow out of a regulated territory (its boundary substations) | batch | K15 (c) |
| **G-31** | Sustained vs peak over the lease | item | K4 |
| **G-32** | Feeder ramp for **non-firm** steps (G-06 covers firm events only) | batch | K4 (e) |
| **G-33** | K15 market segregation: `market.check_territory` per item (§11.6); a 0 kW item passes (R3) | item | K15 (a, b) |
| **G-34** (R3) | Every item's hub is on the proposal's bank, in the guardian's own topology (`R-HUB-NOT-IN-BANK`) | batch | K3/K4 |

**The AS energy hold (Frank #6, D6, F7) is NOT a guardian check.** It is enforced by the selector (C3′) and the
engine/allocator (S6 hold floor), and measured post hoc by the invariants checker (`CHECK_AS_HOLD`). Earlier
drafts of this spec called it "G-32"; that number is now the non-firm feeder ramp.

`guardian/service.py` `_ITEM_LEVEL_RULES` (as built) holds G-01, G-01-ENERGY, G-02, G-04, G-24, G-26, G-27, G-31 and
G-33. A G-27 violation vetoes every item of the batch under the offending transformer; the verdict is PARTLY_VETOED if
other items remain.

### 2.6 Guardian flow-limit checks in detail (owner clarification: independent reads, fail closed)

The rules below apply to every check in this section.

- **No trusting the allocator.** The guardian evaluates on its **own** reads: its hub-state port (telemetry), its SCADA
  port, and static rows from the DB (`og.hub`, `og.asset`, `og.service_transformer`, `og.feeder`, `og.territory`,
  `og.contract`). It never uses the allocator's claimed capability. This is the K3/K4 pattern, "one formula, two data
  paths", using the same `core.limits` functions.
- **The guardian never clips.** It returns PASS, VETO or PARTLY_VETOED (X-2). "Clip-to-safe" below means the check
  evaluates against the conservative bound. The allocator computes the **same** bound, so a compliant proposal passes and a
  non-compliant item is vetoed.
- **Staleness.** An input is stale when it is older than its `*_max_age_s` (default: the G-03 `bank_load_max_age_s`).
- **Relief always passes.** A batch that reduces the magnitude of an already-violated flow is never vetoed, as G-03 does at
  `guardian/checks.py:94-97`.

The checks follow (G-33 is in §11.6). For each: the inequality, the guardian's own data, the fail-closed rule, the veto semantics, and
the tests.

**G-02 (changed): derated power (F1).**

- **Inequality:** $-p^{cmd}_i\le\min\big(P^{cont}_if^{dis}_{SoC}(s^G_i)f^{dis}_T(T^G_i),P^{BMS,dis,G}_i\big)$ and
  $p^{cmd}_i\le\min\big(P^{cont}_if^{ch}_{SoC}(s^G_i)f^{ch}_T(T^G_i),P^{BMS,ch,G}_i\big)$.
- **Own data:** telemetry SoC, `cell_temp_c` and `p_dis_max_kw`; `og.hub` $P^{cont}$ and units; `og.derate_curve`.
- **Fail closed:** stale SoC gives zero discharge (existing, `guardian/service.py:224-228`). A stale temperature makes
  $f_T:=f^{unk}_T$ (default 0.5; config 0 = strict). A stale BMS limit drops that term, so the curve alone applies. A missing
  curve means the class default curve.
- **Veto:** item-level, PARTLY_VETOED.
- **Property test:** for random $(s,T)$ and setpoints, PASS ⇔ $|p|\le$ bound. The allocator's cap never exceeds the guardian's
  bound on identical inputs.
- **Negative tests:** stale temperature at 11 kW is vetoed above 5.5 kW; 45 °C at 11 kW is vetoed above 7.7 kW.

**G-26 (new): home meter export and import (F2).**

- **Inequality:**
  $\hat L^G_i=M^G_i-p^G_i$ (net home load, same timestamp).
  Export: $\hat L^G_i-\Delta L+p^{cmd}_i\ge-X^{exp}_i$.
  Import: $\hat L^G_i+\Delta L+p^{cmd}_i\le S^{svc}_i$.
  $\Delta L$ = load-drop allowance over the lease (default 0.5 kW).
- **Own data:** telemetry `meter_kw`, `p_kw`, `pv_kw`; `og.hub` $X^{exp}$, $S^{svc}$, $PV^{rated}$.
- **Fail closed:**
  - A stale or missing meter reading makes $\hat L:=-PV^{rated}_i$ for the export side (no load, full PV) and $\hat L:=S^{svc}_i$
    for the import side.
  - An unknown $X^{exp}$ is 0, so discharge is limited to the measured load, and to zero if that is stale.
  - The guardian **cannot see the home's future load**. $\Delta L$ covers it for the lease. The hub's own export limiter
    (UL 1741 SB PCS) is the L0 fail-safe and a go-live condition.
- **Veto:** item-level, PARTLY_VETOED.
- **Property test:** random load, PV and limits; PASS ⇔ inequality.
- **Negative tests:** stale meter with PV 7 kW and $X$ 10 kW means discharge above 3 kW is vetoed; unknown $X$ means any
  export is vetoed.

**G-27 (new): service transformer (F3).**

- **Inequality:** for each $x$ touched,
  $\hat F_x=\sum_{i\in H_x}M^G_i+\sum_{i\in H_x\cap\text{batch}}(p^{cmd}_i-p^G_i)$, and
  $-\rho^{rev}S_x\le\hat F_x\le\rho^{xf}S_x$ (defaults 1.0 and 1.0).
  It passes if $|\hat F_x|\le|F^{now}_x|$.
- **Own data:** telemetry `meter_kw` of **all** members (not only batch items); `og.service_transformer` rating and members.
- **Fail closed:**
  - A stale member takes its worst case per direction (as G-26).
  - More than 20% of members stale means any increase of $|\hat F_x|$ is vetoed.
  - An unmapped hub is treated as a group of one with $S^{def}$ = 5 kVA per home (clip-to-safe default). The alert
    `ALR-XFMR-UNMAPPED` fires.
- **Veto:** group-level. All items under $x$ are vetoed; PARTLY_VETOED if others remain.
- **Property test:** random groups, loads and batches; the verdict matches a brute-force evaluation.
- **Negative tests:** reverse flow above $S_x$ at midday PV is vetoed; one stale member plus an increase is vetoed.

**G-28 (new): feeder thermal and head reverse flow (F3).**

- **Inequality:** $\hat F_f=L^G_f+\sum_{\text{banks on }f,\ \text{this cycle}}\Delta p$, and $-R^{rev}_f\le\hat F_f\le\rho^{th}F_f$ (default 0.95).
- **Own data:** feeder-head SCADA (own port); `og.feeder` $F_f$ and $R^{rev}_f$; a per-cycle accumulator across bank batches (as G-06,
  `guardian/service.py:272-282`).
- **Fail closed:** stale feeder SCADA vetoes any batch that increases $|\Delta p|$ on the feeder (as `BANK_LOAD_STALE`). A missing
  $F_f$ vetoes any increase.
- **Veto:** batch-level, VETOED (that bank's batch; the allocator re-solves).
- **Property test:** the cumulative per-cycle sum across banks never exceeds the bound.
- **Negative tests:** two banks whose individually safe batches jointly reverse the feeder head are vetoed; stale SCADA
  vetoes increases and passes relief.

**G-29 (new): substation asset POI and substation transformer (F3).**

- **Inequality:**
  POI: $-P^{POI,exp}_s\le p^{cmd}_s\le P^{POI,imp}_s$ and $|p^{cmd}_s|\le P^{cont}_s$ (or G-31).
  Transformer: $-R^{rev}_\sigma\le L^G_\sigma+\sum_{\text{this cycle on }\sigma}\Delta p\le\rho S^{xf}_\sigma$.
  The territory boundary ($R^{rev}_\sigma=0$ for $\sigma\in\Sigma^u$) is G-30.
- **Own data:** substation meter/SCADA (own port); `og.asset` POI and ratings; `og.substation`.
- **Fail closed:** stale substation SCADA vetoes increases; a missing POI row means zero dispatch.
- **Veto:** batch-level, VETOED.
- **Property test:** random asset and bank batches on one $\sigma$; the bound holds.
- **Negative tests:** a POI export above its limit is vetoed; stale SCADA vetoes increases.

**G-30 (new): territory export, no reverse flow out of a regulated territory (F4, K15 c).**

- **Inequality:** for the regulated territory $u$ of the batch's bank, the Base net injection across $u$'s boundary
  substations stays within native load: $L^G_{u}+\sum_{\text{this cycle, banks and assets in }u}\Delta p\ \ge\ 0$ with
  $R^{rev}=0$ (C25 c). Competitive-area banks are not checked by G-30.
- **Own data:** the territory's aggregate flow from the guardian's own topology/SCADA port (`territory_flow`, all banks
  whose zone maps to $u$ in `[zone_territory]`); the per-cycle accumulator across bank batches.
- **Fail closed:** stale or missing territory flow vetoes any batch that increases discharge in the territory; relief passes.
- **Veto:** batch-level, VETOED.
- **Property test (TS-19-32):** Base net injection at territory boundary substations is ≤ 0 in all fixtures.
- **Negative tests:** a 20 MW discharge at 15 MW territory load is vetoed; stale territory SCADA vetoes increases.

Market segregation per item (K15 a/b) is **G-33**, specified in §11.6.

**G-31 (new): sustained vs peak (F5).**

- **Inequality:** if $|p^{cmd}_i|>P^{cont}_i$, then all of these hold: $|p^{cmd}_i|\le P^{pk}_i$, $TTL\le\tau^{pk}_i$, and
  $(|p^{cmd}_i|-P^{cont}_i)\,TTL\le B^G_i$.
- **Own data:** telemetry `peak_budget_kws`; `og.hub`/`og.asset` $P^{pk}$ and $\tau^{pk}$; the batch TTL.
- **Fail closed:** a missing budget is 0, and a missing $P^{pk}$ means $P^{pk}=P^{cont}$. Either way, anything above continuous is
  vetoed.
- **Veto:** item-level, PARTLY_VETOED.
- **Property test:** random budget and TTL combinations.
- **Negative tests:** a 30 s lease above continuous with $\tau^{pk}=10$ s is vetoed.

**G-32 (new): feeder ramp for non-firm steps (F6).**

- **Inequality:** $|\sum_{\text{banks on }f,\ \text{this step}}\Delta p|\le$ the feeder's ramp ceiling pro rata, using the
  **non-firm** cap (`non_firm_ramp_cap_kw_per_min`, default 10 MW/min) for market, AS and headroom steps
  (`core.limits.check_feeder_ramp`). G-06 keeps firm events. A single 20 MW asset alone can exceed the fleet's non-firm
  cap on its own feeder, which is why this is a separate check.
- **Own data:** the batch setpoints against the guardian's last signed setpoints per hub; `og.bank.feeder_id`; the
  per-cycle feeder accumulator (as G-06).
- **Fail closed:** a bank with no feeder is checked as a feeder of one bank.
- **Veto:** batch-level, VETOED, reason `R-FEEDER-RAMP-NON-FIRM`.
- **Negative tests:** a non-firm step above the cap on one feeder is vetoed; the same step as a firm event passes G-32 and
  is judged by G-06.

**The AS energy hold (F7, Frank #6, D6) is not a guardian check.** The hold
$e\ge\sum_kH_kr/\eta_d$ (plus firm energy owed and need-basis energy) is enforced by the selector (C3′) and by the
engine/allocator (S6 discharges headroom only down to $e^{hold}$). The invariants checker measures it post hoc as
`CHECK_AS_HOLD` (TS-19-36/37 run against the allocator and the checker, not the guardian).

**As built at `r3` (`main` `451a2a2`).** Checked in the `r3` code; the full list with tests is in `02a` §6.8.

- **G-34.** A batch whose item names a hub outside `proposal.bank_id` is VETOED, `R-HUB-NOT-IN-BANK`
  (`orchestrator/src/opengrid/guardian/flow_checks.py:317`, wired `guardian/service.py:627`). The per-bank sums of
  G-03, G-06/G-32 and G-28/G-29/G-30 can no longer credit a foreign hub's step to the wrong bank.
- **Only signed batches count in the per-cycle sums.**
  - G-28's "per-cycle accumulator across bank batches" above, and those of G-05, G-06/G-32, G-29 and G-30, are
    staged while a batch is checked.
  - They are committed on PASS and dropped on VETOED, PARTLY_VETOED or TIMEOUT (`guardian/service.py:185-193`).
- **The flow signal and its sign.**
  - $L^G$ for G-28/G-29/G-30 is the bank's latest GOOD `REAL_POWER_KW`: kW, + = the bank imports from the
    feeder, − = export.
  - Without it, `APPARENT_POWER_KVA` gives only $|F|$, so the flow is the interval $[\text{export floor},\,m]$.
    The export floor comes from the guardian's own hub telemetry; with no hub read it is $-m$ (unknown
    direction = export, fail closed).
  - **Known at `r3`:** the floor is $\sum_h p_h - \sum_h \bar P^{pv}_h$ (`guardian/flow_repo.py:223-228`). No
    process writes `og.hub.pv_rated_kw`, so $\bar P^{pv}_h$ = `default_pv_rated_kw` = 0 (`guardian/config.py:108`).
    With idle batteries the floor is then ≥ 0, so rooftop-PV export is not seen on a kVA-only bank. Only
    `REAL_POWER_KW` shows it.
  - Rows stamped in the future are ignored (`guardian/flow_repo.py:8-17`, `:67`, `:73`).
- **Two `[guardian.flow]` keys:**
  - `fail_closed_missing_topology` (default **false** in R3, `true` at go-live) applies "a missing $F_f$ vetoes
    any increase" to feeders without an `og.feeder_limit` row. With `false`, the configured defaults apply
    (`guardian/flow_repo.py:274-278`).
  - `scada_min_power_factor` (default 0 = strict) narrows the kVA interval once export is ruled out
    (`orchestrator/config/orchestrator.toml:167`, `:170`).
- **K4 fail-safe re-solve.** The hubs a VETOED or PARTLY_VETOED verdict names are excluded for 3 cycles
  (`R-HUB-VETO-EXCLUDED`, traced), and the bank is re-proposed once in the same cycle without them
  (`orchestrator/src/opengrid/engine/veto.py`, `engine/__init__.py:907-928`).
  - **Known at `r3`:** the retry is proposed under cycle id `<cycle>-r1` (`engine/__init__.py:942`, `:965`).
    The per-cycle accumulators of G-05, G-06/G-32 and G-28/G-29/G-30 are keyed on the cycle id
    (`guardian/service.py:405`, `:416`, `:426`, `:529`), so a retried batch starts from fresh budgets within the
    same physical cycle.
  - `[allocator.veto_retry] enabled = false` turns the retry off (`engine/settings.py:61`, `:93`).
- **Telemetry cadence 10 s** (`integration-sims/config/fleet.yaml:19`; `orchestrator.toml` `[fleet]`
  `telemetry_interval_s = 10`, `[health]` `hub_offline_s = 60`).
  - The guardian judges its own hub telemetry stale after `[guardian] telemetry_max_age_s`, default 60 s
    (`guardian/config.py:34`, `:166`). It sets no value in `orchestrator.toml`.
  - The health classifier's stale is 2 × the interval, 20 s (`health/model.py:182-184`).
  - `[health] hub_stale_s = 25` is loaded but not read at `r3`.

**Hub telemetry additions** (interface schema change, additive): `meter_kw`, `pv_kw`, `cell_temp_c`, `p_dis_max_kw`,
`p_ch_max_kw`, `peak_budget_kws`. The guardian consumes them through its own hub-state port. The sim publishes them from its
existing household model (G11).

---

## 3. Data and inputs

| Input | Used by | Source (primary) | Fallback when unavailable |
|---|---|---|---|
| ERCOT load-zone RT prices **per zone incl. LZ_AEN, LZ_CPS** | selector, RT, settle | NP6-905-CD (`feeds/ercot.py:46`, `settlementPointType=LZ`) | last-good forecast; persistence per zone |
| ERCOT DAM SPP per zone | DA gate | NP4-190-CD (add to `PRODUCT_PATHS`) | RT-based forecast |
| AS MCPC | selector, settle | NP4-188-CD (`feeds/ercot.py:50`) | trailing 30-day median per hour |
| ERCOT solar (PVGR) forecast and actuals | solar availability, duck-curve price shape | NP4-737-CD (`feeds/ercot.py:49`, already ingested) | clear-sky × monthly clearness index |
| Sky cover / cloud, temperature per zone centroid | $S^{sol}$, $\hat T$ | NWS api.weather.gov gridpoints (hourly `skyCover`, `temperature`; `feeds/nws.py:57`) | climatology (NOAA normals) |
| Clear-sky irradiance | $S^{sol}$ shape | computed (solar geometry; a pure function in `forecast`) | — |
| TDSP delivery charges (M1) | FREE charging cost | `config/tdsp_tariffs.toml` (verified 2026-09-26) | last effective block |
| Austin Energy tariff (TOU pilot, VoS, demand charges) | REG charging cost | AE FY2026 tariff PDF; research doc §2 | `config/utility_tariffs.toml` (new) with the research values; the contract overrides |
| CPS Energy tariff | REG charging cost | CPS 2024 rate schedules (RA, PL; ELP/SLP pending) | same TOML; flag missing ELP/SLP TOU |
| Utility contract terms (capacity price, window, basis, PF, energy price, charging terms, solar floor, access flag, penalties) | selector, settle | the executed contract → `og.contract` (+ new columns) | planning values: $75/kW-yr, fixed basis 15–18 h Jun–Sep, access 0 |
| Territory polygons | K15 attribution | AE: data.austintexas.gov `i2t2-i3uy` (GeoJSON). CPS: request from CPS GIS | CPS: Bexar County plus EIA-861 county list / PUDL polygons, flagged `APPROXIMATE`, plus manual per-site override |
| Hub territory | K15 | point-in-polygon on `og.hub.lat/lon` at seed, with the utility account from install records | zip-code table; unknown means not eligible (fail closed) |
| Substations ≥ 69 kV | siting, $\sigma(a)$ | HIFLD Electric Substations | utility interconnection filings |
| Distribution substations, feeders, service transformers, ratings | F3 | utility (AE/CPS/TDSP) GIS/AMI connectivity under NDA | Base install records (transformer ID); voltage-correlation clustering from telemetry (later); clip-to-safe defaults (G-27) |
| Feeder-head and substation SCADA | F3, G-28, G-29 | utility DNP3/ICCP (`02-architecture/07`) | none: stale is a veto of increases |
| Substation asset data | C28, F3 | Base: P, E, η, SoC window, aux load, POI, transformer, ramp, warranty throughput, capex | defaults §1.2 (flagged) |
| Per-home export limit, service rating, PV rating | F2 | Base install records (interconnection agreement, PUCT §25.211 in competitive areas; AE/CPS DG agreements) | min($P^{cont}$, 10 kW); 38.4 kW; PV 0 unless known |
| Derating curves, BMS limits, peak rating | F1, F5 | Base (BMS datasheet); hub telemetry | defaults §1.9; peak = continuous |
| Home net load | F2, C8 | telemetry `meter_kw − p_kw` | ogsim household model in sim; the fleet P10/P90 diurnal |
| Capex, incentives, O&M | §4 | Base finance | $7k per 11 kW unit, $14k per 20 kW dual unit; substation $1,000/kW (2 h), $1,500/kW (4 h); O&M 1% capex (assumptions) |

---

## 4. Economic reporting ($/kW-in vs $/kW-out)

**Scopes.** Contract $c$, market $m$, fleet. Period $P$ (settlement month); annualisation $Y_P=8760/\text{hours}(P)$.

**kW bases.**

- **Asset:** $K_a=P^{cont}_a$ (installed).
- **Contract:** $K_c=\sum_{t\in P}\hat y_{c,t}\Delta_t/\sum_{t\in P,\,active}\Delta_t$ (time-weighted committed or reserved kW).
- **Market:** $K_m=\sum_aK_a\,\theta_{a,m}$, where $\theta_{a,m}$ is the share of $a$'s reserved-or-granted kW·h in $P$ that went to market $m$.
  $\theta_{a,\mathrm{REG}}=1$ for territory assets without access.
- **Fleet:** $\sum_aK_a$.

**$-in (charging cost)** per asset:

$$C^{in}_a=\sum_{t\in P}\Big[g^{grid}_{a,t}\big(\mathbb 1_{C}(\lambda^{RT}_{z(a),t}+w_{\tau(a)})+\mathbb 1_{u}c^{u,grid}_t\big)+g^{sol}_{a,t}c^{sol}_{a,t}\Big]\Delta_t+D_a c^{dem}_a+\text{fixed tariff charges}_a$$

This is reported in five components: energy, delivery (M1), demand, solar, fixed. Metered kWh come from the new
`og.charge_ledger`.

**Attribution to contracts** uses the stored-energy average cost, which replaces the discharge-time price (G4):

$$\bar c_a=\frac{C^{in}_a}{E^{dis}_a+\eta^d_a\,(e_{a,end}-e_{a,start})},\qquad C^{in}_c=\sum_a\bar c_a\,E^{dis}_{a,c}$$

$E^{dis}_a$ is all AC kWh discharged in $P$, including HOME self-serve, which is attributed to HOME and excluded from $-out.
The change in the energy inventory is valued at $\bar c$, so period boundaries don't distort.

**$-out (third-party revenue):**

$$R^{out}_c=\underbrace{\sum\text{CAPACITY\_PAYMENT}\times PF}_{\text{capacity}}+\underbrace{\sum\text{ENERGY}}_{v^E\text{ or ERCOT shadow}}+\sum\text{AVAILABILITY}+\underbrace{\sum r\,\mu}_{\text{AS}}-\sum\text{LD\_PENALTY}-\sum\text{BUYBACK}$$

These are all `og.invoice_line` types (`migrations/0001_init.sql:252-253`). FREE headroom revenue is
$\sum_t\lambda^{RT}_{z}\times$ metered export kWh, in the new `og.headroom_pnl`.

**Wear, O&M and net:**

$$W_c=\sum_ac^{deg}_aE^{dis}_{a,c}\quad(\text{D8, the same function as the selector}),\qquad OM_c=\sum_a om_a\,K_a\,\theta_{a,c}$$

$$N_c=R^{out}_c-C^{in}_c-W_c-OM_c,\qquad \text{in}_c=\frac{C^{in}_cY_P}{K_c},\ \ \text{out}_c=\frac{R^{out}_cY_P}{K_c},\ \ n_c=\frac{N_cY_P}{K_c}\quad[\$/\text{kW-yr}]$$

Market and fleet figures are sums over their contracts and headroom pseudo-scopes, divided by $K_m$ and $\sum K_a$.

**Payback:**

- Simple: $PB_s=I_s/(N_sY_P)$, with $I_s=\sum_a\text{capex}_a\theta_{a,s}$.
- Effective (Base framing): $I^{eff}_s=I_s-\text{incentives}_s$, e.g. the AE $500 rebate per home.
- Discounted: $\min\{L:\sum_{y=1}^{L}N_y/(1+r)^y\ge I\}$.
- Lifetime value: $\sum_{y=1}^{L}N_y/(1+r)^y-I$ for $L=5$ and $L=15$ (addendum §3b).
- Reported alongside the hardware view ($7,000/11 kW ≈ $636/kW) and the **3-year target flag** ($PB>3$ is shown amber).

**Settle mapping.**

| Table | Change | Holds |
|---|---|---|
| `og.charge_ledger` (new) | asset_id, interval, source (SOLAR/GRID_REG/GRID_FREE/PV_BTM), kWh, energy_usd, delivery_usd, tariff_ref, market | $C^{in}$ components |
| `og.pnl` (extend) | + market, asset_id, charging_cost_alloc (replaces the `energy_cost` semantics), delivery_charge_alloc, capacity_revenue, energy_revenue, as_revenue. `degradation_cost` = $W$ under D8 | per obligation and interval |
| `og.headroom_pnl` (new) | asset_id, interval, market, kWh out, revenue, charging_cost_alloc, wear | FREE headroom (no obligation id; `og.pnl.obligation_id` is NOT NULL) |
| `og.econ_rollup` (new) | period, scope_kind (CONTRACT/MARKET/FLEET/ASSET), scope_ref, kw_basis, the in/out/wear/O&M/net totals and per-kW-yr values, capex, incentives, payback simple/discounted, NPV5/NPV15, method_version | the profitability screen |
| `og.asset_finance` (new) | asset_id, capex_usd, incentives_usd, om_usd_per_kw_yr, commissioned_at, life_years | $I$, $OM$ |

---

## 5. Complexity and performance

**Counts.** Let $A=|\mathcal B|+|\mathcal S|$, $T=96$, $\Omega=3$, $K=2$ AS products, and $O$ obligations with $\bar A_o$ eligible assets and
$\bar W$ window intervals.

| Block | Variables | Rows |
|---|---|---|
| Per $(a,t,\omega)$: $e,g^{sol},g^{grid},d^F$ (+$\sigma$) | $4$–$5\,AT\Omega$ | ~10 $AT\Omega$: C1, C5′ (1 + 2 SoC segments), charge + taper, C3′ ×2, F2 export and import, bank ± |
| First stage $r$, $\bar y$ | $KAT+\sum_o\bar A_o\bar W$ | $\sum_o\bar W$ (C12′) |
| Need-basis $y$, $z$ | $\Omega\sum_o\bar A_o\bar W$, $\Omega O\bar W$ | $\Omega O\bar W$ |
| Feeders, substations, territory | — | $(|\mathcal F|+|\Sigma|)T\Omega$ |
| Solar floor, lexicographic | $\Omega\cdot\#REG$ contracts | $\Omega\cdot\#REG+1$ |

**At 2,000 hubs** (40 banks + 4 substation assets × 96 × 3): about **55k continuous variables and 126k rows** (measured in the
prototype: 55,216 / 126,004). With 2 AS products, the self-serve split and 20 obligations, the estimate is about 80k
variables and 150k rows.

**LP or MILP.**

- It stays an **LP** except for:
  - the product-rule binaries and integers (`02a` §3.6; tens);
  - C14 exclusivity for **substation assets only**, at negative-price scenario intervals, and only when FREE-eligible
    ($\le|\mathcal S|\cdot T_{neg}\cdot\Omega$, about 120 at 4 assets and ~10 negative intervals).
- Banks need no C14 binary: an aggregated bank can charge some hubs while discharging others. Wear on discharge plus
  $\eta<1$ make pointless cycling unprofitable except at deeply negative prices, where it is physically real.
- Duck-curve midday negatives make C14 matter more often, which is why its scope is limited to single-PCS assets.

**Measured** (prototype, this dev machine, HiGHS 1.15.1 single thread, dual simplex, LP): build 0.7 s, stage R 4.8 s,
stage F 10.3 s. That is **about 16 s in total, against the 30 s KPI** (40 banks × 96 × 3, `02a` §3.7 / FR-DE-051). It is within
target, but with only ~2× headroom. The MILP adds B&B on about 100 binaries, typically a few seconds with a MIP start.

**Aggregation and speed levers, in order:**

1. **Banks, never hubs**, in the selector. Hubs appear only in RT water-fill (unchanged).
2. **Stage R on the REG sub-model** ($\mathcal A^u$ plus coupling rows only: 14 of 44 assets in the prototype's scale case),
   so roughly 3× smaller than stage R on the full model (not yet measured).
3. **Warm starts:** stage F from stage R's basis (done in the prototype); ID gates from the previous plan shifted
   (`02a` §3.7).
4. **Pool unconstrained banks** that share (zone, territory, TDSP, feeder, market access) into one partition (`03` §6.2).
   Constrained banks and substation assets are never pooled.
5. **Hourly resolution after 6 h** (96 → 42 intervals, −56%).
6. **IPM without crossover** for stage F at 10k hubs (100 banks, estimated 35–45 s simplex).

The fallback remains F1 → F2 → F3 (`03` §6.9).

---

## 6. Impact on the invariants (K1–K14) and new K15

| ID | Impact |
|---|---|
| K1 | "Homeowner reserve" becomes "**asset floor**": homes keep the 20% reserve (Base-confirmed); substation assets get a per-asset floor (default 20%). G-01 is generalised. A stale SoC still means zero discharge |
| K2 | One buyer **across markets**: a kW or kWh is REG or FREE, never both in one interval. The ledger carries the market tag. Unchanged in form |
| K3 | Substation asset commands are guardian-signed. The PCS or the `scada-gateway` must verify signatures (go-live condition) |
| **K4** | **Extended** (text below). The invariants checker measures each part |
| K5 | A utility's operating instruction in its territory is L2 (hard). The utility's capacity *dispatch call* under a REG contract is a T1 contract call, not L2 |
| K6–K8, K10–K12 | Unchanged. K7: a substation asset's lease expiry falls back to its own local schedule. Above-continuous leases are ≤ $\tau^{pk}$ (F5) |
| K9 | New loops: the substation POI limiter (PI, one per substation quantity); the feeder limiter is a clamp (no integrator). The DC fast loop stays P-only |
| K13 | The energy form is extended: the AS hold $H_k$ (Frank #6) and need-basis energy go into C3′ and the engine S6 hold floor, measured by the checker (`CHECK_AS_HOLD`); it is not a guardian check. The lock applies across markets. Best effort per D10 |
| K14 | The substation PCS is a single inverter ($N=1$, no √N diversity, `06` §3.2). Its envelope is checked on its own characterisation |
| **K15 (new)** | **Territory** (text below) |

**K4, extended text (proposed for `00-invariants.md`).** Per hub, asset, service transformer, bank, feeder, substation
and territory boundary, charge and discharge flows stay within their limits in **both directions** (reverse flow included):

- (a) hub or asset power ≤ the derated $P_{max}(SoC,T)$ at the continuous rating; the peak rating only within its duration
  budget, and with a lease no longer than the peak duration;
- (b) per-home meter export ≤ the interconnection export limit, and import ≤ the service rating, net of the home's own load;
- (c) service transformer, bank and feeder loading ≤ ratings; no reverse flow at feeder heads or substations unless the
  utility confirmed bidirectional settings;
- (d) substation asset POI import/export and substation-transformer limits;
- (e) hub, asset, fleet and feeder ramps, with no synchronized steps.

Enforced at selector (C5′, F2/F3 rows) → allocator (per-hub, group, feeder and substation caps) → guardian G-02/03/04/05/06/26/27/28/29/30/31/32
→ hub firmware (BMS current limits, UL 1741 SB export limiter). Fail-safe: VETO; re-solve without the vetoed items.

**K15 — territory (new).** Three parts:

- A REG(u) obligation is reserved, granted and delivered only by assets inside utility $u$'s service territory.
- An asset inside a regulated territory takes FREE (ERCOT) opportunities only if $u$'s contract grants wholesale access.
- Base's net injection at every boundary substation of $u$ stays ≤ 0 (energy is consumed inside the territory). Charging is
  priced and settled under the asset's own territory tariff.

Principle P2. Enforced at contracts admission → selector C25 → allocator eligibility → guardian G-33 (market segregation) + G-30 (territory export) → settle
tariff attribution. Fail-safe: VETO the item; a shortfall is recorded against the same obligation (K13 best effort).
Proven by a property test over random fleet and contract mixes, and by the checker counters.

**Invariants checker (`opengrid/invariants/`)** gains post-hoc measurements from persisted telemetry, SCADA and grants. Today
it measures only K1, K2, K13 and K11 (`invariants/models.py:12-20`). New check names:

| Check | Measures | Data |
|---|---|---|
| `K4_HUB_POWER_DERATED` | $|p_i|>P_{max}(SoC,T)$ + tol on any sample | telemetry (`p_kw`, `soc_kwh`, `cell_temp_c`) |
| `K4_METER_EXPORT` | $-M_i>X^{exp}_i$ or $M_i>S^{svc}_i$ | telemetry `meter_kw` |
| `K4_XFMR_LOADING` | $|\sum_{i\in x}M_i|>S_x$ | telemetry per group |
| `K4_BANK_KVA` | bank SCADA outside ±rating (no checker exists today for G-03) | SCADA |
| `K4_FEEDER_FLOW` | feeder head $<-R^{rev}$ or $>F_f$ | SCADA |
| `K4_SUBSTATION_POI` | POI or substation transformer outside limits | SCADA / asset meter |
| `K4_PEAK_DURATION` | a run above continuous longer than $\tau^{pk}$ | telemetry |
| `K15_TERRITORY_MARKET` | any grant or meter interval with territory ≠ obligation territory, or FREE from a no-access territory | `og.grant`, `og.hub`, `og.contract` |
| `K15_TERRITORY_EXPORT` | boundary substation net export > 0 attributable to Base | SCADA + grants |
| `K13_ENERGY_HOLD` | SoC below the hold floor while an AS award or firm energy was held | telemetry + ledger |

These go into `og.invariant_violation` (migration 0014's check-name domain is extended) and the live counters (A-series
dashboard).

---

## 7. Implementation plan

**Ownership** follows the build-team rule: one owner per function in `og.core`; architect and coders build; QA and security
validate.

| WP | Owner | Content | Depends on |
|---|---|---|---|
| **WP-2M-01** zone pricing fix (Frank #5) | selector + forecast | `ScenarioPrice` keyed by `series_key`, `kind=='price'` only; `AssetSnapshot.zone`; $v^E_{a,t,\omega}=\lambda_{z(a)}$; RT `EngineScheduleGateway` per zone (G1, G2) | — |
| WP-2M-02 core functions | architect (core) | `core.tariffs.charging_cost(asset, source, t)` (TDSP + utility TOML); `core.economics.wear_cost`; `core.physics.p_max_derated(soc, T, curves)`, peak rule; `core.limits` checks for F2–F5 and F7 (one formula for allocator, guardian and checker) | — |
| WP-2M-03 assets and territory | fleet/assets | `og.asset`, substation class, territory attribution (point-in-polygon), transformer and feeder registry, telemetry fields, horizon capability with temperature | 02 |
| WP-2M-04 contracts | contracts | market, basis, REG terms, access flag, solar floor; admission territory check `R-TERRITORY-INELIGIBLE` | 03 |
| WP-2M-05 selector v2 | selector | §2.1: C3′ with $r$, C5′, C7(b)′, C12′/″, C25, C27, C28, F2/F3 rows, lexicographic solve, validator, plan outputs ($\nu$, $e^{hold}$, $e^{plan}$) | 01–04 |
| WP-2M-06 allocator | allocator | §2.3/§2.4: territory filter, derated and export caps, transformer group caps, feeder and substation budgets, water-value S6 and hold floor, charging by source, substation setpoint, POI PI, peak leases | 02, 03, 05 (plan outputs) |
| WP-2M-07 guardian | guardian (security validates) | G-02 change, G-26…G-33 (as built; no AS-hold check), group-level veto semantics, own-read ports (telemetry fields, feeder and substation SCADA, territory tables) | 02, 03 |
| WP-2M-08 settle economics | settle | charge ledger, stored-energy cost attribution (fixes G4), wear unification (fixes G3), headroom P&L, econ rollup, payback | 02, 04 |
| WP-2M-09 invariants checker | invariants (QA) | §6 check names | 03, 07 |
| WP-2M-10 sims | sims (independent, not orchestrator) | AE-territory banks on LZ_AEN; a substation asset sim (20 MW/2 h); telemetry fields from the household model; per-hub temperature; transformer groups; feeder and substation SCADA; duck-curve price replay | parallel from day 1 |
| WP-2M-11 UI | ui | $/kW-in vs $/kW-out view per contract, market and fleet; 3-year flag | 08 |

**Ordering.** 01 → (02 ∥ 10) → (03 ∥ 04) → (05 ∥ 07) → 06 → (08 ∥ 09) → 11. WP-01 is a bug fix and should land first,
independently.

**Minimal viable for the regulated pilot (MVP-R, AE, home batteries):**

- WP-01 (zone pricing), and D8 (the wear rule) in the selector and settle;
- K15 end to end: C25 in the selector, the allocator filter, G-33 and G-30, `K15_*` checks and the admission check;
- REG contract, fixed basis, AE TOU charging (no M1) and M1 on FREE charging; the soft solar floor with availability from
  NWS plus ERCOT PV;
- C3′ with AS variables, the engine S6 hold floor and the checker's `CHECK_AS_HOLD` (Frank #6). Existing `ERCOT_AS` needs it regardless;
- lexicographic stages R and F; the RT water-value threshold and hold floor;
- flow limits for homes: F1 (G-02 change), F2 (G-26), F3 transformer (G-27 with clip-to-safe defaults where mapping is
  missing), F3 feeder (G-28), F5 (G-31, trivial while peak = continuous), and the `K4_*` checker measurements;
- $/kW reporting (charge ledger, rollup, UI).
- Migrations 0019–0022 (below) in full, so that "later" needs no schema rework.

**Later:** substation assets live (C28, G-29, POI PI; the data model already in 0020); need-basis REG and DATA_CENTER on
substation assets; a standalone SCED LP; demand charges; CPS; 4 h assets; CVaR release; C18 cycle budget; hourly tails;
voltage-correlation transformer inference; AE ERCOT access if granted (OQ-5).

**Stories (epic ES19 — two-market model and flow limits).**

| Story | As / I want / so that | Acceptance |
|---|---|---|
| ES19-S01 | selector: per-bank zone price | a 4-zone fixture prices each bank at its zone; no load rows leak into prices (TS-19-01, 02) |
| ES19-S02 | contract: market and basis | admission rejects REG obligations with out-of-territory assets (TS-19-03) |
| ES19-S03 | selector: territory C25 | no REG reservation outside the territory; no FREE for no-access assets (TS-19-04, 05) |
| ES19-S04 | selector: lexicographic R/F | a scarcity-spike fixture never shrinks the REG tranche below $(1-\varepsilon)z_R^*$; forgone upside reported (TS-19-06, 07) |
| ES19-S05 | selector: charging sources, M1, solar floor | FREE grid kWh cost $\lambda+w$; AE kWh cost TOU; solar ≥ 30% or slack reported (TS-19-08, 09, 10) |
| ES19-S06 | selector: AS hold C3′ (Frank #6) | a Non-Spin award always has $4r/\eta_d$ above the floor in every scenario (TS-19-11) |
| ES19-S07 | selector: fixed vs need basis, best effort | the need reservation is never resold; the shortfall target stays $Q$ (TS-19-12, 13) |
| ES19-S08 | wear rule (Frank #7) | selector expected wear = settle wear on identical kWh, to $10^{-9}$ (TS-19-14) |
| ES19-S09 | RT: water-value threshold and hold floor | no headroom discharge below $\nu$ or below $e^{hold}$ (TS-19-15, 16) |
| ES19-S10 | RT: territory filter | AE hubs never serve FREE without access (TS-19-17) |
| ES19-S11 | settle: $/kW-in/out, payback | fixture month reproduces hand-computed values; §3c stack reproduced (TS-19-18, 19) |
| ES19-S12 | F1 derating (allocator + G-02) | TS-19-20, 21, 22 |
| ES19-S13 | F2 home export (allocator + G-26) | TS-19-23, 24, 25 |
| ES19-S14 | F3 transformer, feeder, substation (allocator + G-27/28/29) | TS-19-26 … 30 |
| ES19-S15 | F4 territory boundary and segregation (G-30/G-33) | TS-19-31, 32 |
| ES19-S16 | F5 peak vs sustained (G-31) | TS-19-33, 34 |
| ES19-S17 | F6 substation and non-firm feeder ramp (G-04, G-32); F7 hold (selector, engine, `CHECK_AS_HOLD`) | TS-19-35, 36, 37 |
| ES19-S18 | checker K4_*/K15_*/K13_ENERGY_HOLD | TS-19-38, 39, 40 |
| ES19-S19 | performance at scale | TS-19-41 |

**Tests (TS-19-xx).** P = property, N = negative, U = unit, S = scenario, B = benchmark.

| ID | Kind | Test |
|---|---|---|
| TS-19-01 | U | `load_scenarios` keeps one price path per zone; banks in 4 zones get 4 different prices |
| TS-19-02 | N | load-kind rows are never used as prices |
| TS-19-03 | N | an AE REG contract with an Oncor bank in its eligible set is rejected `R-TERRITORY-INELIGIBLE` |
| TS-19-04 | P | random fleets and contracts: $\bar y_{o,a,t}=0$ for $a\notin\mathcal A^{u(o)}$ |
| TS-19-05 | P | $d^F=r=0$ for territory assets with access 0 |
| TS-19-06 | S | P90 $650/MWh evening: the REG tranche is unchanged vs P50-only (±ε) |
| TS-19-07 | U | the stage-F row enforces $\ge(1-\varepsilon)z^*_R$; `forgone_upside` equals the relaxed-minus-locked value |
| TS-19-08 | U | FREE grid charging coefficient = $\lambda+w_\tau$ per TDSP; BTM PV has no $w$ |
| TS-19-09 | U | AE charging coefficient = TOU period rate; no $w$ |
| TS-19-10 | S | a cloudy day gives solar slack > 0, reported; the month-to-date carry restores ≥ 30% over the month |
| TS-19-11 | P | $e_{t-1},e_t\ge\sum_kH_kr/\eta_d$ in every scenario |
| TS-19-12 | P | need-basis: $p^{claim}$ counts $\bar y$ in full; FREE never uses it |
| TS-19-13 | S | mid-window shortfall: the next gate keeps $Q$; delivery restored at the first feasible interval |
| TS-19-14 | P | `wear_cost` is identical in the selector objective evaluation and settle |
| TS-19-15 | U | S6 threshold = $\nu$ + hysteresis; $30 is not used |
| TS-19-16 | P | S6 never takes SoC below $\max(e^{hold},e^{plan})$ unless $\lambda\ge\nu_{ramp}$ |
| TS-19-17 | P | the allocator's eligible set for REG obligations ⊆ the territory |
| TS-19-18 | U | $\bar c_a$ attribution and inventory valuation on a hand fixture |
| TS-19-19 | S | the §3c single-unit stack reproduces ≈ $1,620/yr net before scarcity |
| TS-19-20 | P | G-02: PASS ⇔ $|p|\le P_{max}(s,T)$; the allocator cap ≤ the guardian bound on identical inputs |
| TS-19-21 | N | G-02: stale temperature → bound × 0.5; 45 °C → 0.7 |
| TS-19-22 | S | afternoon heat: the plan lowers the 15–18 h capacity by the forecast derating |
| TS-19-23 | P | G-26: PASS ⇔ export and import inequalities |
| TS-19-24 | N | G-26: stale meter → export bound $X-PV^{rated}$; unknown $X$ → no export |
| TS-19-25 | S | discharge first covers home load; only the excess is exported (sim meter) |
| TS-19-26 | P | G-27 group verdict equals brute force; group-level PARTLY_VETOED |
| TS-19-27 | N | G-27: midday PV reverse flow > $S_x$ vetoed; >20% stale members + increase vetoed |
| TS-19-28 | N | G-28: two individually safe bank batches jointly reversing the feeder are vetoed |
| TS-19-29 | N | G-28: stale feeder SCADA vetoes increases, passes relief |
| TS-19-30 | N | G-29: POI export > limit vetoed; stale substation SCADA vetoes increases |
| TS-19-31 | N | G-33: an AE hub on an Oncor obligation is vetoed; an AE hub on FREE headroom with access 0 is vetoed |
| TS-19-32 | P | G-30/C25(c): Base net injection at territory boundary substations ≤ 0 in all fixtures |
| TS-19-33 | N | G-31: a lease > $\tau^{pk}$ above continuous is vetoed; a missing budget blocks above continuous |
| TS-19-34 | S | DC bridge uses the peak for ≤ $\tau^{pk}$, then steps down on lease expiry |
| TS-19-35 | N | a substation asset step > $\rho_s\Delta t$ is vetoed (G-04 generalised) |
| TS-19-36 | P | allocator S6 + `CHECK_AS_HOLD`: headroom never erodes the AS/firm hold |
| TS-19-37 | N | Non-Spin 100 kW with 350 kWh above the floor: the allocator grants no headroom discharge, and a seeded violation is flagged by `CHECK_AS_HOLD` |
| TS-19-38 | U | the checker flags each seeded K4 violation class exactly once |
| TS-19-39 | U | the checker flags K15 market and export violations |
| TS-19-40 | U | the checker flags `K13_ENERGY_HOLD` |
| TS-19-41 | B | 40 banks + 4 substation assets × 96 × 3: both stages ≤ 30 s p95 on the base node; 100 banks with levers ≤ 45 s |

**Migrations (additive only, next free number 0019).**

| Migration | Content |
|---|---|
| `0019_market_territory.sql` | `og.territory` (utility_code AE/CPS/ERCOT_COMPETITIVE, market_kind, ercot_load_zone, geometry, source, APPROXIMATE flag, version); FK from the existing `og.contract.territory_id` (`0001_init.sql:21`); `og.contract` + market, utility_code, commitment_basis, capacity_price_usd_per_kw_yr, energy_price_usd_per_kwh, solar_share_floor (default 0.30), charging_tariff_ref, free_access_granted (default false); `og.bank` + territory_id, tdsp, substation_id; `og.hub` + territory_id, export_limit_kw, service_rating_kw, pv_rated_kw, xfmr_id, p_peak_kw, peak_duration_s; `og.service_transformer`, `og.feeder` (rating, reverse_flow_allowed), `og.substation` (rating, reverse_flow_allowed, territory_id) |
| `0020_assets_substation.sql` | `og.asset` (asset_class HOME_BANK / SUBSTATION_BESS, e_kwh, p_cont_kw, p_peak_kw, peak_duration_s, eta_c, eta_d, floor_frac, aux_kw, poi_import_kw, poi_export_kw, xfmr_kva, ramp_kw_per_min, wear_usd_per_kwh, zone, territory_id, substation_id); `og.derate_curve` (asset_class, kind SOC/TEMP, direction, breakpoints jsonb, version) |
| `0021_telemetry_flow_fields.sql` | `og.telemetry` and `og.hub_state` + meter_kw, pv_kw, cell_temp_c, p_dis_max_kw, p_ch_max_kw, peak_budget_kws; SCADA signal kinds FEEDER_HEAD_KW, SUBSTATION_XFMR_KVA, POI_KW |
| `0022_settle_economics.sql` | `og.charge_ledger`, `og.headroom_pnl`, `og.pnl` + market, asset_id, charging_cost_alloc, delivery_charge_alloc, capacity_revenue, energy_revenue, as_revenue; `og.econ_rollup`; `og.asset_finance`; extend `og.invariant_violation` check names |

Configuration: `config/utility_tariffs.toml` (AE FY2026 TOU pilot, VoS, demand; CPS 2024 RA/PL), `config/derate_curves.toml`, and
`fleet.yaml` gets AE-territory banks on LZ_AEN plus a substation asset for the sims.

---

## 8. Open questions for Base or the owner (with proposed defaults)

| # | Question | Proposed default |
|---|---|---|
| OQ-1 | Substation asset duration and capex | 20 MW / **2 h**; $1,000/kW. A 4 h asset at $1,500/kW is the sensitivity (§9: 2 h firms only ~11 MW of a 3 h window) |
| OQ-2 | Substation SoC window, floor, RTE, aux load, warranty throughput | floor 20%, RTE 0.88, aux 0.5% of $P$, throughput per warranty |
| OQ-3 | REG product: window, months, basis, performance factor, measurement point | fixed basis, weekdays 15:00–18:00, Jun–Sep, PF = delivered/committed at the meter/POI, net of charging |
| OQ-4 | Energy price paid for REG deliveries | contract value; **none assumed** in production planning. 12.88 ¢ (VoS) only to reproduce §3c. It is a solar export credit, not a storage tariff |
| OQ-5 | **May REG-territory assets sell into ERCOT (LZ_AEN/LZ_CPS)?** The prototype shows AE home-bank payback of **6.1 y without access vs 3.0 y with it**. It is the main lever for the 3-year target | **No** (K15(b) default 0), until the utility contract says otherwise |
| OQ-6 | Base's charging tariff in AE territory. The TOU pilot is a residential retail rate for consumption; reselling retail kWh may be barred | a contract-specific charging rate; the planning default is the TOU power supply + adders (all-in 4.1–4.4 ¢) |
| OQ-7 | What counts as "solar" for the 30% floor, its accounting period, and whether it is hard | a utility solar PPA or allocation under the contract; monthly; soft with a green-premium slack. Rooftop PV in AE costs the VoS credit, so it rarely wins |
| OQ-8 | Per-home export limits and service ratings | install records; default min($P^{cont}$, 10 kW), 200 A → 38.4 kW continuous |
| OQ-9 | Battery chemistry, derating curves, and BMS limits in telemetry | LFP; the §1.9 curves; hubs report `p_dis_max_kw` |
| OQ-10 | Peak vs continuous inverter rating and duration | peak = continuous (F5 inactive) |
| OQ-11 | Transformer-to-meter mapping and ratings; feeder ratings; reverse-flow permissions | utility data under NDA; clip-to-safe defaults; **no reverse flow** at feeder heads and substations |
| OQ-12 | Delivery-charge treatment of substation assets in competitive areas (WSL vs distribution) | full volumetric M1 until confirmed |
| OQ-13 | Data-center locality | the same substation's assets first, then home banks on its feeders |
| OQ-14 | Does new REG capacity always dominate FREE at a gate? | yes (lexicographic), $\varepsilon$ = 0.1%, with forgone upside reported |
| OQ-15 | Wear rate per class; the conflict with §3c | homes $0.03/kWh (A-DE-16), substation $0.015/kWh; replace both with the warranty throughput curve |
| OQ-16 | AE/CPS as QSE for any wholesale participation; ADER in NOIE zones (0 MW approved per claims check #15) | no ERCOT AS from NOIE-territory assets |
| OQ-17 | The CPS territory polygon | request from CPS GIS; approximate county-based polygon flagged until then |
| OQ-18 | Ramp and fleet-cap treatment of a 20 MW asset (G-05 is sized 50/10 MW/min for the home fleet) | per-asset ramp 10 MW/min non-firm, with its own G-05 budget |

---

## 9. Prototype results (`prototypes/two_market_lp.py`)

The prototype is a standalone LP in highspy + numpy. Its toy instance:

- **Home banks:** B1 and B2 in AE territory (no ERCOT access); B3 in Oncor/LZ_NORTH; B4 in CenterPoint/LZ_HOUSTON. Each bank
  is 50 homes (40 × 39.2 kWh/11 kW + 10 × 78.4 kWh/20 kW), with a 20% floor and 600 kVA.
- **Substation asset:** S1 in AE territory, 20 MW.
- **Prices:** a synthetic duck curve; Non-Spin on the FREE banks.
- **Regulated contract:** a REG candidate at $75/kW-yr, fixed basis, 15:00–18:00.

It exercises the lexicographic stages, C3′, C5′ derating, F2/F3, C25(c) and the solar floor. It runs 96 intervals × 3
scenarios and annualises at 300 cycling days. Every number is an assumption from §1.2 and addendum §3c.

| Case | REG q (stage R) | Payback, AE home banks | FREE banks | S1 | Fleet |
|---|---|---|---|---|---|
| S1 2 h, no AE access | **11.1 MW** (energy-limited: 32 MWh above floor / 3 h) | 6.1 y | 6.9–7.2 y | 15.9 y | 14.3 y |
| S1 4 h, no AE access | **19.8 MW**, limited by **C25(c) no reverse flow** at the AE substation at 15:00 | 13–14 y (S1 takes the capacity) | 6.9–7.2 y | 11.9 y | 11.7 y |
| S1 2 h, **AE ERCOT access** | 11.1 MW | **3.0 y** | 6.9–7.2 y | 6.3 y | 6.0 y |

Findings that feed the spec:

1. **Wash trades.** Without C7(b)′, the LP charged S1 at the 8.44 ¢ on-peak TOU while delivering at 12.88 ¢, and "delivered"
   60 MWh/day from a 40 MWh asset. D13 fixes this: no charging in the delivery window, and net metering.
2. **The territory constraint binds.** With a 4 h asset, no-reverse-flow at the boundary substation caps REG capacity.
   F3/F4 are economic constraints, not only safety constraints.
3. **The M1 charge sets the FREE water value** at ~$82/MWh at night (Oncor), vs ~$35 energy. The fixed $30/MWh RT
   threshold (G9) would discharge energy worth ~$80 for $30.
4. **The 3-year payback** is reached here only with ERCOT access for AE-territory assets (OQ-5). Otherwise it is 6–7 y for
   home banks and longer for a 2 h substation asset. This is illustrative, not a forecast; Base's own formula is still
   needed (addendum §4 #5).
5. **Scale:** 55k variables and 126k rows solve in about 16 s total for both stages (§5).

Run it with:

`D:\Projects\OpenGrid\opengrid-orchestrator\.venv\Scripts\python.exe prototypes\two_market_lp.py [--duration 4] [--ae-free-access] [--scale]`

---

## 10. Changes needed in other documents (not edited here)

| Document | Change |
|---|---|
| `00-invariants.md` | K4 extended text (§6); K13 energy: AS hold $H_k$ and need-basis energy; **K15 territory**; guardian numbering G-26…G-33 canonical as built (the AS hold is not a guardian check); retire `02a`'s supplemental G-21…G-31 |
| `02a-mvp-s-spec-engine.md` | §3.2 sets and parameters (assets, markets, $w$, $v^E$ per zone); §3.3 C3′/C5′/C7(b)′/C12′/C25–C28; §3.4 lexicographic objective, wear rule; §3.7 two-stage solve times; §5.1 S2/S4/S6 changes (water value, holds, flow caps, territory); §6.1 guardian table (G-02 change, G-26…G-33; remove the supplemental table); §7.4 profitability → §4 of this spec |
| `02-architecture/03-decision-engine.md` | §6.2–§6.6 market dimension and constraints; §6.3 derating, export and ratings parameters; §8.10 flow limits F1–F7; §10.6 settlement economics; §12.4 KPIs ($/kW-in/out, payback, forgone upside from REG priority) |
| `06-service-profiles-and-power-quality.md` | DATA_CENTER need basis on substation assets and locality; substation PCS PQ ($N=1$); peak-rating use in the DC fast loop |
| `08-market-model-two-markets.md` | §3b formulas → §4 here; §3c: add the wear-rate conflict and the prototype results; OQ-5 (ERCOT access) as the key question |
| `03-mvp-s-epics-stories.md`, `04-mvp-s-test-plan.md` | Add ES19 and TS-19-01…41 |
| `05-integrations-guide.md` | NP4-190-CD per zone incl. LZ_AEN/LZ_CPS; NWS gridpoint `skyCover`; AE GIS layer; CPS GIS request; utility SCADA points for feeders and substations |
| `integrations/tdsp-tariffs-2026-09.md` | State that M1 applies only in the competitive area and to grid-drawn kWh (D5) |
| `interfaces/` telemetry schema, `02b` §4/§12 | New hub telemetry fields; `AssetSnapshot`; horizon capability with temperature; the `core` owners for the tariff, wear and derating functions |

---

## 11. Market model as built (2026-09-26)

Owner: MARKET-MODEL. This section records what was built for the two-market model, the interfaces the
selector, allocator, guardian, settle and invariants checker call, the K15 text for `00-invariants.md`, and
the profitability route. It supersedes the migration plan in §7 for the market and asset tables: the
numbers 0019–0022 listed there were taken by other work, and the market model landed as **migration 0025**.

### 11.1 Scope and files

| Item | Where |
|---|---|
| Migration (additive) | `orchestrator/migrations/0025_market_model.sql` |
| Row shapes and vocabulary | `orchestrator/src/opengrid/core/models/market.py` (`Market`, `UtilityId`, `Territory`, `AssetClass`, `Utility`, `Asset`); `core/models/engine.py` (`Contract.market`, `Contract.utility_id`, `ServiceType`) |
| Market package | `orchestrator/src/opengrid/market/`: `territory.py` (market membership, K15 predicate), `config.py` (utility planning terms, `[zone_territory]` loader), `charging.py` (regulated charging cost, TOU periods, blend), `free_charging.py` (ERCOT charging cost with M1), `capacity.py` (regulated capacity payment), `economics.py` ($/kW economics), `model.py` (`MarketModel` read API), `view.py` (profitability payload), `pg_backend.py` (settled-data read side, the only I/O module) |
| Dev seed (applied by hand, not by migration) | `dev/seed/market_model_seed.sql` |
| Tests | `orchestrator/tests/unit/market/` (unit and property); `orchestrator/tests/integration/test_market_model_db.py` (server) |

### 11.2 Data model (migration 0025)

| Object | Content |
|---|---|
| `og.utility` | `utility_id` (AUSTIN_ENERGY, CPS_ENERGY), name, `territory_zones` (text[]), `capacity_product`, `payment_basis` (USD_PER_KW_MONTH or USD_PER_KW_YEAR), `capacity_price_usd_per_kw`, `charging_tariff_kind` (TOU_OFF_PEAK or NIGHT_RATE), off-, mid- and on-peak rates, `charging_adder_usd_per_kwh`, `solar_cost_usd_per_kwh` (default 0.040), `solar_share_floor` (default 0.30), `free_access_granted` (default false, K15 b), `tariff_ref`, `source_note` |
| `og.contract.market` | REGULATED or FREE, NOT NULL, default FREE. Every existing contract becomes FREE |
| `og.contract.utility_id` | FK to `og.utility`. CHECK: set if and only if `market = 'REGULATED'` |
| `og.contract.service_type` | 0025 owns the full CHECK list: HOME, ERCOT_ENERGY, ERCOT_AS, DIST_DEFERRAL, PARTNER_CAPACITY, DATA_CENTER, PIPELINE_AC, **REGULATED_CAPACITY**, **PJM_CAPACITY**, **MOBILE_STORAGE**, **LARGE_LOAD**. It mirrors `core.models.engine.ServiceType`. A second CHECK makes REGULATED_CAPACITY imply `market = 'REGULATED'` |
| `og.asset` | `asset_id`, `asset_class` (HOME_BANK or SUBSTATION), `bank_id` (FK `og.bank`), `feeder_id`, `substation_id`, `zone`, `utility_id` (territory; NULL is the ERCOT competitive area), `p_kw`, `e_kwh`, `eta_rt`, `floor_frac` (default 0.20), `poi_import_kva`, `poi_export_kva`, `capex_usd`, `status` (PLANNED, ACTIVE, RETIRED). CHECKs: a HOME_BANK names its bank; a SUBSTATION names a bank or a feeder **and** both POI limits |

The planning defaults for a substation set are **20 MW / 2 h (40 MWh)**, RTE 0.88, a 20% floor, POI 20 MW
in both directions and $1,000/kW. The 4 h option is `e_kwh = 80000` at $1,500/kW.

**Territory resolution.** An asset's territory is its explicit `utility_id` if set; otherwise its zone decides
through `[zone_territory]` in `config/tdsp_tariffs.toml` (LZ_AEN is AUSTIN_ENERGY, LZ_CPS is CPS_ENERGY, any
other `LZ_*` is ERCOT_COMPETITIVE). An explicit utility that contradicts a regulated zone, an unknown zone or an
unknown bank resolves to **unknown**, which is never eligible.

### 11.3 Read API (`opengrid.market`)

Pure unless stated otherwise. The guardian imports `opengrid.market.territory` directly.
`market.capacity`, `market.charging`, `market.territory` and `market.economics` never import
`opengrid.settle`, so settle can import them without an import cycle.

```python
# opengrid.market.territory
@dataclass(frozen=True) class MarketRef: market: Market; utility_id: UtilityId | None  # REGULATED needs a utility; FREE never has one
FREE: MarketRef
def market_of(*, market: str | None, utility_id: str | None, service_type: str | None = None) -> MarketRef
    # NULL market is FREE; REGULATED_CAPACITY must be REGULATED; any inconsistency raises MarketModelError
def market_of_contract(contract: Contract) -> MarketRef
def territory_of_zone(zone: str | None, zone_territory: Mapping[str, UtilityId]) -> Territory | None
def utility_of_territory(territory: str | None) -> UtilityId | None
def check_territory(ref: MarketRef | None, asset_territory: Territory | str | None, *, free_access: bool = False) -> str | None
    # None = allowed; else R-TERRITORY-OUTSIDE | R-TERRITORY-NO-FREE-ACCESS | R-TERRITORY-UNKNOWN

# opengrid.market.model
def load_market_model(*, banks: Iterable[tuple[str, str]], assets: Sequence[Asset] = (),
                      utilities: Sequence[Utility] | None = None, config_path: str | Path | None = None) -> MarketModel
    # config_path is the OG_CONFIG path; tdsp_tariffs.toml is read from its directory
class MarketModel:
    def territory_of_bank(self, bank_id: str) -> Territory | None
    def territory_of_asset(self, asset_id: str) -> Territory | None
    def territory_bank_ids(self, utility_id: UtilityId) -> frozenset[str]
    def territory_asset_ids(self, utility_id: UtilityId) -> frozenset[str]
    def free_access(self, territory: Territory | None) -> bool
    def bank_eligible(self, bank_id: str, ref: MarketRef) -> bool
    def eligible_bank_ids(self, ref: MarketRef) -> frozenset[str]     # C25 a/b; FREE also covers headroom
    def eligible_asset_ids(self, ref: MarketRef) -> frozenset[str]
    def utility(self, utility_id: UtilityId) -> Utility
    def charging_cost(self, zone: str, interval: datetime, *, wholesale_usd_per_kwh: Decimal | None = None,
                      solar_share: Decimal | None = None) -> ChargingCost

# opengrid.market.charging / free_charging
def tou_period(utility: Utility, interval_start: datetime) -> TouPeriod
def blend(solar_share: Decimal, solar_usd_per_kwh: Decimal, grid_all_in_usd_per_kwh: Decimal) -> Decimal
def regulated_charging_cost(utility: Utility, zone: str, interval_start: datetime, *, solar_share: Decimal | None = None) -> ChargingCost
def free_charging_cost(zone: str, *, wholesale_usd_per_kwh: Decimal, tdsp_tariff: TdspTariff | None,
                       solar_share: Decimal = 0, solar_usd_per_kwh: Decimal | None = None) -> ChargingCost

# opengrid.market.capacity
def regulated_capacity_payment(*, committed_kw: Decimal, price_usd_per_kw: Decimal, basis: CapacityPaymentBasis,
                               hours: Decimal, performance_factor: Decimal = 1) -> Decimal

# opengrid.market.economics / view
def unit_economics(inputs: UnitEconomicsInputs) -> KwEconomics
def period_kw_economics(totals: PeriodTotals) -> KwEconomics
def rollup(scopes: Sequence[PeriodTotals], *, scope_kind: ScopeKind, scope_ref: str, market: Market | None) -> PeriodTotals
def stored_energy_avg_cost(*, charging_cost_usd, discharged_kwh, eta_d, energy_start_kwh, energy_end_kwh) -> Decimal | None
def illustrative_home_unit() -> KwEconomics
def profitability_per_kw(scopes: Sequence[PeriodTotals]) -> PerKwSummary

# opengrid.market.pg_backend (I/O)
async def fetch_contract_totals(pool: AsyncConnectionPool, start: datetime, end: datetime) -> list[PeriodTotals]
```

`ChargingCost` fields: `zone`, `market`, `utility_id`, `period` (OFF_PEAK, MID_PEAK, ON_PEAK, NIGHT, DAY or
WHOLESALE), `solar_share`, `solar_usd_per_kwh`, `grid_energy_usd_per_kwh` and `delivery_usd_per_kwh` (both per
grid kWh), `blended_usd_per_kwh` (per kWh charged), `tariff_ref`.

**Use by layer.**

- **Selector.** Build one `MarketModel` per gate. $\mathcal A_o$ = `eligible_bank_ids(market_of(...))`
  intersected with contract locality (C25 a/b). $c^{ch}_{a,t}$ = `charging_cost(zone(a), t, wholesale_usd_per_kwh=λ).grid_all_in_usd_per_kwh`
  and $c^{sol}$ = `.solar_usd_per_kwh`. Stage R values new REG capacity with `regulated_capacity_payment(hours=|T|Δ)`.
- **Allocator.** Pass `check_territory(call.market_ref, territory, free_access=...)` per (obligation, bank) and
  `check_territory(FREE, ...)` for headroom (S2.3 "Eligibility").
- **Guardian.** G-33 (§11.6) calls `check_territory` on its own reads.
- **Settle.** Imports `regulated_capacity_payment` and `regulated_charging_cost` / `blend` instead of keeping
  its own copies; uses `stored_energy_avg_cost` for the G4 attribution.

### 11.4 Charging cost and economics

**Charging cost per kWh charged:** $c_{in}=s\,c_{sol}+(1-s)(c_{grid}+w)$.

| Market | $s$ | $c_{sol}$ | $c_{grid}$ | $w$ (M1) |
|---|---|---|---|---|
| REGULATED (AE, CPS) | the utility's floor, 0.30 by default; a larger share is costed as given (soft floor, D4) | `og.utility.solar_cost_usd_per_kwh`, 4.0 ¢ | the utility tariff for the period plus adders. AE TOU pilot: weekdays 22:00–07:00 and all weekend off-peak at 2.677 ¢; 15:00–18:00 on-peak at 8.442 ¢; otherwise mid-peak at 4.118 ¢. CPS: the night rate, a 5.026 ¢ placeholder (OQ-6). A period without a published rate is priced at the next higher known rate, never a cheaper one | 0 |
| FREE (ERCOT) | 0 by default (the floor is a regulated-contract term) | behind-the-meter PV at its forgone export credit, by default λ | λ for the zone (the caller passes it) | the zone TDSP's volumetric charge, via `settle.tariffs` (single owner of M1). An unmapped zone is 0 and flagged in `tariff_ref` |

**Capacity payment:** committed kW × annual price × hours / 8760 × PF. A $/kW-month price is multiplied by 12 first.
PF is clamped to [0, 1].

**$/kW economics** follow §4 exactly: `PeriodTotals` (charging energy, delivery, demand; capacity, energy and
other revenue; penalty and buyback; wear, O&M, capex, incentives; kW basis; hours) gives
in/out/net $/kW-yr, simple, effective and discounted payback, NPV5 and NPV15, and the 3-year flag.
Market and fleet rows are `rollup`s of their contract scopes.

**08 §3c reproduced** (`illustrative_home_unit()`, test TS-19-19):

| Item | Computed |
|---|---|
| Capacity, $75/kW-yr × 11 kW | $825.00 |
| Charging, 3.0739 ¢ × 39.2 kWh × 300 | −$361.49 |
| Energy out, 35.28 kWh × 12.88 ¢ × 300 | +$1,363.22 |
| Degradation and O&M, 3% of $7,000 | −$210.00 |
| **Net** | **$1,616.73/yr** (§3c: ≈ 1,620) |
| $/kW-in, $/kW-out, net $/kW-yr | $32.86, $198.93, $146.98 |
| Capex per kW (hardware view) | $636.36 |
| **Payback** | **4.33 years** before scarcity upside (3-year flag: not met). Adding ~$720/yr of headroom/DR revenue reaches 3.0 years, matching §3c |

### 11.5 K15 invariant text (for the lead to copy into `00-invariants.md`)

> **K15 — Territory.** Every contract belongs to exactly one market: REGULATED(u) for a regulated utility u
> (Austin Energy, CPS Energy) or FREE (ERCOT). (a) A REGULATED(u) obligation is reserved, granted and delivered
> only by assets inside u's service territory. (b) An asset inside a regulated territory takes FREE (ERCOT)
> opportunities, including uncommitted headroom, only if u's contract grants wholesale access (default: no).
> (c) Base's net injection at every boundary substation of u stays ≤ 0. (d) Charging is priced and settled under
> the asset's own territory tariff: the utility's charging terms inside u (no TDSP delivery charge), the ERCOT
> zone price plus the TDSP delivery charge (M1) outside. An unknown territory or market is never eligible.
>
> *Principle P2. Enforced at:* contract admission (`R-TERRITORY-INELIGIBLE`) → selector C25 → allocator
> eligibility (`market.check_territory`) → guardian **G-33** (market segregation, item level) and **G-30**
> (territory export: no net reverse flow out of u's boundary substations) → settle tariff attribution. *Fail-safe:* VETO the item; a shortfall is recorded against the
> same obligation (K13 best effort). *Proven by* the property test over random fleet and contract mixes
> (TS-19-04, `tests/unit/market/test_territory.py`) and the checker counters `K15_TERRITORY_MARKET` and
> `K15_TERRITORY_EXPORT`.

### 11.6 Guardian G-33 (SAFETY)

G-33 is the canonical ID for market segregation. Territory export (no reverse flow out of a regulated
territory, K15 c) is G-30, as built.

- **Rule:** for each batch item $i$ on hub $h$ in bank $b$: `check_territory(ref, territory(b), free_access=access(territory(b)))`
  must return `None`. `ref` = `market_of(...)` of the obligation's contract; a headroom item (no obligation) uses `FREE`.
- **Own reads:** `og.contract.market`, `og.contract.utility_id`, `og.contract.service_type` via
  `og.obligation.contract_id`; `og.hub.bank_id` → `og.bank.zone` (or `og.asset.utility_id` for a substation
  asset); `[zone_territory]` via `market.config.load_zone_territory`; `og.utility.free_access_granted`. It never
  uses the allocator's eligibility list.
- **Fail closed:** a missing obligation or contract, an inconsistent market (`MarketModelError`), an unknown bank
  or zone, or a territory string that is not one of AUSTIN_ENERGY, CPS_ENERGY or ERCOT_COMPETITIVE all give
  `R-TERRITORY-UNKNOWN` → veto.
- **Veto:** item level (in `_ITEM_LEVEL_RULES`); the verdict is PARTLY_VETOED when other items remain. The
  vetoed item carries the reason code from `check_territory`.
- **Tests:** an AE hub granted to an Oncor-area obligation → `R-TERRITORY-OUTSIDE`; an AE hub on headroom with
  access off → `R-TERRITORY-NO-FREE-ACCESS`; the same with access on → PASS; an unknown zone → `R-TERRITORY-UNKNOWN`;
  property: random fleets and contract mixes give zero signed cross-territory items.
- **Status 2026-09-26:** built by SAFETY in `guardian/flow_checks.py` (`check_g33_territory`), calling
  `market.check_territory`.

### 11.7 Invariants checker measurements (INVARIANTS)

| Check | Violation when | Data | Severity |
|---|---|---|---|
| `K15_TERRITORY_MARKET` | any `og.grant` with `granted_kw > 0` whose (obligation → contract) `market_of` and (grant bank → zone → territory) give a non-None `check_territory(...)`. Headroom grants (`is_headroom`, no obligation) are checked as `FREE` | `og.grant`, `og.obligation`, `og.contract`, `og.bank`, `og.utility.free_access_granted`, `[zone_territory]` | critical |
| `K15_TERRITORY_EXPORT` | for a boundary substation of territory u: the Base-attributable net injection in an interval is > 0 (sum of territory-bank grants discharging minus charging, against the substation SCADA) | SCADA + grants (needs 0027/0029 flow tables) | critical; **later** (no boundary-substation SCADA yet) |
| `K15_CHARGING_TARIFF` | a settled charging interval in a regulated territory carries a non-zero M1 delivery charge, or a FREE one carries a utility tariff reference | `og.pnl.delivery_charge` joined to the obligation's reservation zone | warning |

Count one violation per (grant_id, check) and dedupe as migration 0017 does. The reason code recorded is the
`check_territory` result. The checker reuses `market.check_territory` and `market.territory_of_zone`: it never
re-derives the predicate. The check-name domain in `og.invariant_violation` must be widened by INVARIANTS'
own migration.

### 11.8 Profitability route (for the lead or MERGE)

`GET /og/api/profitability/per-kw?start=<ISO8601>&end=<ISO8601>` (operator auth, read-only; default: the
current settlement month in America/Chicago).

1. `scopes = await opengrid.market.pg_backend.fetch_contract_totals(pool, start, end)`
2. `return opengrid.market.view.profitability_per_kw(scopes).model_dump(mode="json")`

The response is `PerKwSummary`: `method_version`, `period_hours`, `hardware_view_usd_per_kw` (636.36),
`target_payback_years` (3), `contracts[]`, `markets{REGULATED, FREE}`, `fleet`, `illustrative_home_unit`
(the §3c reference). Each is a `KwEconomics`: kW basis, annualised $-in and $-out with their components,
`in_usd_per_kw_yr`, `out_usd_per_kw_yr`, `net_usd_per_kw_yr`, capex per kW, effective investment per kW,
simple, effective and discounted payback, `npv_5y_usd`, `npv_15y_usd`, `meets_target` (amber when false) and
`notes`. Decimals serialise as strings. A 400 is returned when `end <= start`.

**Limits until settle's §4 tables exist.** Capex is a PLANNING attribution (kW basis × $7,000/11 kW) and O&M is 3%
of capex per year, both flagged in each row's `notes`. Revenue is split into capacity (REGULATED_CAPACITY,
DIST_DEFERRAL, PARTNER_CAPACITY, ERCOT_AS, DATA_CENTER, PJM_CAPACITY) or energy by service type. FREE headroom
P&L is not yet in the view (no `og.headroom_pnl`).
