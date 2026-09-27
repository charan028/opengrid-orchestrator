# Market model: one regulated market plus one free market (addendum, 2026-09-26)

Status: **direction confirmed by the owner from Base guidance on 2026-09-26.** The detailed design is pending. It
doesn't change the MVP-S demo scope; it sets the next build phase.

## 1. What Base told us

| Topic | Guidance |
|---|---|
| Markets | Two at once: one **free** market (ERCOT, an exchange for the unregulated market) and one **regulated**, vertically integrated utility. |
| Regulated market | The utility owns transmission and won't transmit energy outside its territory, so **the utility is the customer**. Regulated utilities **pay a premium for capacity**. |
| Asset ownership | **Base always owns the batteries**, both home units and units sited at or next to distribution substations. |
| Data centers | They're normally supplied from batteries close to, or built next to, the nearest distribution substation (transformer station). |
| Home batteries | **In scope** for regulated contracts: they serve the utilities' capacity contracts. |
| Economics | Don't value by hardware cost ($7k per battery). Value by **$/kW in (cost to charge) against $/kW out (revenue from selling to third parties, not the home users)**. On that basis the effective investment is **under $500/kW**; the naive figure is $7k per 11 kW unit, about $636/kW. |
| Arbitrage | Arbitrage in open markets **still makes sense** as deployment and capacity grow, because demand swings are large. |

## 2. What already fits in MVP-S

- **The commitment model** (K13 and the 2026-09-26 additions): committed kW is reserved over a period, locked, and
  never resold. It's delivered on a fixed (schedule) basis or on a need basis (measured). That's the shape of a
  regulated capacity contract.
- **Capacity-style profiles:** DIST_DEFERRAL and PARTNER_CAPACITY, with capacity payment = committed kW × price ×
  performance factor (§7.3; billing fix 2026-09-26).
- **DATA_CENTER:** site meter, PQ envelope and closed-loop control (built dark 2026-09-26).
- **Safety chain:** the guardian (K1–K14), the audit trail and the invariants.

## 3. Changes for the next phase

1. **Market model.** Every contract belongs to a market: `REGULATED(utility_id)` or `FREE(ERCOT)`.
   - A regulated contract is served only by assets inside that utility's **service territory**. That's a hard
     constraint in the selector and allocator, re-checked by the guardian.
   - A free-market opportunity uses only **uncommitted headroom** (K13 unchanged).
2. **Selection priority.** Regulated capacity commitments are valued first (premium, long-term), and arbitrage
   takes the remaining headroom. Arbitrage stays because demand swings are large.
3. **Assets.** Add **substation-sited battery assets**, owned by Base: a single larger asset per substation bank,
   modelled as a hub with its own rating, SoC and PQ characterisation, alongside home banks. Data-center contracts
   draw on the substation assets nearest the site first.
4. **Economics ($/kW accounting).** Per contract, per market and fleet-wide:
   - $/kW-in: charging energy cost plus the delivery charge (M1, the free market only) and any demand charges.
   - $/kW-out: capacity payments and energy and AS revenue from third parties.
   - Net $/kW-year, and the effective $/kW investment against the hardware view ($7k per unit).
   - Shown on the profitability screen and in settlement.
5. **Delivery charge (M1).** Applies to grid charging in the free market (ERCOT TDSP tariffs, `tdsp_tariffs.toml`).
   Regulated-market charging cost comes from the utility contract terms, per utility.

## 3a. Answers from Base via the owner, 2026-09-26

| Question | Answer |
|---|---|
| First regulated utility | **Austin Energy**, or the San Antonio utility (**CPS Energy**). Both are municipally owned, vertically integrated, and outside ERCOT retail choice. Base says the Austin Energy rate is **about 4× the normal rate**; exactly which rate is being researched. |
| Charging cost | **About 30% solar charging; the rest at Austin Energy nightly rates.** |
| Territory data | Take it from external sources (ERCOT, the utilities, public GIS). Research is in progress: `integrations/regulated-utilities-austin-cps-2026-09.md`. |
| Substation assets | **Battery sets of about 20 MW.** |
| ROI formula | Not given. Base reports a **very short ROI even assuming a 5-year battery life**, and better at 15 years. We extrapolate the formula in §3b. |

## 3b. Working ROI model (extrapolated; to validate with Base)

Per kW of Base-owned capacity, per year:

- **Charging cost per kWh:** $c_{in} = 0.30 \cdot c_{solar} + 0.70 \cdot c_{night}$, where $c_{solar}$ is the
  solar cost per kWh (LCOE or PPA) and $c_{night}$ is the Austin Energy off-peak or night energy rate. In the free
  market, add the TDSP delivery charge (M1).
- **Energy delivered per kW-year:** $E_{out} = \text{cycles/yr} \times \text{hours per cycle} \times \eta_{rt}$.
- **Revenue per kW-year:**
  $R_{out} = \text{capacity payment}_{reg}\,[\$/\text{kW-yr}] + E_{out}\cdot p_{sale} + \text{arbitrage}_{free}$ on
  headroom.
- **Net value per kW-year:** $N = R_{out} - E_{out}/\eta_{rt}\cdot c_{in} - \text{degradation} - \text{O\&M}$.
- **Payback in years:** $\text{capex}_{/kW} / N$. The hardware view is $7{,}000 / 11\ \text{kW} \approx \$636/\text{kW}$.
  Base's framing ("effective investment < $500/kW") nets early revenue against capex.
- **Lifetime value:** $\sum_{y=1}^{L} N_y/(1+r)^y - \text{capex}$ for $L = 5$ and $L = 15$ years.

The orchestrator's profitability view reports $c_{in}$ and $R_{out}$ per kW (the "$/kW in vs out") and $N$, per
contract, per market and fleet-wide. It must reproduce Base's short-ROI claim with real tariff numbers before we
rely on it.

## 3c. Planning assumptions (owner, 2026-09-26) and a sanity check

These assumptions are used until Base provides its formula:

- "< $500/kW" is a **net** figure: effective investment after early revenue and incentives. It is not a hardware
  cost; the ERCOT 2026 CONE study puts new 2-hour BESS at about $1,750/kW overnight capex.
- The target payback ("extremely short ROI") is **about 3 years**.

Illustrative check for one home unit (11 kW / 39.2 kWh, capex $7,000). Every figure is an assumption, sourced in
`integrations/regulated-utilities-austin-cps-2026-09.md`:

| Item | Assumption | $/year |
|---|---|---|
| Capacity payment (Austin Power Partner-like) | $75/kW-yr × 11 kW | 825 |
| Daily energy cycle | 39.2 kWh × η_rt 0.9 = 35.3 kWh out, 300 days/yr | — |
| Charging cost | 30% solar at 4.0¢ plus 70% Austin off-peak at 2.677¢, which is 3.07¢/kWh, on 39.2 kWh | −361 |
| Energy value out | 35.3 kWh × 12.88¢ (Austin value of solar, Oct 2026) × 300 | +1,364 |
| Degradation and O&M | 3% of capex | −210 |
| **Net, before free-market scarcity upside** | | **≈ 1,620** |

Payback is about 4.3 years before ERCOT free-market scarcity events and demand response. About 3 years needs
roughly $700/yr more per unit, which is plausible from ERCOT price spikes on uncommitted headroom and DR events.
The orchestrator's per-kW reporting must show this stack from real settled data, so the 3-year target can be
verified.

The owner confirmed on 2026-09-26 that these assumptions are reasonable.

Base also noted that **growing solar capacity drives the price swings**:

- midday prices are depressed by solar, and the evening ramp brings scarcity (the "duck curve");
- so the arbitrage spread, and the value of battery flexibility, is expected to WIDEN as solar grows, not shrink as
  storage is deployed.

For the selector and forecast, this means:

- **Forecast:** model solar-driven intraday price shape per load zone, from NWS irradiance and cloud cover and
  ERCOT solar forecasts, and not only a flat diurnal.
- **Charging:** prefer midday charging when solar depresses prices, as well as overnight. The 30% solar share of
  the charging mix is a floor, not a fixed ratio.
- **Energy reserve:** keep energy for the evening ramp, where the spikes are largest, subject to the commitments
  already held.

## 4. Open questions for Base

1. For the first regulated utility: its name or region, its capacity product (demand response, non-wires
   alternative / distribution deferral, resource adequacy, data-center interconnection support), and its payment
   basis ($/kW-month or $/kW-year).
2. Regulated charging cost: the utility's retail or wholesale rate for Base's charging, and any demand charges.
3. Territory data: the service-territory boundaries, and the feeder and substation list for siting.
4. Substation asset sizes (kW / kWh per site) and the interconnection limits.
5. The exact "$/kW in vs out" formula Base uses internally, so the orchestrator's reports match their ROI model.

## D-37: LCRA and Rayburn zones (2026-09-26, supersedes D-32)

| Zone | Utility (`og.utility`) | Market | M1 | Banks (production) | Availability |
|---|---|---|---|---|---|
| LZ_AEN | AUSTIN_ENERGY | REGULATED | none | bank-040..049 + the 20 MW set | AVAILABLE (real toll) |
| LZ_CPS | CPS_ENERGY | REGULATED | none | off | -- |
| LZ_LCRA | LCRA ("LCRA (Lower Colorado River Authority)") | REGULATED (NOIE) | none | bank-050..059 | UNAVAILABLE, `REGULATED_NO_CONTRACT` |
| LZ_RAYBN | RAYBURN ("Rayburn Country Electric Cooperative") | REGULATED (NOIE) | none | bank-060..069 | UNAVAILABLE, `REGULATED_NO_CONTRACT` |

LCRA and Rayburn are modelled like Austin Energy: the utility is the customer and energy is territory-bound (K15).
The real counterparties may be their member cities and distribution co-ops; `LCRA` and `RAYBURN` stand for them
until contracts name them. No contract exists, so each utility has one **"Sample Contract: ... Tolling (placeholder
terms)"** cloned from the Austin toll (REGULATED_CAPACITY / TOLLING, 90 min, $102/kW-yr placeholder, 6,000 kW), kept
SUSPENDED with `is_sample = true` (shown as SAMPLE – INACTIVE; never called, reserved or billed, so it never
enters revenue). Their banks show "Regulated market – no contract" and contribute nothing to available kW.

**D-32 is superseded.** D-32 had switched LZ_LCRA and LZ_RAYBN on as ERCOT free-market zones. Since D-37 (shipped
in r3.4.2) they are regulated: no ERCOT energy, AS or headroom there (K15), no M1 (`tdsp_tariffs.toml` gives them
`delivery_charge = "NONE"`), and manual discharges are vetoed by G-33 unless they serve the utility's own
obligation. The banks stay monitored (telemetry, alerts, health, safe stop, firmware, invariants). They flip to
AVAILABLE only when a real contract is activated for the utility (operator guide 6.10); the utility API and grid
link identities for LCRA and RAYBURN exist in config but stay disabled until then.

## As built at r3.4.3: how the two markets are served

| Topic | Regulated (utility) | Free (ERCOT) |
|---|---|---|
| How a call arrives | Austin Energy issues toll calls through the utility customer API (D-33, enabled for AUSTIN_ENERGY only) or, once enabled, the DNP3 grid link (D-34, **disabled by default**). Every origin goes through the one core call function, `opengrid.calls`. | ERCOT AS deployments (DEPLOY_AS / RECALL_AS) are polled by og-feeds every 5 s (AS-POLL, D-35, `[feeds.ercot_as_poll]`, **off in the repo**) and go through the same core. |
| Settlement | Capacity on availability, $102/kW-yr (D-29); no M1 | AS capacity is priced at the **cleared DAM MCPC** of the award's product for the delivery hour (r3.4.3; flag `MCPC`), falling back to the opportunity's own price when no MCPC observation exists (flag `OPPORTUNITY_PRICE`, logged). Energy settles at the zone SPP (D-10); M1 applies. |
| Delivery evidence | Measured per call (D-38, r3.4.3): `og.delivery_record`, PASS / PARTIAL / FAIL, meter check on metered banks; the utility sees measured delivered kW. | Same record per AS deployment. |

Details for the optimizer and dispatcher (feeder ramp ceilings, truck positions, nameplate rating, delivery
alerts and AT_RISK) are in [09 §11.9](09-optimizer-dispatcher-update.md#119-changes-at-r341-to-r343).
