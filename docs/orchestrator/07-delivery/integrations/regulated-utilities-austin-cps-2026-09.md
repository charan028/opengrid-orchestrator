# Regulated Utilities in ERCOT: Austin Energy and CPS Energy — Tariffs, Capacity Programs, GIS Data, and Battery ROI Benchmarks

**Date:** 2026-09-26
**Scope:** Austin Energy (City of Austin) and CPS Energy (San Antonio) as potential capacity customers for OpenGrid-orchestrated Base Power batteries (39.2 kWh usable / 11 kW home units; ~20 MW substation-sited sets). Both utilities are inside ERCOT but outside retail competition — vertically integrated municipal utilities where the utility itself, not a REP, is the counterparty.

**Sourcing convention:** Each figure is tagged **[Primary]** (utility tariff PDF, city ordinance, or official filing) or **[Secondary]** (news, aggregator, or third-party estimate). Uncertain or unconfirmed points are flagged explicitly. All quotes are under 15 words.

---

## 1. What "4x the normal rate" most likely means

Base Power's shorthand — Austin Energy's rate is "4 times the normal rate" — does not map cleanly onto any single published line item, but the closest, best-supported reading is a **peak-vs-off-peak ratio under Austin Energy's own time-of-use options**, not a demand charge, the PSA, or the Value of Solar rate in isolation. Specifics, from the FY2026 tariff **[Primary]**:

- **Residential Time-of-Use pilot** (power-supply component only, replaces the PSA for enrolled customers): weekday **on-peak $0.08442/kWh** (3–6 PM) vs. **off-peak $0.02677/kWh** (10 PM–7 AM and all weekend) — a **3.15x ratio**.
- **EV360 PEV charging schedule** (closed to new customers but illustrative): summer **on-peak $0.40000/kWh** vs. **off-peak $0.00000/kWh** — off-peak charging is priced at zero marginal power-supply cost, an extreme (technically infinite) ratio, with non-summer on-peak at $0.14/kWh (a **~35x–∞** spread depending on season).
- **Residential inverted-tier structure** (standard, non-TOU): Tier 1 (0–300 kWh) energy charge $0.04640/kWh vs. Tier 4 (>2,000 kWh) $0.10884/kWh — only a **2.35x** ratio on energy alone; including PSA/CBC/Regulatory adders (which don't vary by tier) narrows the all-in ratio further (~1.5–1.6x).
- **Value of Solar credit** vs. **overnight charging cost**: the VoS credit (avoided cost $0.0761/kWh + societal benefit $0.023/kWh = **$0.0991/kWh**, rising to **$0.1288/kWh** effective October 2026) is what Austin Energy pays *out* for exported solar, while the cheapest available overnight energy charge to a battery operator (TOU off-peak, $0.02677/kWh, or Tier 1 $0.0464/kWh) is roughly **2–4.8x lower** — this is a plausible informal shorthand for "the daytime/solar rate is ~4x the night rate," but it conflates a feed-in credit with a retail charge and should be treated as approximate.

**Assessment:** "4x" is closest to the **on-peak/off-peak TOU ratio (~3.15x)** or an **approximate solar-credit-to-night-rate comparison (~2–4.8x)**. It is *not* a demand charge (Austin Energy demand charges run $9.83–$16.24/kW depending on class and don't have a "4x" analog), not a fixed PSA (single value, no multiple), and not a tiered residential comparison (only ~1.6–2.35x). **Flag:** Base Power's own basis for the "4x" claim was not independently verifiable from public sources; it may be an informal/rounded characterization rather than a specific published ratio.

---

## 2. Austin Energy tariffs (FY2026, effective November 1, 2025 unless noted)

Source: *City of Austin Fiscal Year 2026 Electric Tariff* **[Primary]**, https://austinenergy.com/-/media/project/websites/shared/pdfs/rates/tariff.pdf ; residential/commercial rate pages at austinenergy.com/rates.

### 2.1 Residential (inside city limits)

| Component | Rate | Notes |
|---|---|---|
| Customer charge | $16.50/month | |
| Energy: 0–300 kWh | $0.04640/kWh | Tier 1 |
| Energy: 301–900 kWh | $0.05138/kWh | Tier 2 |
| Energy: 901–2,000 kWh | $0.07525/kWh | Tier 3 |
| Energy: >2,000 kWh | $0.10884/kWh | Tier 4 (inverted/tiered design) |
| Power Supply Adjustment (PSA) | $0.04118/kWh | Secondary voltage rate |
| PSA Administrative Adjustment | −$0.00206/kWh | Effective Dec 1, 2025 |
| Community Benefit Charge (CBC) — CAP | $0.00564/kWh | |
| CBC — Service Area Lighting | $0.00254/kWh | $0 outside city limits |
| CBC — Energy Efficiency Services | $0.00457/kWh | |
| Regulatory Charge | $0.01338/kWh | |

No standard time-of-use rate exists for ordinary residential service; a **residential TOU pilot** exists (see §1), capped and separate.

### 2.2 Residential Time-of-Use pilot (power-supply component, replaces PSA)

| Period | Rate ($/kWh) |
|---|---|
| Weekday off-peak (10 PM–7 AM) | $0.02677 |
| Weekday mid-peak (7 AM–3 PM, 6–10 PM) | $0.04118 |
| Weekday on-peak (3–6 PM) | $0.08442 |
| Weekend (all day) | $0.02677 |

**This is the cheapest published overnight/off-peak charging rate available to a residential battery operator: $0.02677/kWh power-supply component**, plus CBC + Regulatory Charge (~$0.0128–0.015/kWh) and the $16.50 customer charge — an effective floor around **$0.041–0.044/kWh all-in** for off-peak energy. **[Primary]**

### 2.3 Commercial / General Service (secondary voltage, inside city limits)

| Class (by summer peak demand) | Customer ($/mo) | Demand ($/kW) | Energy ($/kWh) | PSA ($/kWh) | Regulatory |
|---|---|---|---|---|---|
| <10 kW | $38.23 | — | $0.03129 | $0.04118 | $0.01338/kWh |
| 10 kW–300 kW | $60.08 | $9.83 | $0.01932 | $0.04118 | $3.73/kW |
| ≥300 kW | $300.42 | $12.56 | $0.01840 | $0.04118 | $3.73/kW |

### 2.4 Large General Service (12,470–69,000 V, primary voltage)

| Class | Customer ($/mo) | Demand ($/kW) | Energy ($/kWh) | PSA ($/kWh, primary=0.04005) | Regulatory ($/kW) |
|---|---|---|---|---|---|
| <3,000 kW | $327.73 | $12.56 | $0.00119 | $0.04005 | $3.65 |
| 3,000–20,000 kW | $2,731.05 | $15.30 | $0.00020 | $0.04005 | $3.65 |
| ≥20,000 kW | $3,004.16 | $16.24 | $0.00166 | $0.04005 | $3.65 |
| High-Load-Factor (≥85% LF) 3,000–20,000 kW contract | $6,881.15 | $14.66 | $0.00000 | $0.04005 | $3.65 |
| High-Load-Factor ≥20,000 kW contract | $21,848.40 | $15.74 | $0.00000 | $0.04005 | $3.65 |

At these voltages the **energy charge is nearly zero and cost recovery is almost entirely via the demand charge** ($12.56–$16.24/kW) plus PSA — meaning a substation-scale (20 MW) battery interacting with Austin Energy at this level would be priced overwhelmingly on **$/kW-month demand**, not $/kWh energy, reinforcing that capacity value (not energy arbitrage) is the primary lever for a 20 MW asset on this tariff. **[Primary]**

No standard commercial/large-general-service time-of-use energy schedule (on-peak/off-peak/night $/kWh) is published for these classes; TOU is offered only via the residential pilot and the "Load Shifting Voltage Discount Rider" (a discount rider tied to shifting ≥30% of on-peak demand via storage, not a separate energy rate). **[Primary]**

### 2.5 Power Supply Adjustment (PSA)

**[Primary]** Recovers fuel costs, net purchased-power costs, and one-time PSA items; adjustable administratively ±5%/year without Council vote.

| Voltage level | PSA ($/kWh) | Admin. Adjustment ($/kWh, from Dec 1, 2025) |
|---|---|---|
| System average | $0.04091 | −$0.00205 |
| Secondary | $0.04118 | −$0.00206 |
| Primary | $0.04005 | −$0.00200 |
| Transmission | $0.03956 | −$0.00199 |

### 2.6 Regulatory Charge

**[Primary]** Recovers ERCOT transmission charges, NERC/TRE fees, ERCOT Nodal/Administrative fees. Billed per kWh for non-demand customers ($0.01338/kWh) or per kW for demand customers ($3.61–$3.75/kW depending on voltage).

### 2.7 Community Benefit Charge (CBC)

**[Primary]** Three components, billed $/kWh: Customer Assistance Program (CAP, $0.00237–$0.00564/kWh depending on class), Service Area Lighting (SAL, $0.00243–$0.00254/kWh inside city limits, $0 outside), Energy Efficiency Services (EES, $0.00438–$0.00457/kWh). Recent secondary reporting: CBC "went up by around 27%" in the FY2026 cycle while the Regulatory Charge held flat **[Secondary]**.

### 2.8 Value of Solar (VoS) Rate

**[Primary]** Recalculated every 3 years as a 5-year trailing average.

| Component | Rate |
|---|---|
| Avoided cost (non-demand / demand <1,000 kW-ac) | $0.0761/kWh |
| Avoided cost (demand ≥1,000 kW-ac) | $0.0494/kWh |
| Societal benefit adder | $0.023/kWh |
| **Total VoS, systems <1 MW** | **$0.0991/kWh** (tariff table) → reported updated to **$0.1288/kWh effective Oct 1, 2026** **[Secondary — solaraustin.org, HelioRoofer]** |
| **Total VoS, systems ≥1 MW** | ~$0.0724/kWh (tariff) → reported **$0.1021/kWh** post-update **[Secondary]** |

The October 2026 increase (9.91¢→12.88¢, ~30%) was reported in trade/solar-installer press; the underlying FY2026 tariff PDF (effective Nov 1, 2025) still shows the pre-update figures, so the increase is a **mid-cycle update not yet reflected in the primary tariff document reviewed** — flagged as an update-in-progress.

### 2.9 Cheapest overnight rate vs. peak rate — summary table

| Rate point | $/kWh | Ratio to cheapest overnight |
|---|---|---|
| TOU off-peak (residential pilot) | $0.02677 | 1.0x |
| Standard Tier 1 residential (no PSA) | $0.04640 | 1.7x |
| TOU mid-peak | $0.04118 | 1.5x |
| TOU on-peak | $0.08442 | **3.15x** |
| VoS credit (<1MW, current) | $0.0991 | 3.7x |
| VoS credit (<1MW, Oct-2026 update) | $0.1288 | **4.8x** |
| EV360 on-peak summer | $0.40000 | ~14.9x |

---

## 3. Austin Energy capacity / demand-response programs and storage projects

| Program | Basis | Payment | Source |
|---|---|---|---|
| **Power Partner Battery Pilot** (residential) | Home battery DR, launched March 24, 2026 | **$500 upfront rebate** + average **>$300/year** performance incentive (secondary reporting cites up to **$75/kW annual**) for allowing discharge during peak events; capped at **1,500 systems**; targets **78 MW by 2027, 270 MW by 2035** (part of Resource, Generation & Climate Protection Plan) | **[Primary/Secondary mixed]** austinenergy.com news release; mgrid.org secondary cites $75/kW |
| **Power Partner Thermostats** | Residential DR | $75 one-time bill credit + $30/year retention | **[Secondary]** austinenergy.com program page |
| **Commercial Demand Response** | C&I curtailment, summer events | **$50–$80 per average kW saved** during peak events | **[Secondary]** austinenergy.com |
| **Load Shifting Voltage Discount Rider** | Non-residential storage/thermal shifting ≥30% of on-peak demand | Not a direct $/kW payment — a **rate discount** applied to underlying demand/energy/regulatory charges for shifted on-peak load (on-peak window 3–6 PM demand, 7 AM–10 PM energy) | **[Primary]** FY2026 tariff |
| **Storage/Battery RFP** | No open utility-scale battery storage RFP identified for Austin Energy as of Sept 2026 (unlike CPS) | — | Not found; **flagged as absence, not confirmed non-existence** |

Austin Energy's own published storage cost data (from Power Partner Battery Pilot economics) implies an effective utility cost of roughly **$500 upfront + ~$75/kW-yr** per participating home battery for the demand-response service — a **useful direct comparable to Base Power's claimed customer/utility economics**, though it is a residential DR incentive, not a wholesale capacity price. **[Secondary — mgrid.org; not independently confirmed against a primary program manual]**.

---

## 4. CPS Energy tariffs (rates effective February 1, 2024 — the last confirmed rate case; no 2025 increase; a 2026 rate request was "signaled" but not yet approved as of this writing) **[Primary + Secondary]**

Source: CPS Energy rate schedule PDFs, https://www.cpsenergy.com/content/dam/corporate/en/Documents/2024_Rate_ResidentialAllElectric.pdf and .../2024_Rate_GeneralService.pdf **[Primary]**.

### 4.1 Residential (Schedule RA — Residential All-Electric)

| Component | Rate |
|---|---|
| Service Availability Charge | $9.50/month |
| Energy — summer (June–Sept) | $0.07503/kWh for all kWh |
| Energy — non-summer (Oct–May), first 600 kWh | $0.07503/kWh |
| Energy — non-summer, kWh beyond 600 | $0.06429/kWh |
| **Peak Capacity Charge** (summer only, kWh beyond 600) | **+$0.02150/kWh** |
| Fuel adjustment base | $0.01416/kWh (monthly true-up clause, uncapped, tied to actual fuel/purchased-power costs) |

The **Peak Capacity Charge** is CPS Energy's closest analog to a residential on-peak surcharge — a summer-only adder introduced "over 30 years ago... to encourage customers to conserve energy" **[Secondary — CPS Energy Newsroom explainer]**. It roughly triples the marginal summer rate above 600 kWh (base $0.07503 vs. $0.02150 adder = ~29% surcharge, not 4x), and does not exist Oct–May.

### 4.2 General Service — Base Commercial Rate (Schedule PL, small/general commercial)

| Component | Rate |
|---|---|
| Service Availability Charge | $9.50/month |
| Energy — first 1,600 kWh (200 kWh/kW of demand >5 kW) | $0.07817/kWh |
| Energy — additional kWh | $0.03610/kWh |
| Peak Capacity Charge, summer (kWh >600) | $0.02150/kWh |
| Peak Capacity Charge, non-summer (kWh >600) | $0.01087/kWh |
| Minimum bill demand component | $4.35/kW over 5 kW |
| High-voltage discount (≥13.2 kV, ≤1 step-down) | −$0.00225/kWh on first 200 kWh/kW of billing demand |
| Fuel adjustment base | $0.01416/kWh (same monthly clause) |

CPS Energy's larger commercial/industrial schedules (General Service PL → Large Lighting & Power → Extra Large Power (ELP) → Super Large Power (SLP)) are **demand-driven**; billing demand for June–Sept equals metered demand, while Oct–May billing demand is the greater of metered demand or 80% of the prior summer peak (a demand "ratchet"). **[Primary]** A dedicated $/kWh on-peak/off-peak TOU schedule for large commercial customers is referenced in secondary sources ("mandatory TOU rate for large commercial customers") but the specific $/kWh on-peak/off-peak figures could not be extracted from the ELP/SLP tariff PDFs in this pass — **flagged as needing direct confirmation from the ELP/SLP rate PDFs** (https://www.cpsenergy.com/content/dam/corporate/en/Documents/2024_Rate_ExtraLargePowerService.pdf and .../2024_Super_Large_Power_Serv_Elec_Rate.pdf).

### 4.3 Fuel Adjustment Clause

**[Primary]** Both residential and commercial rates carry a monthly fuel-cost true-up (base $0.01416/kWh) reconciling actual vs. forecast fuel/purchased-power costs, plus separate riders for a City-of-San-Antonio-authorized "regulatory asset" amortization (per Ordinance 2022-01-13-0001) and pass-through of tax/franchise-fee changes.

### 4.4 Demand response and storage programs

| Program | Basis | Payment | Source |
|---|---|---|---|
| **Commercial & Industrial Demand Response** | Voluntary summer curtailment (June 1–Sept 30), ~25 events/season, 1–7 PM weekdays | **$45/kW** for ≤30-min response (up to 40 hrs, 10 events); **$40/kW** for ≤60-min response | **[Secondary]** CPS Energy program PDF via search summary |
| **FlexPOWER Bundle / FlexSTEP** | Long-term generation & DR procurement strategy targeting **410 MW of incremental demand reduction by 2027**, part of replacing 1,700 MW of retiring capacity | Procurement RFP/RFI mechanism, not a fixed $/kW rate | **[Secondary]** CPS Energy newsroom, peakload.org |
| **500 MW Battery Storage RFP** (Dec 2025) | Utility-owned/contracted storage; proposals due Jan 30, 2026 | Payment basis not disclosed publicly in the RFP announcement | **[Secondary]** pv-magazine-usa.com, CPS Energy newsroom |
| **20 MW Distribution-Scale BESS + Microgrid RFP** (June 2026) | CPS Energy to own/operate 2 battery sites | Not disclosed | **[Secondary]** CPS Energy newsroom |
| **CPS Energy own storage build-out** | 50 MW online under Vision 2027; additional 470 MW in development for 2026; target >1,000 MW operational/contracted after the 500 MW RFP | Capex not published | **[Secondary]** news4sanantonio.com, publicpower.org |

**No published $/kW-month or $/kW-year capacity payment rate was found for CPS Energy's battery RFPs** — these are competitive-bid procurements, not posted tariff rates, so exact capacity payment levels are commercially confidential until contracts are awarded. This is a genuine data gap, not an oversight.

---

## 5. Service territory GIS data and substation data

| Dataset | Coverage | Format / Access | License | Notes |
|---|---|---|---|---|
| **Austin Energy Service Area** | ~437 sq mi (Austin, Travis Co., part of Williamson Co.) | Feature layer via data.austintexas.gov; CSV, KML, Shapefile (zip), GeoJSON, GeoTIFF, PNG; ArcGIS REST (GeoServices/WMS/WFS) | City of Austin Open Data Terms (generally open, attribution-style) | https://data.austintexas.gov/Locations-and-Maps/Austin-Energy-Electric-Utility-Service-Area/i2t2-i3uy ; also https://geohub.austintexas.gov **[Primary — city open-data portal]** |
| **CPS Energy service territory** | ~1,566 sq mi, Bexar County + parts of 7 surrounding counties; >840,750 electric customers | No dedicated CPS Energy open GIS layer for the exact boundary was found; City of San Antonio's general GIS portal (opendata-cosagis.opendata.arcgis.com) hosts city layers but a specific CPS service-area polygon was not confirmed in this pass | Presumed City of San Antonio open-data terms | **[Secondary for boundary figures; primary boundary layer not confirmed found]** — flagged: recommend direct outreach to CPS Energy GIS/Growth Forecasting team or sanantonio.gov/GIS/GISData for the authoritative polygon |
| **HIFLD Electric Substations** | National, substations ≥69 kV | Public download via Homeland Infrastructure Foundation-Level Data (gii.dhs.gov/HIFLD) and mirrored on data.gov, ArcGIS Hub, Kaggle | DHS/HIFLD "Custom License" — generally public/open but with a specific HIFLD terms page; some fields for control centers/critical infrastructure locations are intentionally generalized or withheld for CEII (Critical Energy/Electric Infrastructure Information) sensitivity | https://catalog.data.gov/dataset/electric-substations-c633a ; https://gii.dhs.gov/HIFLD |
| **EIA-861 / EIA-860** | National; EIA-861 = utility service-territory county lists (not polygons) and DR/EE program data; EIA-860 = generator-level data (≥1 MW) | Excel/CSV at eia.gov/electricity/data/eia861 and /eia860; derived GeoParquet service-territory polygons available via the PUDL project (github.com/catalyst-cooperative or IMMM-SFA/electricity_entity_boundaries) built from EIA-861 + Census county geometries | Public domain (U.S. government data) | Useful for utility-level generation/DR context, not substation-level siting |
| **ERCOT network model** | Full node/substation/transmission topology | Public: aggregate reports (CDR — Capacity, Demand & Reserves; GIS — Generator Interconnection Status reports, used directly in the Brattle CONE study below); **the detailed nodal network model itself is CEII-restricted** and requires an ERCOT Market Participant / CEII non-disclosure agreement | CEII-restricted for the detailed model; public reports available at ercot.com | Distinguish: ERCOT's public *reports* (GIS report, CDR report) are usable for market-sizing; the underlying *nodal model* (node-level topology, protective relay settings, substation-level line ratings) is not public. |

**Practical implication for OpenGrid:** substation-level siting analysis for either utility's territory can combine the **public HIFLD substation layer (≥69 kV only — battery-relevant distribution substations below 69 kV are not covered)** with the **city open-data service-area polygons**, but detailed distribution-level substation lists (the ones most relevant to 20 MW battery interconnection) are **not public** for either Austin Energy or CPS Energy and would need to come from direct utility engagement or interconnection-queue filings.

---

## 6. Battery ROI benchmarks — checking Base Power's <$500/kW and short-payback claims

### 6.1 Cost of New Entry (CONE) — ERCOT, 2026 online year

Source: Brattle Group / Sargent & Lundy, *ERCOT CONE for 2026*, prepared for ERCOT, June 10, 2024 **[Primary — commissioned study, publicly released by ERCOT/Brattle]**, https://www.brattle.com/wp-content/uploads/2024/08/ERCOT-CONE-for-2026.pdf

| Resource | Overnight capital cost | Levelized CONE (2026$/kW-yr) |
|---|---|---|
| Aeroderivative gas peaker (6×0 GE LM6000PC, 291 MW) | **$1,764/kW** (installed: $1,934/kW) | **$293/kW-yr** |
| PV + 2-hr BESS hybrid (200 MW PV / 100 MW BESS) | **$1,743/kW** (of PV capacity; installed $1,864/kW) | **$263/kW-yr** |
| Frame CT (indicative, sensitivity case) | lower capital cost | **$162/kW-yr** |
| ERCOT's current regulatory CONE (2012 study, escalated to 2026$) | — | **$149/kW-yr**, underlies the current **$315,000/MW-yr (=$315/kW-yr) Peaker Net Margin threshold = 3×CONE** |

Within this study, **BESS-specific capital cost is $928/kW ($464/kWh for a 2-hour system)**, and total FOM for the PV+BESS is ~$45/kW-yr including battery augmentation. ATWACC used: 10.35%.

### 6.2 NREL ATB 2025 — utility-scale battery storage costs

**[Primary — NREL, "Cost Projections for Utility-Scale Battery Storage: 2025 Update," docs.nrel.gov/docs/fy25osti/93281.pdf]**

- 4-hour lithium-ion systems, base-year (2025) costs form the starting point for trajectories reaching **$147/kWh (low), $243/kWh (mid), $339/kWh (high)** by 2035, and **$108/$178/$307/kWh** by 2050.
- Converting $/kWh to $/kW at 4-hour duration: multiply by 4 (e.g., $243/kWh mid-case ≈ **$972/kW** for a 4-hour system in 2035).
- Fixed O&M ≈ 4% of $/kW capital cost per year in the 2025 ATB.
- **2-hour systems (comparable to the ERCOT CONE PV+BESS reference) cost roughly $928/kW today per the Brattle/S&L bottom-up estimate above**, consistent with NREL's shorter-duration, lower total $/kW (but higher $/kWh) pricing pattern.

### 6.3 Lazard Levelized Cost of Storage (LCOS), 2025 edition (18th edition of Lazard's LCOE+/LCOS)

**[Primary — Lazard, June 2025, lazards-lcoeplus-june-2025.pdf]**

- U.S. LCOS fell to **~$93/MWh** (2025) from **$104/MWh** (2024) and **$155/MWh** (2023) — a reversal of 2021–2024 cost increases, driven by EV-battery oversupply and cell-technology gains.
- **100 MW utility-scale standalone BESS:** 2-hour duration **$129–$277/MWh**; 4-hour duration **$115–$254/MWh**.
- **C&I standalone BESS (1 MW, 2-hour):** **$319–$506/MWh** — materially more expensive than utility-scale, relevant if comparing to Base Power's fleet of small (11 kW) home units rather than substation-scale assets.

### 6.4 Assessment of Base Power's claims

- **"Effective investment under $500/kW"**: Current published benchmarks for utility-scale 2-hour BESS capital cost cluster around **$900–$1,800/kW** (Brattle/S&L bottom-up $928/kW for BESS equipment alone, plus BOP/EPC bringing the full PV+BESS plant to $1,743/kW; NREL ATB mid-case 4-hour ≈ $970/kW by 2035). A **sub-$500/kW effective investment is well below all primary benchmarks found** for utility-scale battery capex as of 2025–2026. It could conceivably be reached only via: (a) counting only the *battery cell* cost stripped of power electronics/BOP/EPC/interconnection, which is not representative of a deployable system; (b) netting capex against upfront incentive payments (e.g., ITC or the Power Partner $500/kW-equivalent rebate) to arrive at a *net effective* cost to the operator rather than total installed cost; or (c) using home-battery-scale hardware (Base's own 11 kW/39.2 kWh units) purchased at scale with different cost structure than utility-scale BESS — home battery unit economics were not independently benchmarked in this research and would need a separate check against residential storage market pricing (e.g., $/kWh for Tesla Powerwall-class systems, roughly $600–900/kWh installed per general market reporting, which is *higher* per kWh than utility-scale, not lower). **On current evidence, the <$500/kW claim is not supported by verified benchmarks and should be treated as unverified/likely optimistic** unless Base Power is defining "effective investment" net of subsidies, incentive payments, or third-party financing in a way not stated.
- **"Very short ROI even at 5-year battery life"**: The ERCOT CONE ($263–293/kW-yr) and Lazard LCOS ($115–277/MWh-equivalent for 2–4 hr systems) benchmarks represent the *revenue a merchant asset needs to earn* to justify entry over a 20-year economic life at a ~10.35% cost of capital — i.e., they are cost benchmarks, not achieved revenues. Whether a 5-year-life, sub-$500/kW asset could show fast payback depends entirely on (a) the true installed cost (see above — likely higher than claimed) and (b) the achievable revenue stack (capacity/DR payments like Austin Energy's ~$75/kW-yr Power Partner incentive, CPS Energy's $40–45/kW summer DR payment, plus any energy arbitrage and ancillary services). At CPS Energy's $40–45/kW *per-event-season* DR rate or Austin's ~$75/kW-yr, and even optimistically assuming $150–200/kW-yr achievable blended revenue, a 5-year payback would require an installed cost under roughly $750–1,000/kW — **plausible only near the low end of published capital-cost ranges, and only if Base is also monetizing multiple revenue streams (capacity + arbitrage + ancillary services) simultaneously**. This is a **directionally supportable but numerically unconfirmed claim; the underlying "<$500/kW" premise, once corrected, tightens but does not obviously invalidate a 5-year payback if capacity + energy + DR revenues stack.

---

## 7. Key uncertainties and gaps to flag

1. **Base Power's exact basis for "4x the normal rate" was not found in any primary Austin Energy document.** The closest published ratios are ~3.15x (TOU on/off-peak) and ~4.8x (post-Oct-2026 VoS credit vs. off-peak TOU rate); treat as an approximation, not a cited figure.
2. **CPS Energy commercial time-of-use ($/kWh on-peak/off-peak) for large commercial (ELP/SLP) schedules could not be fully extracted** from the rate PDFs in this pass; the PDFs were located (https://www.cpsenergy.com/content/dam/corporate/en/Documents/2024_Rate_ExtraLargePowerService.pdf, .../2024_Super_Large_Power_Serv_Elec_Rate.pdf) but only the General Service (PL) schedule was fully parsed.
3. **CPS Energy's 2026 rate case status is unresolved** — secondary reporting notes a "possible 2026 rate request" not yet approved; figures above reflect the February 1, 2024 rate case, the last confirmed one.
4. **CPS Energy service-territory GIS boundary**: no primary, dedicated open-data polygon for the CPS Energy electric service area (as distinct from City of San Antonio's general GIS portal) was confirmed; recommend direct request to CPS Energy or sanantonio.gov/GIS.
5. **CPS Energy and Austin Energy battery-storage RFP capacity payment bases ($/kW-month or $/kW-yr) are not publicly disclosed** — these are competitive procurements; only program size (MW) and timeline are public.
6. **Base Power's <$500/kW claim is not corroborated by any primary capex benchmark found** (all cluster $900–1,800+/kW for utility-scale 2–4hr systems); this is the single most important flag for OpenGrid's internal diligence.
7. **Austin Energy's October 2026 Value-of-Solar rate increase (9.91¢→12.88¢)** is documented only in secondary/trade press as of this writing; the primary FY2026 tariff PDF reviewed (effective Nov 1, 2025) still shows the pre-update table — the increase should be re-confirmed against Austin Energy's own published rate schedule once the November 2026 update posts.

---

## 8. Source list

**Austin Energy (primary):**
- FY2026 Electric Tariff (eff. Nov 1, 2025): https://austinenergy.com/-/media/project/websites/shared/pdfs/rates/tariff.pdf
- Residential Rates: https://austinenergy.com/rates/residential-rates
- Commercial Rates: https://austinenergy.com/rates/commercial-rates
- Value of Solar Rate: https://austinenergy.com/rates/residential-rates/value-of-solar-rate
- Power Partner Battery Pilot news release: https://austinenergy.com/about/news/news-releases/2026/Austin-Energy-launches-innovative-Power-Partner-Battery-Pilot-program
- Commercial Demand Response: https://savings.austinenergy.com/commercial/offerings/load-management/commercial-demand-response
- Austin Energy Service Area (GIS): https://data.austintexas.gov/Locations-and-Maps/Austin-Energy-Electric-Utility-Service-Area/i2t2-i3uy

**Austin Energy (secondary):**
- 2026 VoS increase reporting: https://solaraustin.org/2026/07/29/austin-energy-value-of-solar-rate-increase-2026/ ; https://helioroofer.com/articles/austin-energy-value-of-solar-rate-increase-october-2026/
- Power Partner Battery Pilot MW targets: https://mgrid.org/2026/04/07/austin-energy-launches-battery-vpp-pilot-with-500-incentive-targets-78-mw-demand-response-by-2027/
- PSA increase reporting: https://cbsaustin.com/news/local/austin-energy-implementing-5-increase-for-customers-starting-new-years-day-ercot-power-bills

**CPS Energy (primary):**
- Residential All-Electric Rate (RA), eff. Feb 1, 2024: https://www.cpsenergy.com/content/dam/corporate/en/Documents/2024_Rate_ResidentialAllElectric.pdf
- General Service Rate (PL), eff. Feb 1, 2024: https://www.cpsenergy.com/content/dam/corporate/en/Documents/2024_Rate_GeneralService.pdf
- Extra Large Power Service (ELP): https://www.cpsenergy.com/content/dam/corporate/en/Documents/2024_Rate_ExtraLargePowerService.pdf
- Super Large Power Service (SLP): https://www.cpsenergy.com/content/dam/corporate/en/Documents/2024_Super_Large_Power_Serv_Elec_Rate.pdf
- Understanding Your CPS Energy Bill (fuel/regulatory charges): https://www.cpsenergy.com/en/about-us/who-we-are/financial-information/fuel-charges.html

**CPS Energy (secondary):**
- Peak Capacity Charge explainer: https://newsroom.cpsenergy.com/what-is-the-peak-capacity-charge-on-my-cps-energy-summer-bill/
- Commercial DR incentive rates: CPS Energy program PDF (via search) — https://www.cpsenergy.com/content/dam/corporate/en/Documents/EnergyEfficiency/requirements_demand_response.pdf
- FlexPOWER Bundle: https://www.peakload.org/cps-rfp-7-28-2020 ; https://newsroom.cpsenergy.com/cps-energy-battery-storage-microgrid-rfp-2026/
- 500 MW Battery Storage RFP: https://www.publicpower.org/periodical/article/cps-energy-rfp-seeks-500-mw-additional-battery-storage ; https://pv-magazine-usa.com/2025/12/16/rfp-alert-cps-energy-seeks-500-mw-of-texas-battery-storage/
- Rate/2026 request status: https://nuwattenergy.com/en/texas/cps-energy-rates-2026

**GIS / substation / territory data:**
- HIFLD Electric Substations: https://catalog.data.gov/dataset/electric-substations-c633a ; https://gii.dhs.gov/HIFLD
- EIA-861/EIA-860: https://www.eia.gov/electricity/data/eia861 ; https://www.eia.gov/electricity/data/eia860
- PUDL service-territory GeoParquet derivation: https://docs.catalyst.coop/pudl/en/v2026.2.0/autoapi/pudl/analysis/service_territory/
- City of San Antonio GIS portal: https://opendata-cosagis.opendata.arcgis.com/ ; https://www.sanantonio.gov/GIS/GISData

**Battery ROI benchmarks:**
- Brattle Group / Sargent & Lundy, *ERCOT CONE for 2026* (June 10, 2024): https://www.brattle.com/wp-content/uploads/2024/08/ERCOT-CONE-for-2026.pdf
- NREL, *Cost Projections for Utility-Scale Battery Storage: 2025 Update*: https://docs.nrel.gov/docs/fy25osti/93281.pdf ; https://atb.nrel.gov/electricity/2025/utility-scale_battery_storage
- Lazard, *Levelized Cost of Energy+* (June 2025, includes LCOS): https://www.lazard.com/media/eijnqja3/lazards-lcoeplus-june-2025.pdf
