# Decision log

These are owner and lead decisions that supersede older spec text. Newest first. Each one lists the docs that must
reflect it. Spec, epic, story and test-plan updates are tracked in `10-traceability-matrix.md` (CONFLICTS).

| # | Date | Decision | Source | Affects |
|---|---|---|---|---|
| D-27 | 2026-09-26 | **The guardian independently enforces every discharge-flow limit:** SoC/temperature derating, per-home export at the meter, transformer, feeder and substation interconnection limits (including reverse flow), territory, and sustained vs peak. Missing data fails closed. | Owner | 00-invariants (K4 or new), 02a §6 guardian, 09 |
| D-26 | 2026-09-26 | **The dispatcher models max discharge flow at every level:** P_max(SoC, T), the home export limit net of home load, service transformer, feeder, substation, and sustained vs peak. | Owner | 02a, 09 |
| D-25 | 2026-09-26 | **20% SoC discharge floor confirmed by Base** (already built: K1, G-01, G-01-ENERGY). | Base via owner | 02b §4.2 (confirmation note) |
| D-24 | 2026-09-26 | **Solar expansion widens the price swings.** Forecast a solar-driven intraday shape; charge at midday as well as overnight; hold energy for the evening ramp. | Base via owner | 08 §3c, 09, forecast spec |
| D-23 | 2026-09-26 | **ROI planning assumptions:** "< $500/kW" is a NET effective investment, and the target payback is about 3 years. The §3c illustrative stack was confirmed reasonable. | Owner | 08 §3c |
| D-22 | 2026-09-26 | **Charging mix:** at least 30% solar; the rest at the utility's night or off-peak rates (Austin Energy). | Base via owner | 08, 09 |
| D-21 | 2026-09-26 | **First regulated utility:** Austin Energy or CPS Energy. **Substation battery sets of about 20 MW. Base owns all batteries. Home batteries are in scope for regulated contracts.** | Base via owner | 08, 02b (asset model), 09 |
| D-20 | 2026-09-26 | **Two markets:** one regulated (a vertically integrated utility is the customer, with premium capacity and territory-bound energy) plus one free market (ERCOT). Economics are $/kW in vs $/kW out. | Base via owner | 08 (new), 01, 02a, 03, 09 |
| D-19 | 2026-09-26 | **M1 delivery charge:** assume the FULL TDSP charge on grid charging; a flat per-kWh rate, per TDSP; the PUCT 2026-09-01 rates are in `config/tdsp_tariffs.toml`. | Owner | 02a §3.4, 04-external-data-integration, settle |
| D-18 | 2026-09-26 | **Commitments are over a period, on a FIXED (schedule) or NEED (measured) basis.** A need-basis commitment is a reserved maximum that's never resold; G-19 accepts R-GRANT-CLOSED-LOOP only after its own checks. | Owner | 00-invariants (done), 02a §5 and §6, 06 |
| D-17 | 2026-09-26 | **A mid-window SHORTFALL is best effort:** keep delivering the maximum feasible, restore the full commitment quickly, set AT_RISK, and settle on actual delivery. It never stops for the rest of the window. | Owner | 00-invariants (done), 02a (escalation), 03 stories, 04 tests |
| D-16 | 2026-09-26 | **The base server (192.168.5.35) is the permanent host.** | Owner | 01, 02b, the chaos design |
| D-15 | 2026-09-26 | **Keep the EIA key** (no rotation); logging of it is fixed. | Owner | none |
| D-14 | 2026-09-26 | **ftbrown is a team member.** | Owner | WORKBOARD |
| D-13 | 2026-09-26 | **Per-workspace MQTT users** (`ogw_<ws>`, `ogtest/<ws>/#` only); workspaces never receive production MQTT credentials. | Lead (delegated) | BUILD.md §5 (done), 02b §6 |
| D-12 | 2026-09-26 | **Test accounts on dev:** named operators og-op-a and og-op-b (the two-person stop release) and customers og-cust-*; the API trusts identity only with the Apache proxy secret. | Owner | 02b §8 (auth), 03 (UI/ops stories) |
| D-11 | 2026-09-26 | **Customer-operator simulators** (ogsim/customer) plus a customer API, site ingest and closed-loop controllers (built dark). | Owner | 02b, 03, 04, 06 |
| D-10 | 2026-09-26 | **The delivery-rate economics use per-bank load-zone pricing.** Hub prices are reference only (the Houston Hub bug is fixed). | Frank's review | 02a §6.1 |
| D-9 | 2026-09-26 | **Dual-unit homes are spread 10 per bank; banks stay single-zone.** | Lead (Frank #4) | 02b §4.2, seed |
| D-8 | 2026-09-25 | Inverter terms: **substitution** means a different hub; **inverter swap** means a hardware replacement; remote **calibration** comes first. | Owner | 06 (done) |
| D-7 | 2026-09-25 | PQ spec approved with amendments (DATA_CENTER profile, K14, G-21..G-25, continuous monitoring, waveforms over SCADA, arbitrage at the grid-code minimum). | Owner | 06 (done) |
| D-6 | 2026-09-25 | Energy sufficiency is checked continuously (energy, not only kW). | Owner | 00-invariants (done) |
| D-5 | 2026-09-25 | Hardware: 39.2 kWh / 11 kW per unit; 20% of homes dual-unit (78.4 kWh / 20 kW); about 600 kVA banks. | Owner/Base | 02b §4.2 (done) |
| D-4 | 2026-09-25 | Commitment lock: once committed, delivery completes; no mid-contract switching on price. | Owner | 00-invariants K13 (done) |
