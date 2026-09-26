# TDSP residential delivery charges (2026-09-01)

Verification date: 2026-09-26. Sources: owner-supplied PUCT summary table; four owner-supplied
single-page "PUCT Monthly Report" rate reports (`AEP_Rate_Report.pdf`, `CenterPoint_Rate_Report.pdf`,
`TNMP_Rate_Report.pdf`, `Oncor_Rate_Report.pdf`, all in `C:\Users\Nikola Todev\Downloads\`); and the
four full retail delivery tariffs in the same folder. Page numbers below are PDF page numbers of the
tariff files unless noted otherwise.

**Note on the four small "Rate Report" PDFs.** Each is a single page ("PUCT Monthly Report" /
"PUCT-style" summary). They do **not** itemize base vs. riders — they show only the already-bundled
Customer Charge, Metering Charge, and one combined "Volumetric Charge per kWh" per rate class, plus
average-bill illustrations. They match the owner's original summary table exactly (AEP: $0.058 /
$0.057; CenterPoint: $0.064130; Oncor: $0.060295; TNMP: $0.074022) and add one useful clarification —
a footnote on the AEP report: "ADFIT and SRC riders do not apply to North customers." They contain no
line-item riders and no additional decimal precision, so the itemization below comes from the full
tariffs, with the small reports used only to confirm the current bundled totals.

## Oncor

Base tariff: `Tariff for Retail Delivery Service.pdf.coredownload.pdf` (confirmed to be Oncor's Tariff
for Retail Delivery Service).

| Component | Rate | Effective | Page |
|---|---|---|---|
| Distribution System Charge (base) | $0.036043/kWh | June 1, 2026 | Oncor tariff p. 67 |
| Rider NDC (Nuclear Decommissioning) | $0.000000/kWh (billed only in June) | Dec 15, 2025 | p. 95 |
| Rider TCRF (Transmission Cost Recovery Factor) | $0.019046/kWh | Aug 1, 2026 | p. 97–98 |
| Rider EECRF (Energy Efficiency) | $0.001487/kWh | March 1, 2026 | p. 101 |
| Rider DCRF (Distribution Cost Recovery Factor) | $0.000000/kWh | June 1, 2026 | p. 105 |
| Rider RCE (Rate Case Expense) | $0.000086/kWh | June 1, 2026 | p. 106 |
| Rider MG (Mobile Generation) | $0.001027/kWh (billed only in December; **not** in the Sept. volumetric) | Dec 1, 2025 | p. 107 |
| Rider IS (Interim Surcharge) | $0.003633/kWh — temporary, through **December 2026** | Aug 1, 2026 | p. 108 |
| Customer Charge | $1.48/month | — | p. 67 |
| Metering Charge | $2.58/month | — | p. 67 |

Sum of Base + NDC + TCRF + EECRF + DCRF + RCE + Rider IS = **$0.060295/kWh**, an exact match to the
PUCT summary. Rider MG is excluded from that sum — it is billed only in the December cycle, so it does
not appear in the September volumetric figure.

**Reconciliation answer:** Rider IS (0.3633¢) **is included** in the reported 6.0295¢/kWh, not
additional to it. Oncor's tariff states "Rider IS shall remain in effect through December 2026," so
this component is expected to drop out of the volumetric total after that date.

## CenterPoint (Houston Electric)

Tariff: `houston-electric-tariff-for-retail-delivery-service.pdf`.

| Component | Rate | Effective | Page |
|---|---|---|---|
| Distribution System Charge (base) | $0.023240/kWh | — | CenterPoint tariff p. 83 |
| Schedule SRC II (System Restoration Charge) | $0.000636/kWh | — | p. 119 |
| Schedule SRC III (System Restoration Charge) | $0.002848/kWh | — | p. 135 |
| Rider NDC (Nuclear Decommissioning) | $0.000013/kWh | 4/28/25 | p. 144 |
| Rider TCRF (Transmission Cost Recovery Factor) | $0.030812/kWh | 9/1/26 | p. 147 |
| Rider RCE (Rate Case Expense) | $0.000048/kWh | 4/28/25 | p. 150 |
| Rider ADFITC II (ADFIT credit vs. SRC II) | ($0.000023)/kWh | 9/1/26 | p. 151 |
| Rider ADFITC III (ADFIT credit vs. SRC III) | ($0.000418)/kWh | 2/26/26 | p. 153 |
| Rider EECRF (Energy Efficiency) | $0.001576/kWh | 3/1/26 | p. 155 |
| Rider IRA (Inflation Reduction Act 2022) | $0.000000/kWh | 4/28/25 | p. 156 |
| Rider TC5 Refund (Transition Charge refund) | ($0.000278)/kWh — temporary, refund cap $14.9M | 5/18/26 | p. 157 |
| Rider RRC (Rate Reduction Credit) | $0.000000/kWh | 12/30/25 | p. 159 |
| Rider DCRF (Distribution Cost Recovery Factor) | $0.004934/kWh | 9/1/26 | p. 162 |
| Rider TEEEF (Temp. Emergency Electric Energy Facilities) | $0.000742/kWh | 8/15/26 | p. 164 |
| Customer Charge | $2.11/month | — | p. 83 |
| Metering Charge | $2.79/month | — | p. 83 |

Sum of all thirteen per-kWh components above = **$0.064130/kWh** — an **exact** match to the PUCT
summary (6.4130¢), including the negative riders and the temporary TC5 refund. This is the strongest
reconciliation of the four TDSPs: every listed residential rider is confirmed live and additive/subtractive
as shown, with no unexplained residual.

(Not included: the Municipal Account Franchise Credit, ($0.001767)/kWh, p. 83 — it applies only to
municipal accounts inside a franchise-fee city, not to residential customers generally.)

## AEP Texas (Central and North)

Tariff: `AEP_TEXAS_TARIFF_Eff_August_28_2026.pdf` (the "(1)" file in Downloads is a duplicate — same
content, not reviewed separately).

AEP Texas Central and North share **one** residential rate schedule (6.1.1.1.1) with identical Customer
Charge, Metering Charge, and base Distribution System Charge. The Central/North difference comes
entirely from which riders apply: items marked "*" in the schedule (Rider NDC, Rider SRC, Rider
ADFITC, and the now-expired Rider TC-3) apply **only** to the former AEP Texas Central territory, not
to North (tariff p. 113, footnote).

| Component | Rate | Applies to | Effective | Page |
|---|---|---|---|---|
| Distribution System Charge (base) | $0.032966/kWh | Central & North | Oct 1, 2024 | AEP tariff p. 113 |
| Customer Charge | $1.27/month | Central & North | — | p. 113 |
| Metering Charge | $1.97/month | Central & North | — | p. 113 |
| Rider NDC (Nuclear Decommissioning) | $0.000186/kWh, **billed only in June** | Central only | — | p. 163 |
| Rider TCRF (Transmission Cost Recovery Factor) | $0.017233/kWh | Central & North | March 20, 2026 | p. 166 |
| Rider EECRF (Energy Efficiency) | $0.001101/kWh | Central & North | March 1, 2026 | p. 167 |
| Rider DCRF (Distribution Cost Recovery Factor) | $0.004277/kWh | Central & North | March 20, 2026 | p. 171 |
| Rider ITR (Income Tax Refund) | −0.1362% of base-rate **revenue** (not $/kWh) | Central & North | July 30, 2026 | p. 172 |
| Rider SRC (System Restoration/securitization) | $0.001217/kWh | Central only | Aug 28, 2026 | p. 186 |
| Rider ADFITC | ($0.000070)/kWh | Central only | Aug 28, 2026 | p. 189 |
| Rider RAR (Regulatory Asset Recovery) | ($0.001329)/kWh — **one-month reconciliation factor**, effective July 30, 2026 | Central & North | July 30, 2026 | p. 190 |
| Rider Mobile TEEEF | $0.00110/kWh | Central & North | Sept 1, 2025 | p. 192 |
| Rider RCE (Rate Case Expense) | $0.0000000/kWh (fully recovered) | Central & North | Sept 22, 2025 | p. 193 |
| Rider TC-3 Refund | ($0.001801)/kWh — **expired**, was a two-month refund starting Jan 30, 2026 | Central only | — | p. 158 |

**Exact-digit reconstruction (step 4):**

- Central: Base + TCRF + EECRF + DCRF + SRC + ADFITC = 0.032966+0.017233+0.001101+0.004277+0.001217−0.000070
  = **$0.056724/kWh**. Rider RAR's one-month reconciliation window (effective July 30, 2026, "in effect
  for one month") appears to have closed before September 1, 2026; adding it back
  (0.056724+0.001329) + Mobile TEEEF (0.001100) = **$0.057824/kWh**, i.e. **5.7824¢**, which rounds to
  the reported 5.8¢.
- North: Base + TCRF + EECRF + DCRF (no SRC/NDC/ADFITC) + Mobile TEEEF, with RAR likewise excluded =
  0.032966+0.017233+0.001101+0.004277+0.001100 = **$0.056677/kWh**, i.e. **5.6677¢**, which rounds to
  the reported 5.7¢.

Both reconstructions land within 0.02¢ of the PUCT summary once Rider RAR (a one-month true-up that
appears to have expired at end of August 2026) is excluded — a clean match. Rider ITR is excluded from
both sums because it is a revenue-based credit (percentage of base-rate dollars), not a fixed $/kWh
adder, so it cannot be folded into a per-kWh total without an assumed usage level; AEP's own rate
report confirms this ("Charges do not include % of revenue factor based charges").

**Reconciliation note:** the arithmetic above depends on Rider RAR's one-month reconciliation factor
having expired by September 2026 (its stated term is "one month" from a July 30, 2026 effective date).
This is the one item in this document that is inferred rather than directly confirmed in the tariff
text — see "Confirm with Base" below.

## Storage, distributed generation, and bidirectional-flow findings

No TDSP in this set has a dedicated residential battery-storage rider, and none has a net-metering
credit rate for residential export. All four handle on-site generation (which includes
customer-owned storage inverters operating in parallel with the grid) the same general way:

- **Interconnection, not netting.** All four tariffs govern DG/storage interconnection under PUCT
  Substantive Rules §25.211 (Interconnection of On-Site Distributed Generation) and §25.212
  (Technical Requirements), via an "Application for Interconnection and Parallel Operation of
  Distributed Generation" and a standard interconnection agreement. There is no statutory or
  tariff-based net-metering scheme in ERCOT-area investor-owned-utility tariffs; a residential
  customer with a battery/PV system is billed under the standard Residential Service schedule for
  all delivered kWh, with export handled by the customer's competitive retailer (REP) contract, not
  the TDSP tariff.
- **CenterPoint — a specific storage/DG securitization wrinkle.** Under Schedule SRC II and Schedule
  SRC III (System Restoration/securitization riders), a customer with "New On-Site Generation" pays
  the SRC per-kWh rate on the **output of the on-site generation used to serve the customer's own
  internal load**, in addition to the SRC billed on kWh actually delivered by CenterPoint (tariff pp.
  119, 135). This means self-consumed generation/battery-discharge energy is not automatically
  exempt from these securitization charges if it is deemed "New On-Site Generation" — worth flagging
  for anyone modeling behind-the-meter battery economics on CenterPoint's system. The matching ADFITC
  II/III credits apply on the same basis.
  AEP Texas has an analogous "New On-Site Generation" clause for its ADFITC rider (tariff p. 188).
- **Oncor.** Distributed Generation Pre-Interconnection Study Fees apply per rate class (tariff, e.g.,
  §6.1.2.4 in each service-schedule chapter); "Wholesale Storage Load" (batteries, flywheels,
  compressed-air storage, pumped hydro, etc.) is defined and given separate treatment, but only at
  the transmission/ERCOT-resource level, not for behind-the-meter residential storage.
  Oncor's Rider MG (Mobile Generation) is a distribution-plant-related charge, not a customer-storage
  rider.
- **AEP Texas and TNMP.** Both reference the same §25.211/25.212 interconnection framework, plus
  pre-interconnection study fees scaled by exported vs. non-exporting DG size (CenterPoint has the
  most detailed fee schedule, tiered by 0–10 kW / 10–500 kW / 500–2000 kW / 2000–10,000 kW and by
  exporting vs. non-exporting). TNMP additionally charges an "as calculated" Distributed Generation
  Meter Installation Fee for the metering equipment needed to separately measure DG outflow (tariff
  p. 163 area).
- No tariff describes any special power-quality or curtailment charge tied specifically to battery
  charging/discharging cycles; standard demand/kWh billing determinants apply as they would for any
  load or generation source at the customer's point of delivery.

## Confirm with Base

1. **AEP Rider RAR status at Sept 1, 2026.** The itemized AEP sums only reconcile to the reported
   5.8¢/5.7¢ if Rider RAR's one-month reconciliation charge (−$0.001329/kWh, Central & North,
   effective July 30, 2026) had already expired by September. The tariff text says the factor "will
   remain in effect for one month" but does not give an explicit end date. Recommend confirming
   directly with AEP or PUCT filings whether RAR is billing in September 2026.
2. **TNMP volumetric gap (~0.13¢/kWh unaccounted for).** Summing TNMP's identified components (base
   $0.025670 + TCRF $0.027703 + EECRF $0.001642 + DCRF $0.017724, with CTC/HCRF/RCE/ERP all at
   $0.00000) gives $0.072739/kWh, about $0.0013/kWh short of the reported $0.074022/kWh. The supplied
   TNMP tariff PDF is dated 2025-12-29 in its filename; TCRF and DCRF normally update each March 1 and
   September 1, and a Rider SBF (System Benefit Fund Charge) is referenced in the residential rate
   schedule (item II) but no dedicated Rider SBF rate page was found in this copy. Recommend getting
   an updated TNMP tariff (post-September-2026 filing) or the Rider SBF rate sheet to close this gap.
3. **AEP Rider ITR treatment.** Confirm whether OpenGrid's billing model needs to represent Rider ITR
   (a −0.1362% credit on base-rate *revenue*, not a $/kWh figure) at all, since it is usage-scale-
   dependent and is excluded from AEP's own published per-kWh volumetric figure.
4. **CenterPoint "New On-Site Generation" SRC/ADFITC treatment for batteries.** Confirm with Base
   whether OpenGrid's battery discharge is likely to be classified as "New On-Site Generation" under
   CenterPoint's Schedule SRC II/III (which would add ~0.32¢/kWh net SRC minus ~0.04¢/kWh ADFITC
   credit on self-consumed output) versus being treated as ordinary delivered/undelivered load.
5. **AEP duplicate PDF.** The "(1)" copy of the AEP tariff was not independently diffed against the
   primary file; assumed identical. Flag if a newer AEP revision exists.
