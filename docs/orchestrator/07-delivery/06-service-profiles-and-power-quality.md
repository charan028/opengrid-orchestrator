# 06 — Service Profiles and Power Quality (MVP-S+ increment)

Status: **Approved by owner 2026-09-25 (with amendments)**. Implements RC-3 and RC-4 of `01-saturday-delivery-plan.md`
§0b (acceptance A12/A13). **K14 is now canonical** in `00-invariants.md` (owner decision 2026-09-25), including
guardian checks G-21..G-24. This is a spec document — no product code is written here. It extends the dispatch-profile
model of `02-architecture/03-decision-engine.md` §2.5–2.7 and §8.6, the six control primitives of
`06-reviews/06-first-principles-review.md` §4, and the MVP-S delivery of `07-delivery/02a`/`02b`; it does not replace
any of them. Every new invariant, guardian check, schema field and DDL table is additive.

**Owner decisions recorded 2026-09-25 (amendments to the draft):**

1. `DATA_CENTER` as a new profile through the full `03 §2.7` activation gate — **accepted as written** (§4.b, §9).
2. Additive-only schema/DDL — **agreed** (§1.5 unchanged).
3. K14 + guardian checks G-21..G-24 — **agreed**; `00-invariants.md` updated to canonical, guardian check list amended.
4. PQ-aware selection **amended**: it is not enough to filter eligible hubs for *uncommitted* headroom (§5.2). Power
   quality on **already-committed** deliveries must now be monitored continuously and corrected in place — §5.4
   (new) specifies the monitoring loop, corrective-action ladder, `AT_RISK` escalation and PQ M&V/settlement.
5. **Amended**: harmonics are not modelled estimates alone. Each hub inverter streams real waveform telemetry over
   the SCADA network; the orchestrator ingests, analyzes and serves it. §3's aggregation formulas are retained
   explicitly as a **pre-delivery planning/forecast cross-check**, not the delivery-time source of truth. §6.4–§6.6
   (new) specify the waveform payload, transport and bandwidth, the ingestion/storage/analysis pipeline, and the
   query API/UI; §7 (simulator) is extended to generate and publish real per-inverter waveforms.
6. `ERCOT_ENERGY` keeps the grid-code-minimum envelope only — **unchanged** (§4.c).
7. **Added**: a strict terminology split, used consistently from here on — **substitution** is the software dispatch
   action of moving a committed obligation's delivery to *different hubs*, within seconds, under K13's
   `R-SUBSTITUTION` exception; **inverter swap / replacement** is a *physical hardware* action (a technician replaces
   a unit's inverter). The word "swap" is never used for a dispatch action in this document.
8. **Added**: an asset-health and maintenance workflow for inverter calibration, including a **remote calibration**
   step the orchestrator can attempt before any hardware replacement — §5.5 (workflow), §6.7 (calibration command/ack
   interfaces), §7.5 (simulator support), §8 (DDL, stories, tests), §9 (work packages).

**Why this document exists.** The nine dispatch profiles already differ in signal, request, control and M&V (§2.6 of
`03-decision-engine.md`). What they do not yet carry is a *machine-checkable, per-contract* statement of the electrical
service the customer is actually buying — which quantity is regulated, to what tolerance, on which phase(s), and
inside what voltage/current/frequency/harmonic envelope — nor a model of how imperfect, DC-behind-inverter hubs
combine to deliver (or violate) that envelope. Pipeline AC mitigation, a data-center firm-capacity contract and ERCOT
energy arbitrage are three different electrical products sharing one fleet; today the profile schema (§2.5 of `03`)
lets them differ in *dispatch logic* but not in *quality guarantees*. This document adds that layer.

---

## 1. The ServiceProfile model

### 1.1 Relationship to the existing dispatch profile

`03-decision-engine.md` §2.5 already defines a **dispatch profile** as eight elements (signals, request, admission,
control, arbitration, performance, M&V/billing, failure). `ServiceProfile` in this document is **not a tenth
element or a new object** — it is the concrete, per-contract *instance* of dispatch-profile elements 4 (control) and
6 (performance) plus one addition: a `pq_envelope` reference. Where §2.5's table names a *building block* (e.g.
`CLOSED_LOOP_REGULATION`), `ServiceProfile` names the *bound parameters* the allocator and guardian read at run time:
which quantity, which controller gains, which tolerance, which customer envelope. Concretely, `ServiceProfile` is
carried on `og.contract` (new columns, §1.4) and `og.product_rule` continues to carry commercial divisibility
(`min_qty_kw`, `increment_kw`, `block`) unchanged.

### 1.2 The six control primitives, restated as a closed enum

Per `06-first-principles-review.md` §4 (P1, P2), every profile decomposes into one of six control primitives. This
document fixes them as the `control_primitive` enum every `ServiceProfile` must pick exactly one of:

| Primitive | Meaning | Existing `03 §2.5` control-block examples |
|---|---|---|
| `OPEN_LOOP_SCHEDULE` | Follow a pre-computed setpoint trajectory; no feedback on a measured quantity | `SCHEDULED_NPC`, `TOLLING_SCHEDULE`, `HOME` reserve floors |
| `CLOSED_LOOP_REGULATION` | Drive a measured quantity to a target inside a deadband, with a feedback controller | `CLOSED_LOOP_REGULATION` (bank kVA), `BAND_SMOOTHING` (pipeline), `ISO_NPC_REGULATION` (ADER net power), `SELF_SERVE_NET_LOAD` |
| `PRICE_RESPONSE` | Act only when a price/value signal crosses a threshold, inside energy/power limits | `PRICE_RESPONSIVE` |
| `CAPACITY_HOLD` | Reserve power/energy against a call that may or may not arrive; deliver on instruction | `CAPACITY_HOLD` (AS ring-fence) |
| `EVENT_SCHEDULE_TRACKING` | Track a declared event or deployment profile to a compliance band | `EVENT`, `ISO_XML_DEPLOYMENT`, `NCLR_DEPLOYMENT_BAND` |
| `MODE_ISLAND_CONTROL` | Change operating mode (grid-parallel ↔ island) under an external switching authority | `ISLAND_FORMING`, `MODE_CONTROL` |

A `ServiceProfile` names one primitive. Profiles that appear to mix primitives over time (e.g. `ERCOT_ENERGY`'s ALR
online vs. off-line behaviour) are two `ServiceProfile` instances bound to the same contract by an eligibility
condition (`online_ader` true/false), not one profile with a variable primitive — this keeps the guardian's read of
"what is this contract allowed to do right now" a single lookup.

### 1.3 Fields

| Field | Type | Meaning |
|---|---|---|
| `service_profile_id` | uuid | Primary key |
| `contract_id` | uuid FK → `og.contract` | Owning contract |
| `version` | int | Monotonic; a running event keeps its version (V-27, unchanged) |
| `control_primitive` | enum (§1.2) | The one primitive this profile executes |
| `target_quantity` | enum: `KW`, `KVAR`, `LINE_CURRENT_A`, `PIPE_TO_SOIL_V`, `BANK_KVA`, `NET_POWER_MW`, `NONE` | The physical quantity the control loop regulates; `NONE` for `OPEN_LOOP_SCHEDULE`/`MODE_ISLAND_CONTROL` |
| `target_scope` | enum: `HUB`, `BANK`, `FEEDER`, `CORRIDOR_LINE`, `ADER`, `SITE_METER` | What the target quantity is measured over |
| `setpoint_source` | enum: `PLAN` (day-ahead/intraday schedule), `MEASURED_FEEDBACK` (closed loop), `ISO_INSTRUCTION`, `PRICE_FEED`, `CUSTOMER_API` | Where this cycle's setpoint comes from |
| `feedback_signal_ref` | string, nullable | The measured point the controller reads (SCADA point ID, hub telemetry field, or corridor sensor ID); required when `setpoint_source = MEASURED_FEEDBACK` |
| `response_time_s` | number | Time from admitted request to ≥ 90% of target (design deadline) |
| `ramp_limit` | number, unit-typed (`kw_per_min`, `a_per_min`, …) | Maximum rate of change of the delivered quantity |
| `sustain_duration_s` | number, nullable | Minimum time the target must be held once reached (event/AS profiles) |
| `accuracy_tolerance` | number | Allowed steady-state error, in the target quantity's unit |
| `deadband` | number | No-action band around the setpoint, same unit |
| `priority_tier` | enum: `L0`, `L1`, `L2`, `T1`, `T2`, `T3`, `T4` | Unchanged from `og.contract.tier` (§2.3 of `03`); duplicated here only for the conformance check that a profile's tier matches its contract |
| `mv_method` | enum (§2.5 element 7 library, unchanged) | e.g. `DIRECT_HUB_METER`, `SCADA_OUTCOME` |
| `settlement_metric` | string | The specific computed quantity billed (e.g. `achieved_line_current_delta_a`, `capacity_payment_x_pf`, `energy_x_price`) |
| `pq_envelope_id` | uuid FK → `og.pq_envelope` | The customer's PowerQualityEnvelope (§2) |
| `failure_behaviour` | enum (§2.5 element 8 library, unchanged) | e.g. `NEUTRAL_ON_SIGNAL_LOSS`, `HOLD_THEN_SCHEDULE` |

### 1.4 JSON Schema

New file `interfaces/contracts/service_profile.schema.json` (contract-side object, not an MQTT message — validated at
profile authoring and admission time per §2.7 of `03`):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://opengrid.example/interfaces/contracts/service_profile.schema.json",
  "title": "ServiceProfile",
  "type": "object",
  "additionalProperties": false,
  "required": [
    "service_profile_id", "contract_id", "version", "control_primitive", "target_quantity",
    "target_scope", "setpoint_source", "response_time_s", "ramp_limit", "accuracy_tolerance",
    "deadband", "priority_tier", "mv_method", "settlement_metric", "pq_envelope_id", "failure_behaviour"
  ],
  "properties": {
    "service_profile_id": { "type": "string", "format": "uuid" },
    "contract_id": { "type": "string", "format": "uuid" },
    "version": { "type": "integer", "minimum": 1 },
    "control_primitive": {
      "type": "string",
      "enum": ["OPEN_LOOP_SCHEDULE", "CLOSED_LOOP_REGULATION", "PRICE_RESPONSE", "CAPACITY_HOLD",
               "EVENT_SCHEDULE_TRACKING", "MODE_ISLAND_CONTROL"]
    },
    "target_quantity": {
      "type": "string",
      "enum": ["KW", "KVAR", "LINE_CURRENT_A", "PIPE_TO_SOIL_V", "BANK_KVA", "NET_POWER_MW", "NONE"]
    },
    "target_scope": { "type": "string", "enum": ["HUB", "BANK", "FEEDER", "CORRIDOR_LINE", "ADER", "SITE_METER"] },
    "setpoint_source": { "type": "string", "enum": ["PLAN", "MEASURED_FEEDBACK", "ISO_INSTRUCTION", "PRICE_FEED", "CUSTOMER_API"] },
    "feedback_signal_ref": { "type": ["string", "null"] },
    "response_time_s": { "type": "number", "exclusiveMinimum": 0 },
    "ramp_limit": { "type": "number", "exclusiveMinimum": 0 },
    "ramp_limit_unit": { "type": "string", "enum": ["kw_per_min", "a_per_min", "kvar_per_min", "mw_per_min"] },
    "sustain_duration_s": { "type": ["number", "null"], "minimum": 0 },
    "accuracy_tolerance": { "type": "number", "minimum": 0 },
    "deadband": { "type": "number", "minimum": 0 },
    "priority_tier": { "type": "string", "enum": ["L0", "L1", "L2", "T1", "T2", "T3", "T4"] },
    "mv_method": { "type": "string" },
    "settlement_metric": { "type": "string" },
    "pq_envelope_id": { "type": "string", "format": "uuid" },
    "failure_behaviour": { "type": "string" }
  },
  "allOf": [
    { "if": { "properties": { "setpoint_source": { "const": "MEASURED_FEEDBACK" } } },
      "then": { "required": ["feedback_signal_ref"] } },
    { "if": { "properties": { "control_primitive": { "const": "CLOSED_LOOP_REGULATION" } } },
      "then": { "not": { "properties": { "target_quantity": { "const": "NONE" } } } } }
  ]
}
```

### 1.5 DB DDL delta (migration `0009_service_profile.sql`, additive only)

```sql
CREATE TABLE og.pq_envelope (
    pq_envelope_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id         uuid NOT NULL,
    phase_config        text NOT NULL CHECK (phase_config IN ('1P','SPLIT_PHASE','3P')),
    max_phase_imbalance_pct numeric(5,2) NOT NULL DEFAULT 3.0,
    voltage_band_pct    numeric(5,2) NOT NULL DEFAULT 5.0,        -- +/- % of nominal
    ride_through_class  text NOT NULL DEFAULT 'CATEGORY_III',      -- IEEE 1547-2018 style label
    current_limit_a     numeric(10,2),                             -- NULL = no per-line cap
    current_limit_scope text CHECK (current_limit_scope IN ('PER_PHASE','SPECIFIC_LINE',NULL)),
    freq_tolerance_hz   numeric(5,3) NOT NULL DEFAULT 0.5,
    rocof_limit_hz_s    numeric(5,3),
    pf_min              numeric(4,3) NOT NULL DEFAULT 0.90,
    reactive_requirement text,                                     -- free text: e.g. "unity +/-0.02" or "volt-var per IEEE 1547"
    thd_voltage_limit_pct numeric(5,2) NOT NULL DEFAULT 5.0,        -- IEEE 519 style
    thd_current_limit_pct numeric(5,2) NOT NULL DEFAULT 5.0,
    flicker_pst_limit   numeric(5,2),                               -- NULL = not applicable
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE og.service_profile (
    service_profile_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_id         uuid NOT NULL REFERENCES og.contract(contract_id),
    version             int  NOT NULL DEFAULT 1,
    control_primitive   text NOT NULL CHECK (control_primitive IN
        ('OPEN_LOOP_SCHEDULE','CLOSED_LOOP_REGULATION','PRICE_RESPONSE','CAPACITY_HOLD',
         'EVENT_SCHEDULE_TRACKING','MODE_ISLAND_CONTROL')),
    target_quantity     text NOT NULL CHECK (target_quantity IN
        ('KW','KVAR','LINE_CURRENT_A','PIPE_TO_SOIL_V','BANK_KVA','NET_POWER_MW','NONE')),
    target_scope        text NOT NULL CHECK (target_scope IN ('HUB','BANK','FEEDER','CORRIDOR_LINE','ADER','SITE_METER')),
    setpoint_source      text NOT NULL CHECK (setpoint_source IN
        ('PLAN','MEASURED_FEEDBACK','ISO_INSTRUCTION','PRICE_FEED','CUSTOMER_API')),
    feedback_signal_ref  text,
    response_time_s      numeric(8,2) NOT NULL,
    ramp_limit           numeric(10,3) NOT NULL,
    ramp_limit_unit      text NOT NULL DEFAULT 'kw_per_min',
    sustain_duration_s   numeric(10,2),
    accuracy_tolerance   numeric(10,4) NOT NULL,
    deadband             numeric(10,4) NOT NULL,
    priority_tier        text NOT NULL,
    mv_method            text NOT NULL,
    settlement_metric    text NOT NULL,
    pq_envelope_id       uuid NOT NULL REFERENCES og.pq_envelope(pq_envelope_id),
    failure_behaviour    text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_feedback_required CHECK (
        setpoint_source <> 'MEASURED_FEEDBACK' OR feedback_signal_ref IS NOT NULL)
);
CREATE INDEX ix_service_profile_contract ON og.service_profile(contract_id, version DESC);

-- Inverter PQ characterization per hub (§3), refreshed from a periodic sim/estimation job, never billed directly.
CREATE TABLE og.hub_inverter_pq (
    hub_id               text PRIMARY KEY REFERENCES og.hub(hub_id),
    phase_connection     text NOT NULL CHECK (phase_connection IN ('A','B','C','AB','BC','CA','ABC')),
    kva_rating           numeric(8,2) NOT NULL,
    pf_min_leading       numeric(4,3) NOT NULL DEFAULT 0.90,
    pf_min_lagging       numeric(4,3) NOT NULL DEFAULT 0.90,
    freq_offset_hz       numeric(6,4) NOT NULL DEFAULT 0,     -- mean steady-state offset from nominal
    freq_offset_std_hz   numeric(6,4) NOT NULL DEFAULT 0.01,
    voltage_offset_pct   numeric(6,4) NOT NULL DEFAULT 0,
    voltage_offset_std_pct numeric(6,4) NOT NULL DEFAULT 0.5,
    thd_current_pct      numeric(5,2) NOT NULL DEFAULT 3.0,
    dominant_harmonics    jsonb,                              -- e.g. {"3": {"mag_pct": 1.2, "angle_deg": 40}, "5": {...}}
    phase_angle_error_deg numeric(6,3) NOT NULL DEFAULT 0,
    response_time_ms      numeric(8,2) NOT NULL DEFAULT 200,
    ride_through_class    text NOT NULL DEFAULT 'CATEGORY_III',
    quality_score         numeric(4,3) NOT NULL DEFAULT 1.0,  -- 0..1, derived, §5.1
    last_estimated_at      timestamptz
);
```

`og.contract` is unchanged; a contract may have many `og.service_profile` versions (V-27 semantics carried over
verbatim: a running event keeps its bound version).

---

## 2. PowerQualityEnvelope

`og.pq_envelope` (DDL above) is per customer (a data-center or pipeline customer has exactly one; a residential
aggregate uses a fleet-default envelope shared by all `HOME` contracts unless the utility's interconnection agreement
sets a tighter one). Fields, with the standards they track (cited by name only, per instruction):

| Field | Meaning | Standard reference |
|---|---|---|
| `phase_config` | `1P` (single-phase home), `SPLIT_PHASE` (120/240 V US residential, two legs of one phase), `3P` (data center, pipeline compressor station) | — |
| `max_phase_imbalance_pct` | Maximum allowed negative-sequence voltage or current imbalance, IEEE 1159 / NEMA MG-1 style definition: $100\times\max_\phi\lvert I_\phi-\bar I\rvert/\bar I$ | NEMA MG-1 (motor-load derating basis); IEEE 1159 (imbalance definition) |
| `voltage_band_pct` | ± % of nominal the service point must stay within in steady state | ANSI C84.1 Range A (±5%) is the residential default; data-center contracts typically tighten to ±2–3% |
| `ride_through_class` | `CATEGORY_I/II/III` sag/swell ride-through curve the inverter fleet must not trip on | IEEE 1547-2018 Table 15 (Category I/II/III voltage ride-through) |
| `current_limit_a` / `current_limit_scope` | A hard current cap, either per phase (data center service entrance) or on a named line (`PIPELINE_AC`'s monitored corridor conductor) | — (contract- or corridor-specific) |
| `freq_tolerance_hz` / `rocof_limit_hz_s` | Steady-state frequency band and rate-of-change-of-frequency limit before the hub's own droop/trip logic engages | IEEE 1547-2018 Table 16-18 (frequency ride-through, default trip settings); ERCOT's UFLS/ROCOF guidance for the ROCOF figure |
| `pf_min` / `reactive_requirement` | Minimum power factor, or an explicit Q-setpoint / volt-var curve requirement | IEEE 1547-2018 §5 (reactive power capability, Category B) |
| `thd_voltage_limit_pct` / `thd_current_limit_pct` | Total harmonic distortion limits | IEEE 519-2014 (voltage THD by bus voltage class; current TDD by short-circuit ratio) |
| `flicker_pst_limit` | Short-term flicker severity, only for customers sensitive to voltage fluctuation (data centers with UPS transfer sensitivity) | IEC 61000-4-15 / IEEE 1453 ($P_{st}$) |

**Defaults by customer class** (used when a contract does not override): `HOME` → `SPLIT_PHASE`, imbalance n/a
(single customer), voltage ±5% (ANSI C84.1 Range A), Category III ride-through, freq ±0.5 Hz / no ROCOF limit, PF
0.90, THD_V 5% / THD_I 5% (IEEE 519 default for the smallest short-circuit-ratio bucket), no flicker limit.
`DATA_CENTER`/`LARGE_LOAD` and `PIPELINE_AC` set tighter values per §4.

---

## 3. Inverter model per hub/unit, and how imperfections aggregate

**Status of this section after the 2026-09-25 amendment.** The per-inverter parameters and aggregation formulas below
were originally specified as the delivery-time source of truth for harmonic/offset behaviour. The owner has since
directed that real per-inverter **waveform telemetry** is streamed over the SCADA network and analyzed by the
orchestrator (§6.4–§6.6); that measured pipeline is now the delivery-time source of truth for THD, imbalance and f/V
deviation, and for the guardian's G-21..G-23 checks. This section's parameters (`og.hub_inverter_pq`) and formulas
remain in the design for two purposes that are still model-based by nature: (1) **pre-delivery planning** — sizing an
admission decision or a day-ahead/intraday plan before any waveform has been measured for the specific dispatch under
consideration, and (2) a **forecast cross-check** against the measured pipeline, so a large disagreement between
modelled and measured aggregation is itself a signal (of a mis-characterized inverter, a stale characterization, or a
sensor fault) rather than being silently overwritten. `og.hub_inverter_pq.last_estimated_at` (§1.5) already exists to
distinguish "characterized from real waveform data" from "default/assumed."

### 3.1 Per-inverter model

Every hub inverter (`og.hub_inverter_pq`, §1.5) is modeled with:

- **Phase connection**: which leg(s) it is wired to. A 1-unit `HOME` hub is single-phase (`A`, `B`, or `C` on a
  3-phase secondary, or one leg of a split-phase service); a 2-unit home's two 11 kW units may be on the same leg or
  split across legs — recorded per hub, not assumed.
- **P/Q capability**: a kVA circle of radius `kva_rating` with lagging/leading PF limits (`pf_min_leading/lagging`),
  per IEEE 1547-2018 Category B reactive capability (a real inverter cannot deliver rated kW and rated kVAR
  simultaneously; the allocator must not request a point outside the circle — this is a new admission check, §5).
- **Frequency accuracy/offset**: real inverters do not track grid frequency losslessly; `freq_offset_hz` (mean bias,
  from PLL/clock error) and `freq_offset_std_hz` (unit-to-unit and thermal variation) characterize it.
- **Amplitude/voltage offset**: `voltage_offset_pct`/`voltage_offset_std_pct`, from output-voltage regulation
  tolerance and line-drop compensation error.
- **Harmonic spectrum/THD**: `thd_current_pct` (aggregate) plus `dominant_harmonics` (per-order magnitude and phase
  angle, typically dominated by odd, non-triplen orders — 3rd, 5th, 7th — from PWM switching).
- **Phase-angle error**: `phase_angle_error_deg`, the steady-state error between commanded and actual output
  current phase relative to the local voltage — affects both delivered PF and cross-inverter harmonic cancellation
  (below).
- **Response time**: `response_time_ms`, matched against a `ServiceProfile.response_time_s` requirement at admission.
- **Ride-through settings**: `ride_through_class`, the IEEE 1547-2018 category the unit is configured to.

### 3.2 Aggregation across N inverters on a bank/phase

The fleet is DC batteries behind independent inverters; what the grid (and the customer) sees is the *sum* of N
imperfect AC sources. Four aggregation effects matter, each with a formula the allocator and guardian use:

**(a) Random offset averaging (frequency/voltage).** If each inverter's offset is drawn from
$\mathcal N(\mu,\sigma^2)$ independently (reasonable for frequency/voltage bias — different units, different thermal
states, no common driver), the *power-weighted mean* offset of $N$ units of similar output is

$$\bar\delta = \mu,\qquad \sigma_{\bar\delta} = \frac{\sigma}{\sqrt N}$$

i.e. the aggregate bias does not cancel (a systematic PLL bias $\mu$ persists), but unit-to-unit *variance* shrinks
as $1/\sqrt N$. A bank of 50 single-phase hubs with $\sigma=0.01$ Hz sees the aggregate frequency-tracking error's
random component fall to about 0.0014 Hz, while the systematic $\mu$ (e.g. an inverter firmware family's shared bias)
does not shrink with fleet size — this is why `freq_offset_hz` is tracked separately from `freq_offset_std_hz`, and
why a single firmware defect can violate K14 fleet-wide even at large N.

**(b) Harmonic vector summation with phase diversity.** Each inverter's $k$-th harmonic current is a phasor
$I_{k,i}\angle\theta_{k,i}$. The bank's $k$-th harmonic is the vector sum, not the scalar sum:

$$I_{k,\text{bank}} = \left\lvert \sum_{i=1}^N I_{k,i}\,e^{j\theta_{k,i}} \right\rvert$$

- **Stacking** (worst case): all $N$ units share the same PWM carrier phase and switching-frequency lock (e.g.
  synchronized to the same grid zero-crossing with identical firmware) → $\theta_{k,i}$ identical →
  $I_{k,\text{bank}} = N\,I_{k,i}$ (linear growth; THD does not fall with fleet size).
- **Cancellation** (random phase, independent switching): if $\theta_{k,i}$ are i.i.d. uniform, the vector sum's
  magnitude grows as $\sqrt N\,I_{k,i}$ (a random walk in the complex plane), so the *harmonic current relative to
  total fundamental current* falls as $1/\sqrt N$ — the aggregate THD improves with fleet diversity.
- **Design consequence**: the allocator's diversity objective (§5) explicitly prefers assigning inverters with
  *different* `dominant_harmonics` phase angles to the same bank/phase when a sensitive customer's envelope is active,
  because it is the only lever that turns stacking into the $1/\sqrt N$ regime. Firmware-homogeneous hubs (same
  vendor batch, same phase-lock reference) are the worst case and are flagged for exclusion or spreading across
  banks (never concentrated behind one sensitive customer) per §5.

**(c) Per-phase imbalance from a single-phase home mix.** With $N_A, N_B, N_C$ single-phase hubs delivering mean
power $\bar P_A,\bar P_B,\bar P_C$ on each leg of a three-phase bank, the NEMA/IEEE-style current imbalance is

$$\text{Imb}\% = 100\times\frac{\max_\phi\lvert I_\phi-\bar I\rvert}{\bar I},\qquad \bar I=\frac{I_A+I_B+I_C}{3}$$

Because homes are assigned to phases largely by construction (fixed service-drop wiring, not dispatch-time choice),
the allocator's only real-time lever is *which already-connected hubs to dispatch and how much*, not which phase a
home is on. The bank PI controller of `03 §8.6.1` already regulates per-phase current when the utility rates by
phase; §5 extends its request generation so that, when a bank serves a phase-imbalance-sensitive customer, the
per-phase dispatch target is chosen to minimize $\text{Imb}\%$ subject to each obligation's committed kW (K13 unchanged
— rebalancing may move *which hub* delivers a phase's kW, never *whether* a committed obligation's kW is delivered).

**(d) Aggregate P/Q envelope.** The bank's deliverable kVA circle is the Minkowski sum of member circles, clipped by
each unit's individual PF limit and by the K9 one-loop-per-quantity rule (§8.6.1); at fleet scale this is
well-approximated by scaling the single-unit circle by $\sum_i \text{kva\_rating}_i$ with an effective PF limit equal
to the *tightest* member's, when members are dispatched proportionally — a conservative bound used at admission
(§5) rather than an exact LP evaluation on every cycle.

---

## 4. The priority profiles, fully specified

### 4.a `PIPELINE_AC` mitigation

**Control law.** Extends `03 §8.6.6` unchanged in its ramp-limit filter; this section adds the PQ-aware layer.
Measured line current $I_k$ from utility SCADA/ICCP (class A1/A2 per `03 §4.2`). Ramp-limited target:

$$\tilde I_k=\tilde I_{k-1}+\operatorname{clip}\!\big(I_k-\tilde I_{k-1},\,-r_A\Delta t_c/60,\,+r_A\Delta t_c/60\big)$$

Requested fleet action, via the shift factor $SF$ (corridor injection's share of the monitored line):

$$r_k=\operatorname{clip}\!\Big(s_{dir}\,\frac{\sqrt3\,V_{kV}\,(I_k-\tilde I_k)\,\mathrm{PF}}{SF},\,-B,\,+B\Big)\ \text{kW}$$

**Own-harmonic respect (measured, amended 2026-09-25).** The fleet's own harmonic injection contributes to the
*measured* line current the loop is trying to smooth — a bank stacking harmonics (§3.2b) can itself raise $I_k$ at
the harmonic order nearest a resonance, defeating the mitigation. The controller evaluates the fleet's current THD
contribution at dispatch time, $\text{THD}_I^{fleet}(r_k)$, from the **measured, waveform-derived per-hub harmonic
summaries** of the assigned inverters (§6.5's per-bank/phase aggregation, vector-summed exactly per the §3.2b formula
but on measured phasors rather than characterized defaults), and rejects a candidate assignment whose
$\text{THD}_I^{fleet}$ would push the corridor's current THD above the pipeline customer's `thd_current_limit_pct`
(typically tight — pipeline coating and cathodic-protection interference studies commonly hold current distortion to
IEEE 519's smallest-SCR bucket or tighter by contract). Where a candidate hub's waveform summary is stale or missing
(freshness gate, same pattern as K1/G-01), the modelled §3.2b estimate from `og.hub_inverter_pq` is used
conservatively as a fallback, and the assignment is flagged `PQ_ESTIMATE_FALLBACK` in the trace. This is an
**admission-time and per-cycle feasibility check**, not a new control loop (K9 preserved: the line-current PI loop
remains the only integrator; harmonic feasibility is a constraint on which inverters may be selected, evaluated by
the allocator's assignment step, §5).

**Inputs/outputs.** Input: line current (A), shift factor $SF$ (from TO/ERCOT model), pipeline customer's PQ
envelope (current limit on the monitored line, THD_I limit). Output: fleet kW request, clipped to band $B$; achieved
$\Delta I = SF\times\text{delivered kW}/(\sqrt3\,V\,\mathrm{PF})$, reported to the customer.

**Tuning.** $r_A$ (ramp limit) 30 A/min default (`03` prototype value); $B$ per contract. No new PI gains — the
existing ramp-limit filter is retained; the harmonic-feasibility check is a hard constraint, not a tuned loop, so it
adds no pole to the existing stability analysis of `03 §8.6.1`/`§8.6.6`.

**Stability notes.** Unchanged from `03 §8.6.6`: a ramp-limit filter has no integrator and cannot hunt; the new
harmonic constraint can only *remove* candidate inverters from the assignment (never add gain), so it cannot
destabilize the loop — worst case it reduces available $B$ when diverse-phase inverters are scarce, which is reported
as a capability reduction, not a fault.

**PQ constraints.** `current_limit_scope = SPECIFIC_LINE` (the monitored conductor), `thd_current_limit_pct` tight;
`voltage_band_pct`/`freq_tolerance_hz` at grid-code minimum (pipeline AC mitigation does not itself buy voltage/
frequency quality — see (c) ERCOT_ENERGY for the "grid-code minimum" baseline).

**M&V/settlement.** Unchanged from `03 §10.3`: delivered kW (hub meters) + achieved $\Delta I$ + line-current ramp
statistics; `settlement_metric = achieved_line_current_delta_a`; `FIXED_FEE` billing (pilot). New: the per-cycle
$\text{THD}_I^{fleet}$ estimate is logged to the trace and reported to the customer alongside RMU readings, so a
future contract can price the harmonic-quality term explicitly.

**Failure behaviour.** Unchanged: line current unusable → `NEUTRAL_ON_SIGNAL_LOSS` (band to 0 kW); new — if the only
available inverters would breach `thd_current_limit_pct`, the controller degrades to the *lowest-THD feasible subset*
at reduced $B$ (never assigns a breaching set), logs `PQ_CAPABILITY_REDUCED`, and notifies the customer; this is the
same shape as an ordinary capability reduction (`03 §7.5`), not a new failure class.

### 4.b `DATA_CENTER` (new profile; supersedes ad-hoc treatment inside `LARGE_LOAD`)

`DATA_CENTER` is a new profile distinct from `LARGE_LOAD`: `LARGE_LOAD` (per `03 §2.6`) is a load-offset *event*
profile with no PQ obligation beyond grid-code minimum; `DATA_CENTER` additionally sells **firm bridging capacity
inside a tight PQ envelope** — the customer cares about ride-through and clean power during the bridge, not only
delivered kW.

**Control law.** `control_primitive = CLOSED_LOOP_REGULATION`, `target_quantity = KW`, `target_scope = SITE_METER`.
Reuses the `03 §8.6.3` event-average tracker for the coarse kW target, plus a **fast inner loop** absent from
`LARGE_LOAD`: on a declared bridging event, hubs in scope ramp to committed kW within `response_time_s` (target ≤
2 s, tighter than `LARGE_LOAD`'s per-contract default) and then hold with deadband tracking against the site meter,
$e_k = P^{target} - P^{meter}_k$, $u_k = u_{k-1} + K_p e_k$ (proportional only — no integrator, per K9: the site's
own UPS/ATS transfer logic is the integrating authority during a bridge; the fleet is feed-forward to it, matching
the "one loop per quantity" pattern of `03 §8.6.1`'s add-back design).

**Inverter selection by PQ quality (new, this is the profile's defining feature).** At admission and at every
re-plan, the allocator restricts the eligible hub set to those whose `og.hub_inverter_pq.quality_score` (§5.1) meets
the contract's minimum, and whose individual `voltage_offset_pct`, `thd_current_pct` and `phase_angle_error_deg` are
each below thresholds derived from the customer's envelope divided by $\sqrt N$ of the *committed* fleet size (so
that the aggregate, not just one unit, clears the envelope — see §3.2a and §5.2).

**Phase balance.** `phase_config = 3P` is required; the allocator applies the per-phase minimization of §3.2c at
every cycle, weighted more heavily than for other profiles (this contract's `max_phase_imbalance_pct` is typically
1–2%, tighter than `HOME`'s implicit tolerance).

**Ride-through.** `ride_through_class = CATEGORY_III` by default for firm bridging contracts (must not trip during
the sag/swell that likely caused the bridging event in the first place); inverters below Category III are excluded
from this profile's eligible set regardless of other quality.

**Tuning.** $K_p$ sized so the fast loop settles within `response_time_s` given the fleet's measured
`response_time_ms` (§3.1) plus telemetry latency; no integrator to tune (K9). Coarse event-average tracker gains
unchanged from `03 §8.6.3`.

**Stability notes.** A pure-proportional fast loop with no integrator cannot wind up; its only stability risk is
interaction with the site's own transfer-switch control, which is why the profile is feed-forward-only during a
bridge (the site, not the fleet, is the authority during transfer) — consistent with K9's "others treat it as
feed-forward" language.

**PQ constraints.** Full envelope enforced (§2): tight voltage band (±2–3% typical), Category III ride-through, PF
≥ 0.95 typical, THD_V/THD_I at IEEE 519's tighter bus-voltage-class bands, phase imbalance ≤ 1–2%.

**M&V/settlement.** `mv_method = DIRECT_HUB_METER` in scope; `settlement_metric = capacity_payment_x_pf` plus a new
`pq_compliance_pct` line (share of the bridging interval inside the full envelope, not just kW delivered) —
`CAPACITY_PAYMENT×PF` is retained from `03`'s billing library, with `PF` (performance factor) redefined for this
profile to include PQ compliance, not kW compliance alone: $PF = \min(PF_{kW}, PF_{PQ})$.

**Failure behaviour.** `HOLD_THEN_SCHEDULE` per `03` library; additionally, if the only inverters that can meet the
kW target would breach the PQ envelope, the engine holds the *smaller, PQ-compliant* delivery and reports a
capacity shortfall against the obligation (never a silent PQ breach) — this is the concrete instance of the new
G-2x guardian family (§5.3) refusing to sign a non-compliant batch even when it would otherwise close the kW gap.

### 4.c `ERCOT_ENERGY` arbitrage

**Control law.** Unchanged from `03 §8.6.5`: `control_primitive = PRICE_RESPONSE`, threshold rule on water value
$\nu$ and territory value $v^E$, 5-min dwell, $5/MWh (or $0.005/kWh) hysteresis. No new control logic.

**PQ constraints — grid-code minimum only (by design).** This is the deliberate contrast case: `pq_envelope_id`
points at a **fleet-default envelope** identical to `HOME`'s (§2 defaults) — ANSI C84.1 Range A voltage, IEEE
1547-2018 Category III ride-through, IEEE 519 default THD bands, PF ≥ 0.90 — because the customer of this profile is
the wholesale market, which has no bespoke PQ ask beyond interconnection compliance. The only difference from `HOME`
is that arbitrage dispatch is explicitly excluded from the PQ-aware inverter-selection preference of §5 (it may use
*any* grid-code-compliant inverter, including ones a sensitive customer's contract would exclude) — this keeps
arbitrage from competing with `DATA_CENTER`/`PIPELINE_AC` for the fleet's highest-quality units when it does not need
them, which is itself a diversity benefit (§3.2b): concentrating the highest-`quality_score` inverters on sensitive
contracts and leaving the rest for arbitrage is a direct allocator objective (§5.2).

**M&V/settlement.** Unchanged from `03 §10.3`/`§10.5`: `ISO_SETTLEMENT_SHADOW`, `settlement_metric = energy_x_price`.

### 4.d Mapping the existing profiles onto the model

| Existing profile | `control_primitive` | `target_quantity` | `pq_envelope` |
|---|---|---|---|
| `HOME` | `OPEN_LOOP_SCHEDULE` (reserve floor updates, not a call) | `NONE` | Fleet default (§2) |
| `ERCOT_AS` | `CAPACITY_HOLD` (ring-fence) → `EVENT_SCHEDULE_TRACKING` on deployment | `KW` (ALR: implicit via net power; NCLR: `KW`) | Fleet default |
| `DIST_DEFERRAL` | `CLOSED_LOOP_REGULATION` | `BANK_KVA` (or per-phase A) | Utility interconnection minimum, tightened only if the utility's contract specifies |
| `PARTNER_CAPACITY` (`EVENT`) | `EVENT_SCHEDULE_TRACKING` | `KW` | Per-program contract; typically fleet default |
| `PARTNER_CAPACITY` (`TOLLING`) | `OPEN_LOOP_SCHEDULE` (follows utility schedule) | `KW` | Per-utility contract |

---

## 5. PQ-aware dispatch

### 5.1 Inverter quality score

A scalar summary used for fast filtering (not a substitute for the constraint checks below):

$$\text{quality\_score} = 1 - w_1\frac{\lvert\text{freq\_offset\_hz}\rvert}{\Delta f_{ref}} - w_2\frac{\text{voltage\_offset\_pct}}{\Delta V_{ref}} - w_3\frac{\text{thd\_current\_pct}}{\text{THD}_{ref}} - w_4\frac{\lvert\text{phase\_angle\_error\_deg}\rvert}{\theta_{ref}}$$

clipped to $[0,1]$, with reference scales ($\Delta f_{ref}=0.05$ Hz, $\Delta V_{ref}=2\%$, $\text{THD}_{ref}=5\%$,
$\theta_{ref}=10°$) and weights $w_i$ summing to 1 (default equal weight 0.25 each — a tuning parameter, not a
physical constant). Recomputed by a periodic estimation job (§7) from telemetry, not by the allocator per cycle.

### 5.2 Assignment as an LP/assignment extension of the existing allocator

`03 §5`/`06-first-principles-review §5` already frame real-time dispatch as lexicographic LP + water-filling
(`allocator/waterfill.py::water_fill`, `allocator/substitution.py::realize_obligation` in the current code). This
document adds **no new solver** — it adds a filtering/weighting stage ahead of the existing water-fill:

1. **Eligibility filter** (hard). For a `ServiceProfile` with `pq_envelope_id` ≠ fleet default, the eligible hub set
   $\mathcal H_o$ passed into `realize_obligation` is pre-filtered to hubs whose individual PQ characterization
   (`og.hub_inverter_pq`) cannot, even in the worst case, push the *aggregate* envelope over limit: exclude hubs with
   `thd_current_pct` or `voltage_offset_pct` more than $k\sigma$ above the bank's mean (default $k=2$) when the
   customer's envelope is tighter than the fleet default, and exclude hubs whose kVA-circle PF at the requested
   dispatch point is infeasible (§3.1). This is exactly the same shape as the existing `TOPOLOGY`/`FEASIBILITY`
   admission checks of `03 §2.5` element 3 — a new admission predicate, not a new pipeline stage.
2. **Diversity weighting** (soft, inside water-filling). `hub_weight(hub, stickiness)` in `waterfill.py` already
   ranks candidates; this document adds a diversity term so that, among eligible hubs, the water-fill prefers a set
   whose `dominant_harmonics` phase angles are spread rather than clustered (minimizing the vector-sum THD of §3.2b)
   and whose phase assignment minimizes $\text{Imb}\%$ (§3.2c) subject to each obligation's committed kW (K13
   unchanged: this reweights *which eligible hub* serves a cycle's grant, never *how much* a committed obligation
   receives). Implementation note for the build phase: this is a rank-order tweak to the existing weight function,
   not a new optimization variable.
3. **Post-assignment verification.** After water-fill produces a candidate grant set, the allocator computes the
   predicted aggregate imbalance/THD/V/f deviation (§3.2 formulas) for the batch *before* it reaches the guardian —
   this is the allocator's own self-check; the guardian (§5.3) re-derives the same figures independently as the K2
   pattern requires (primary check at the allocator, independent check at the guardian).

### 5.3 Invariant and guardian additions

- **K14 (already recorded as proposed in `00-invariants.md`).** Restated here as the canonical definition this
  document owns: *dispatch serving a customer must keep the aggregated per-phase imbalance, voltage/frequency
  deviation and estimated THD within that customer's `PowerQualityEnvelope`.* Enforced at: allocator (§5.2 step 3,
  primary) → guardian G-21…G-24 (independent, below) → hub firmware ride-through settings (fail-safe, unchanged
  hardware behavior). Fail-safe: VETO the batch and re-solve with the offending hubs excluded (same VETO/re-solve
  shape as K4's G-02..G-06), never a silent envelope breach. Proven by: property test over random fleet composition
  and random envelope tightness (§8).
- **G-21 — per-phase imbalance.** Given the batch's proposed per-hub setpoints and each hub's `phase_connection`,
  compute $\text{Imb}\%$ (§3.2c) per affected bank/feeder and compare to the tightest active `max_phase_imbalance_pct`
  among obligations served behind it; VETO the batch (item-level, like G-01/G-02/G-04) if exceeded. Reuses
  `core.limits`-style pure functions — no duplication of the SCADA-reading or bank-snapshot code already used by
  G-03 (`check_g03_bank_kva`); G-21 calls the same `BankSnapshot`/per-phase current fields, adding only the
  imbalance formula.
- **G-22 — THD estimate.** Given the batch and each assigned hub's `dominant_harmonics`, compute the vector-summed
  THD estimate (§3.2b) at the target scope and compare to the tightest active `thd_current_limit_pct`/
  `thd_voltage_limit_pct`; VETO if exceeded. This is an *estimate* check (no real-time waveform capture exists in
  MVP-S+ — see §7); it is intentionally conservative (assumes worst-case phase alignment when `dominant_harmonics`
  phase data is stale or missing, per the existing "stale → exclude" pattern of K1/G-01).
- **G-23 — frequency/voltage deviation.** Given the batch and each hub's `freq_offset_hz`/`voltage_offset_pct`,
  compute the aggregate offset (§3.2a) and compare to the tightest active `freq_tolerance_hz`/`voltage_band_pct`;
  VETO if exceeded. Runs alongside the existing frequency-deadband freeze of `03 §8.6` (R26) — G-23 checks the
  *inverter-fleet's own contribution*, R26's freeze handles *grid* frequency events; they are complementary, not
  duplicative.
- **G-24 — ride-through class conformance.** Refuse to admit (at profile-authoring/admission time, not per cycle) a
  hub whose `ride_through_class` is below the contract's `pq_envelope.ride_through_class` into that contract's
  eligible set. This is an admission-time check (like `TOPOLOGY`/`FEASIBILITY`), listed here for numbering
  completeness even though it fires earlier in the pipeline than G-21..G-23.

Guardian numbering note: per the current `checks.py` registry (G-01…G-06, G-09, G-13…G-15, G-19, G-20 in use), G-21
is the first free number after the already-reserved K14 note in `00-invariants.md`; G-07/08/10-12/16-18 remain
reserved gaps from earlier design work and are not reused here.

### 5.4 Continuous PQ monitoring on committed obligations (owner amendment 2026-09-25)

§5.2's eligibility filter only governs *selection* — which hubs a new grant may draw from. It says nothing about a
delivery already in progress. The owner has directed that PQ is monitored **continuously** on committed obligations
too, and corrected in place when it drifts, without ever touching K13 (a correction may change *which hub* delivers,
never *whether* the obligation's committed kW is delivered).

**Cadence.** Every allocator cycle for any obligation whose `ServiceProfile.pq_envelope_id` is not the fleet default
— the same 2–10 s cadence as `03 §3.1`'s L-RT layer, using the latest measured PQ summary per hub (§6.5). A summary
older than the freshness gate (2× the profile's telemetry cadence, matching the existing stale-data pattern of K1)
is treated as missing, not as compliant.

**Per-customer baseline.** In addition to the static `og.pq_envelope` limits, a rolling baseline is kept per contract:
the trailing 5-minute median (configurable) of each monitored quantity (imbalance %, THD_V/I %, frequency and voltage
deviation, PF) computed from measured summaries. The baseline exists to catch *drift toward* a limit, not only
crossings of it — a customer's own dashboard (§6.6) shows both the static envelope and the live baseline.

**Deviation detection with hysteresis.** Two thresholds per monitored quantity, mirroring the dwell/hysteresis
pattern already used for price response (`03 §8.6.5`, 5-min dwell) and the `DIST_DEFERRAL` deadband (`03 §8.6.1`):

- **WARN**: sustained at or above 70% of the envelope limit (default, per-contract configurable) for a dwell window
  (default 60 s). Raises an alert; does not yet act.
- **BREACH**: sustained at or above 100% of the limit for the `ServiceProfile.deadband`/`accuracy_tolerance` window.
  Triggers the corrective-action ladder below.
- **Recovery**: the quantity must fall below a lower hysteresis threshold (default 60% of limit) for the same dwell
  window before the obligation is returned to nominal monitoring — this prevents chatter across the limit exactly as
  the existing $5/MWh price hysteresis prevents dwell-flapping.

**Corrective-action ladder** (each step attempted, in order, before escalating; every step is traced before act, K10,
and every resulting command batch still passes G-21..G-25 before the guardian signs it — the ladder generates
requests, it never bypasses the independent check):

1. **Rebalance phases** within the obligation's already-assigned hub set (§3.2c minimization) — no new hubs, no
   change to committed kW.
2. **Substitute** hubs *within the same obligation* (K13's `R-SUBSTITUTION` exception, unchanged authority): swap in
   an eligible spare hub for a drifting one, from the §5.2-eligible pool, preserving committed kW. This is a
   **software dispatch action** (§ terminology note, front matter item 7) — it never involves touching hardware.
3. **Remote recalibration** of a drifting inverter that is *not currently the sole/uncompensated source for a
   committed PQ-sensitive delivery* (§5.5's ladder and safety rule) — attempted only after step 2 has already moved
   any committed PQ-sensitive delivery off that hub, per §5.5.
4. **Adjust reactive/PF setpoints** on assigned inverters within their kVA-circle (§3.1) to correct a PF/voltage-band
   deviation before touching real-power delivery.
5. **Exclude** the drifting unit(s) from this obligation's active set entirely (updates `quality_score` history, §5.1,
   and may open a §5.5 asset-health work order) and re-run steps 1, 2 and 4 on the reduced set.
6. **Escalate to `AT_RISK`.** If no combination of 1–5 restores compliance within 3 cycles (default, per-contract
   configurable), mark the obligation `AT_RISK` (existing state, `03 §8.11`), notify the customer and operator, and
   record a shortfall against the **PQ compliance metric** specifically — distinct from the kW compliance metric —
   never a silent out-of-envelope delivery. Reason code `R-PQ-DRIFT-AT-RISK`.

**M&V and settlement of PQ compliance.** Extends `03 §10.2`'s interval arithmetic: for interval $j$,
$PQ_{o,j} = $ the share of interval $j$ during which every monitored dimension of the obligation's envelope was
satisfied, computed from measured summaries (§6.5), independent of whether kW compliance was also met.
`pq_compliance_pct` (introduced for `DATA_CENTER` in §4.b) generalizes to any profile with a non-default envelope.
Contract billing may combine it as $PF=\min(PF_{kW},PF_{PQ})$ (as specified for `DATA_CENTER`) or post a dedicated PQ
liquidated-damages line, per contract; either way `og.invoice_line` carries a `pq_compliance_pct` reference alongside
the existing M&V record IDs (`03 §10.6`).

### 5.5 Asset health and calibration workflow (owner addition 2026-09-25)

**Terminology (binding throughout this document, front matter item 7).** *Substitution* — §5.4 step 2 — is the
software action of moving delivery to different hubs, in seconds, under K13. *Inverter swap / replacement* is a
physical hardware action: a technician removes and replaces a unit's inverter. The two are never conflated in code,
traces or UI labels.

**1. Detecting calibration drift.** From the measured waveform summary pipeline (§6.5), four quantities are tracked
per hub against `og.hub_inverter_pq`'s characterized values: frequency offset, voltage/amplitude offset, THD, and
phase-angle error. A candidate drift is flagged only after:

- **Minimum observation window**: the deviation is present in at least 90% of summaries over a rolling 15-minute
  window (default), so a single noisy sample or a transient grid event (sag/swell, a frequency excursion the whole
  bank rides through together, R26) is never mistaken for a hardware drift.
- **Hysteresis**: the same WARN/BREACH/recovery shape as §5.4, applied to the *characterization* deviation rather
  than the envelope — a `WATCH`-worthy drift is one exceeding 1.5× the unit's own historical variance (or an absolute
  floor — 0.02 Hz, 1.0% voltage, 1.5% THD, 3° phase angle, defaults) sustained through the observation window.
- **Persistent vs. transient classification**: a transient (clears within the observation window, or correlates with
  a fleet-wide/bank-wide event already explained by a trace entry) is logged but does not change asset state; a
  persistent deviation (still present after the window, isolated to one hub, uncorrelated with a bank/feeder-wide
  event) advances the asset state machine below.

**2. Asset states** (`og.hub_inverter_pq.asset_state`):

```
OK → WATCH → DEGRADED/DERATED → QUARANTINED → AWAITING_REPLACEMENT → RECOMMISSIONING → OK
```

- **OK**: within characterization tolerance.
- **WATCH**: a persistent drift detected; no dispatch restriction yet; increased telemetry/summary sampling requested
  (§6.4's on-demand capture) to build evidence.
- **DEGRADED/DERATED**: drift confirmed past the BREACH threshold, or a remote recalibration attempt (below) did not
  fully correct it; dispatch-eligibility restrictions apply (§3 below).
- **QUARANTINED**: drift severe enough, or recalibration attempted and failed/rolled back (§5.5.4), that the unit is
  excluded from all grid-facing dispatch.
- **AWAITING_REPLACEMENT**: a maintenance work order is open and a hardware inverter replacement has been scheduled.
- **RECOMMISSIONING**: hardware has been replaced; the unit is under the post-replacement verification capture
  (§5.5.5) and not yet eligible for dispatch.
- Return to **OK** only after recommissioning verification passes.

**3. Service eligibility per state.**

| State | `DATA_CENTER` / `PIPELINE_AC` (PQ-sensitive) | `ERCOT_ENERGY` / arbitrage (grid-code minimum) | `HOME` (homeowner reserve) |
|---|---|---|---|
| OK | Eligible | Eligible | Always serves the homeowner |
| WATCH | Eligible, but deprioritized in §5.2's diversity weighting | Eligible | Unaffected |
| DEGRADED/DERATED | **Excluded immediately** | Eligible **only if** the unit still meets grid-code-minimum envelope (§4.c); re-verified every cycle from measured data, not assumed | Unaffected — the homeowner's own reserve and backup service is never gated on grid-facing asset state |
| QUARANTINED | Excluded | Excluded (no grid-facing dispatch at all) | Unaffected |
| AWAITING_REPLACEMENT / RECOMMISSIONING | Excluded | Excluded | Unaffected |

This table is enforced at the same admission/eligibility point as §5.2 step 1 (an additional hard filter, not a new
pipeline stage), and independently by guardian check G-24 (ride-through/state conformance, extended to read
`asset_state`).

**4. Remote recalibration — the ladder, ahead of any hardware action (owner addition).**

```
drift detected (WATCH) → remote recalibration attempt(s) → verify from new waveform data
    → corrected: back to OK (logged as a calibration_attempt, outcome CORRECTED)
    → not corrected, or drift recurs within N days (default 14): → DEGRADED
        → maintenance work order opened → hardware inverter replacement → RECOMMISSIONING → OK
```

- **Calibration reference.** A grid-synchronized reference derived from the feeder/bank voltage phasor (SCADA, or a
  PMU where available) and a precise time base (PTP/GPS-disciplined server clock — the same clock-quality discipline
  K12/G-20 already requires of the guardian). The reference carries: reference phase angle, reference frequency,
  nominal amplitude, and the target unit's currently measured offsets (frequency, voltage, phase-angle) from §6.5.
- **Correction.** The inverter applies bounded phase-angle / frequency / amplitude correction parameters against
  that reference. Bounds are configured per inverter model/firmware family (never unbounded) — see the guardian check
  below.
- **Rate limiting.** At most one calibration attempt per hub per rolling 24 h (default, configurable), to avoid
  chasing noise or interacting badly with the unit's own control loop.
- **Verification.** A post-calibration waveform capture (§6.4's on-demand trigger) is compared against the
  pre-calibration characterization; outcome is recorded as `CORRECTED`, `IMPROVED` (partial, stays `WATCH`),
  `NO_CHANGE`, or `WORSE_ROLLED_BACK`.
- **Automatic rollback.** If the post-calibration waveform is *worse* than pre-calibration on any tracked dimension,
  the hub restores its own pre-command parameters locally, within the same apply; no second command is issued (amended
  2026-09-26, so G-25's one-per-24 h limit applies to every signed command without exemption). The attempt is marked
  `WORSE_ROLLED_BACK` and counts toward escalation just as a failed attempt does.
- **Never on a live PQ-sensitive delivery.** A calibration command is never sent to a hub that is currently the
  source (in whole or in part) of a committed `DATA_CENTER`/`PIPELINE_AC` (or any non-default-envelope) obligation,
  unless §5.4 step 2 (substitution) has already moved that obligation's delivery off the hub first. This is enforced
  by the new guardian check (§6.7) reading the ledger, not by convention.
- **Escalation.** A drift that a calibration attempt cannot correct, or that recurs within 14 days of a `CORRECTED`
  outcome, moves the unit to `DEGRADED` and opens a maintenance work order — recalibration is attempted at most once
  per drift episode before escalating, so the ladder cannot be used to indefinitely defer a real hardware fault.

**5. Maintenance work order** (new entity, `og.maintenance_work_order`, §8): hub, evidence (a `jsonb` bundle of the
waveform-summary IDs and `calibration_attempt` IDs that justified opening it), severity, opened/closed timestamps,
status, technician notes. The hardware replacement itself is recorded as an **asset event**
(`og.asset_event`, event_type `INVERTER_REPLACED`) carrying the old and new inverter serial numbers and firmware
versions — never described as a "swap" of dispatch delivery.

**6. Recommissioning.** After a physical replacement, the unit enters `RECOMMISSIONING` and is excluded from all
dispatch (table above) until a verification waveform capture (§6.4) is analyzed and passes the same calibration
checks a new unit would need to pass at initial characterization (§3.1); only then does it return to `OK` and become
dispatch-eligible again, with `og.hub_inverter_pq.last_estimated_at` reset to the recommissioning timestamp.

**7. Trace, UI and API.** Every state transition, calibration attempt and asset event is a traced decision (K10/K11),
`decision_type` extended with `ASSET_STATE_TRANSITION` and `CALIBRATION_ATTEMPT` alongside `03`'s existing
`og.trace.decision_type` enum. UI and API surfaces are specified in §6.6 alongside the PQ dashboard (an asset-health
list, per-hub work-order and calibration history, and a two-step-confirm operator "calibrate" action distinct from
the automatic ladder).

---

## 6. Telemetry and interface changes

### 6.1 Hub telemetry — per-phase electrical quality (new fields, additive)

`interfaces/mqtt/telemetry.schema.json` gains an optional `pq` object (existing required fields unchanged, so
current consumers keep working):

```json
{
  "properties": {
    "pq": {
      "type": ["object", "null"],
      "additionalProperties": false,
      "properties": {
        "v_rms": { "type": "number", "description": "line-neutral or line-line RMS voltage, V" },
        "i_rms": { "type": "number", "description": "RMS current, A" },
        "q_kvar": { "type": "number" },
        "freq_hz": { "type": "number" },
        "pf": { "type": "number", "minimum": -1, "maximum": 1 },
        "thd_v_pct": { "type": "number", "minimum": 0 },
        "thd_i_pct": { "type": "number", "minimum": 0 },
        "phase": { "type": "string", "enum": ["A", "B", "C", "AB", "BC", "CA", "ABC"] }
      }
    }
  }
}
```

### 6.2 SCADA — per-phase bank/feeder signals

`interfaces/mqtt/scada_bank_signal.schema.json`'s `signal` enum gains
`VOLTAGE_A_PU | VOLTAGE_B_PU | VOLTAGE_C_PU | CURRENT_A_PHASE_A | CURRENT_A_PHASE_B | CURRENT_A_PHASE_C |
FREQUENCY_HZ | THD_V_PCT | THD_I_PCT` alongside the existing scalar signals (the schema's one-signal-per-message
shape is unchanged — a per-phase reading is three messages, matching the existing pattern where `APPARENT_POWER_KVA`
is already one message per bank).

### 6.3 Market/partner API — customer PQ specs

New endpoint (contract-authoring, not MQTT): `POST /api/v1/contracts/{contract_id}/pq-envelope` accepting the
`og.pq_envelope` fields of §2 as a JSON body; validated against a new `interfaces/contracts/pq_envelope.schema.json`
(same field set as the DDL, JSON Schema mirrors §1.4's pattern). A `GET` on the same path returns the active
envelope plus its `service_profile` linkage, for the partner-facing dashboard to show "what quality are we
contracted to deliver."

### 6.4 Waveform telemetry — payload, transport and bandwidth (owner amendment 2026-09-25)

**(a) Payload.** Two payload kinds, both per hub:

- **Edge-computed PQ summary** — published every telemetry period (the hub's existing telemetry cadence, `03 §3.1`
  L-RT-aligned, 2–10 s), extending the `pq` object introduced in §6.1 with the fields needed for measured aggregation
  (§3.2) and the monitoring loop (§5.4):
  - `v_rms`, `i_rms`, `q_kvar`, `freq_hz`, `pf`, `thd_v_pct`, `thd_i_pct`, `phase` — as already specified in §6.1.
  - `phase_angle_deg` — the fundamental's phase angle relative to the calibration reference (§5.5.4) or, absent an
    active calibration reference, relative to the hub's own last PTP/GPS-disciplined sync pulse.
  - `harmonics` — magnitude (`% of fundamental`) **and** angle (degrees, relative to the same sync reference) for
    orders 2–50, for both V and I: four parallel arrays of 49 numbers each. Carrying angle (not just magnitude) at
    summary cadence is what makes **measured** bank/phase-level vector summation (§3.2b, now on real data) possible
    without needing full raw waveforms on every cycle.
  - Encoding: scaled `int16` per value (0.01% resolution for magnitude, 0.1° for angle) inside a compact array, not
    verbose per-harmonic JSON objects — see the bandwidth math below.
  - `sync_source`: `"ptp" | "gps" | "ntp_disciplined"` and `sync_quality_ns` (estimated clock offset), so a summary
    computed against a poor sync reference is flagged rather than trusted for cross-hub phase comparison.
- **Raw waveform capture** — per-phase voltage and current samples, published only when triggered (below), not on
  every telemetry cycle:
  - **Sample rate**: 128 samples/cycle (7,680 samples/s at 60 Hz) — enough to resolve harmonics through order 50
    cleanly above Nyquist (50th harmonic = 3,000 Hz; 128 samples/cycle gives a 3,840 Hz one-sided bandwidth margin).
  - **Window**: 10 cycles (≈166.7 ms) — long enough for a stable FFT bin at the fundamental and to catch a sag/swell
    transition (IEEE 1159 events are typically defined on a half-cycle to several-cycle basis; 10 cycles safely
    brackets one).
  - **Channels**: 2 (V, I) for a `1P` hub, 4 (`V_A,V_B,I_A,I_B`) for `SPLIT_PHASE`, 6 (`V_A,V_B,V_C,I_A,I_B,I_C`) for
    `3P` — per the hub's `phase_connection` (§1.5/§3.1).
  - **Timestamp/sync**: the capture's start time is stamped against the same PTP/GPS-disciplined reference as the
    summary, at sub-millisecond precision, so waveforms from different hubs on the same bank can be time-aligned for
    a true (not estimated) vector sum.
  - **Sample resolution**: 16-bit signed integers (adequate post-calibration for a Class-A-style PQ measurement; the
    edge computes its own summary at higher internal precision before quantizing the raw export).
  - **Compression**: lossless (delta-coding + generic compression) on the near-periodic waveform; typical 2–4×
    reduction assumed for the bandwidth math below. Raw payload is binary (not base64-in-JSON, which would add ~33%
    overhead) with a small structured header (hub_id, phase_connection, sample_rate_hz, cycles, channel order,
    ts, sync_source) followed by the binary sample block.

**(b) Transport over the SCADA network.** New topics under the existing root (mirrors the naming convention in
`interfaces/mqtt/topics.md`):

```
<root>/scada/wave/<zone>/<bank_id>/<hub_id>/summary    QoS 0, periodic (telemetry cadence)
<root>/scada/wave/<zone>/<bank_id>/<hub_id>/raw        QoS 1, triggered only
<root>/scada/wave/<zone>/<bank_id>/<hub_id>/request    QoS 1, orchestrator → hub, on-demand capture trigger
```

**Bandwidth math** (illustrative, at $N$ = 2,000–10,000 hubs):

- *Summary channel.* Harmonics block: 4 arrays × 49 orders × 2 bytes (`int16`) = 392 bytes; other summary scalars
  (RMS×3 phases, freq, PF×3, THD×3×2, phase-angle×3 ≈ 20 numbers × 2 bytes) ≈ 40 bytes; header/overhead ≈ 60–100
  bytes → **≈ 500 bytes/hub/period** uncompressed JSON-ish encoding, or materially less with a compact binary/CBOR
  encoding (recommended over JSON for this topic specifically, given the fixed-shape numeric payload). At the fleet's
  telemetry cadence (illustrative 5 s) and $N=10{,}000$: $10{,}000 \times 500\text{ B} / 5\text{ s} = 1.0\text{ MB/s}
  \approx 8\text{ Mbps}$. **Design mitigation**: split the summary into a *fast* sub-block (RMS/freq/PF/aggregate
  THD, ~150 bytes) published every telemetry period, and a *harmonic-detail* sub-block (the order-2–50 arrays)
  published at a slower cadence (default 30 s) or on-change (only when aggregate THD moves by more than a
  configurable delta) — cutting steady-state bandwidth to roughly $10{,}000\times(150/5 + 350/30)\text{ B/s}
  \approx 0.42\text{ MB/s} \approx 3.4\text{ Mbps}$, a materially smaller and still generous control-network budget.
- *Raw channel.* Per capture: `channels × 1,280 samples × 2 bytes`, before compression — 5.1 KB (`1P`), 10.2 KB
  (`SPLIT_PHASE`), 15.4 KB (`3P`); after 2–4× compression, roughly 1.3–5 KB on the wire. Default trigger policy
  (below) caps concurrent captures at **1% of the fleet per minute** plus any hub currently `WATCH`/`AT_RISK` or under
  active dispute: at $N=10{,}000$ that is ≤100 hubs/min ≈ 1.7 hubs/s × ~5 KB ≈ **8.5 KB/s ≈ 68 kbps** — negligible
  next to the summary channel.
- *Trigger policy.* Raw capture is **on-demand/triggered**, not periodic-for-the-whole-fleet: triggered by (i) a
  guardian/allocator PQ deviation (§5.4 WARN/BREACH) on the specific hub, (ii) an orchestrator API request (§6.6),
  (iii) a low-rate rotating audit sample (default 1%/minute, round-robin by `hub_id` hash) so every hub is
  periodically spot-checked without sustaining full-fleet raw bandwidth, and (iv) a post-calibration verification
  capture (§5.5.4).

### 6.5 Orchestrator-side ingestion, storage and analysis

- **Ingestion.** A new service (parallel to the existing `feeds`/`ingest-writer` pattern) subscribes to
  `.../wave/+/+/+/summary` and `.../wave/+/+/+/raw`, validates against the schemas below, and writes:
  - Summaries → `og.pq_waveform_summary` (hub_id, ts, the fast/harmonic sub-blocks, `sync_source`/`sync_quality_ns`).
  - Raw captures → object storage (a blob reference, not a DB blob), indexed by `og.pq_waveform_raw_index`
    (hub_id, ts, trigger_reason, blob_ref, channels, sample_rate_hz, cycles).
- **Retention** (per the existing K11 "configurable per event class, pruning keeps checkpoints" pattern):
  - Raw captures: short-lived by default (30 days), except a capture that is evidence for an open work order,
    guardian VETO investigation, or customer dispute, which is retained per that record's own retention class.
  - Summaries: retained at the same horizon as other M&V records (`03 §10`), since `pq_compliance_pct` billing (§5.4)
    must be able to reproduce its inputs for the contract's audit window.
- **Analysis pipeline.**
  1. Edge devices compute their own FFT/THD/RMS/phase-angle (the summary *is* the edge computation); the
     orchestrator does not recompute a full FFT for every summary.
  2. A background audit job periodically (default hourly, configurable) pulls a raw capture for a random sample of
     hubs and independently recomputes THD/RMS/phase-angle from the raw samples, comparing against that hub's
     concurrent summary — a disagreement beyond tolerance flags the hub's edge computation itself as suspect
     (`PQ_EDGE_MISCALIBRATION_SUSPECTED`), feeding §5.5's drift detection as an additional evidence source.
  3. **Per-bank/phase aggregation from measured data**: exactly the §3.2 formulas (offset averaging, harmonic vector
     sum, phase imbalance), now evaluated on live summaries rather than static `og.hub_inverter_pq` characterization
     — this is the "measured" pipeline referenced throughout §4 and §5.
  4. Output feeds three consumers: the allocator's post-assignment verification (§5.2 step 3), the guardian's
     G-21..G-23 (§5.3, now checking measured aggregates with the modelled values as a stale-data fallback only), and
     the continuous monitoring loop (§5.4).

### 6.6 Query API and UI views

New read endpoints (all under `/api/v1`, additive):

- `GET /hubs/{hub_id}/waveform?at=<ts>` — most recent cached raw capture, or triggers a new on-demand capture
  (publishes to the `.../request` topic) if none is fresh enough and the hub is online.
- `GET /hubs/{hub_id}/spectrum?from=&to=` — harmonic-order time series (order, magnitude, angle) from summaries.
- `GET /hubs/{hub_id}/pq?from=&to=` — RMS/THD/frequency/PF/phase-angle time series from summaries.
- `GET /banks/{bank_id}/pq?phase=A|B|C&from=&to=` — measured, aggregated per-phase PQ (§6.5 step 3 output).
- `GET /contracts/{contract_id}/pq-compliance?from=&to=` — `pq_compliance_pct` history feeding settlement (§5.4).
- `POST /hubs/{hub_id}/waveform-capture` — operator/API-triggered on-demand capture (rate-limited per hub).

UI views (partner/operator dashboard, additive pages): an oscilloscope-style time-domain waveform plot (V/I per
phase); a spectrum bar chart (harmonic order vs. magnitude, with the customer's THD limit drawn as a reference line
per IEEE 519 style); a per-phase PQ panel showing each envelope dimension (imbalance, V, f, THD, PF) against its
limit with WARN/BREACH coloring (§5.4); a bank/feeder roll-up view; and a contract PQ-compliance timeline tied to the
settlement record.

### 6.7 Calibration command/ack interfaces and asset-health API (owner addition 2026-09-25)

**New MQTT topics and schemas**, following the existing signed-command pattern of `command_batch.schema.json`:

```
<root>/cmd/cal/<hub_id>     QoS 1, guardian-signed, calibration command
<root>/ack/cal/<hub_id>     QoS 1, calibration result/ack
```

`interfaces/mqtt/calibration_command.schema.json` (new):

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "CalibrationCommand",
  "type": "object",
  "additionalProperties": false,
  "required": ["calibration_id", "hub_id", "epoch", "seq", "issued_at", "expires_at",
               "reference", "correction", "bounds", "key_id", "signature"],
  "properties": {
    "calibration_id": { "type": "string", "format": "uuid" },
    "hub_id": { "type": "string", "minLength": 1 },
    "epoch": { "type": "integer", "minimum": 0 },
    "seq": { "type": "integer", "minimum": 0 },
    "issued_at": { "type": "string", "format": "date-time" },
    "expires_at": { "type": "string", "format": "date-time" },
    "reference": {
      "type": "object",
      "additionalProperties": false,
      "required": ["phase_deg", "freq_hz", "amplitude_v", "sync_source"],
      "properties": {
        "phase_deg": { "type": "number" },
        "freq_hz": { "type": "number" },
        "amplitude_v": { "type": "number" },
        "sync_source": { "type": "string", "enum": ["ptp", "gps", "ntp_disciplined"] }
      }
    },
    "correction": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "freq_hz": { "type": "number" },
        "voltage_pct": { "type": "number" },
        "phase_deg": { "type": "number" }
      }
    },
    "bounds": {
      "type": "object",
      "additionalProperties": false,
      "required": ["max_freq_hz", "max_voltage_pct", "max_phase_deg"],
      "properties": {
        "max_freq_hz": { "type": "number", "exclusiveMinimum": 0 },
        "max_voltage_pct": { "type": "number", "exclusiveMinimum": 0 },
        "max_phase_deg": { "type": "number", "exclusiveMinimum": 0 }
      }
    },
    "key_id": { "type": "string" },
    "signature": { "type": "string" }
  }
}
```

`interfaces/mqtt/calibration_ack.schema.json` (new): required `calibration_id, hub_id, applied (boolean), applied_at,
resulting_offsets {freq_hz, voltage_pct, phase_deg}, status` (`status` enum `APPLIED | REJECTED | EXPIRED`).

**Guardian check G-25 — calibration command safety.** Refuses to sign a calibration command unless: (i) the
correction magnitudes are within the command's own `bounds` *and* the inverter model/firmware family's configured
maximum (defense in depth); (ii) the per-hub rate limit (§5.5.4, default 1/24h) is not exceeded; (iii) the ledger
shows the target hub carries **no active committed grant for a non-default-envelope obligation** at issue time (i.e.
any PQ-sensitive delivery has already been substituted off the hub, §5.4 step 2, before calibration is attempted).
Fail-safe: refuse to sign (hold); the calibration ladder step is skipped and the drift proceeds to escalation on its
own timeline rather than being forced through. This follows the same "primary check (allocator/ladder) → independent
check (guardian)" shape as every other K-invariant in this document.

**Asset-health API** (additive, `/api/v1`): `GET /hubs/{hub_id}/asset-health` (current state, history, open work
order if any); `GET /hubs/{hub_id}/calibration-history`; `GET /maintenance/work-orders?status=`; `POST
/hubs/{hub_id}/calibrate` — the **operator-initiated** action, distinct from the automatic ladder, requiring a
two-step confirmation (a `POST` that returns a confirmation token plus a summary of what will be sent, then a second
`POST` with that token to actually issue the guardian-signed calibration command) so a manual calibration is never a
single accidental click.

---

## 7. Simulator changes (`ogsim`, independent — per memory, simulators are not the orchestrator)

### 7.1 Inverter imperfection model (normal variation)

In `ogsim.fleet.state.FleetState`, add per-hub fields (loaded from a new `inverter_pq` block in `fleet.yaml`, mirroring
the existing `phase_offset_s`-style per-hub array pattern): `phase_connection`, `freq_offset_hz` (drawn once per hub
at seed time from $\mathcal N(0,\sigma_f^2)$, default $\sigma_f=0.01$ Hz), `voltage_offset_pct`
($\mathcal N(0,\sigma_V^2)$, default $\sigma_V=0.5\%$), `thd_current_pct` (log-normal, default median 2%),
`dominant_harmonics` (fixed per-hub phase angle for the 3rd/5th/7th, drawn uniformly — this is what makes §3.2b's
cancellation/stacking distinction observable in the sim: a config flag `harmonic_phase_lock: bool` per fleet batch
switches between "diverse" (uniform random angles, the cancellation regime) and "locked" (identical angles, the
stacking regime) so both regimes are testable).

### 7.2 New anomaly types

**Fleet-side** (`ogsim.fleet.anomalies.FLEET_ANOMALY_TYPES`, additive):

| New type | Modifier | Effect |
|---|---|---|
| `frequency_drift` | `freq_offset_hz` ramps linearly to a target over the anomaly duration | Models a PLL fault or firmware regression |
| `amplitude_deviation` | `voltage_offset_pct` step or ramp | Models a voltage-regulation fault |
| `harmonic_injection` | `thd_current_pct` step increase + `dominant_harmonics` reweighted toward one order | Models a failing DC-link capacitor or PWM fault |
| `phase_imbalance_injection` (fleet-side companion to the existing SCADA-side `phase_imbalance`) | Shifts a subset of hubs' effective dispatched kW to bias one phase's aggregate | Models a real single-phase load/generation skew, not just a SCADA reporting artifact |

**SCADA-side** (`ogsim.scada.anomalies.SCADA_ANOMALY_TYPES`, additive):

| New type | Modifier | Effect |
|---|---|---|
| `pq_event_sag` | `VOLTAGE_x_PU` readings dip to a configured pu level for a configured duration, shaped per an IEEE 1159 sag category | Customer-site sag, tests ride-through and G-23 |
| `pq_event_swell` | Symmetric swell version | Customer-site swell |
| `pipeline_line_current_surge` | `CURRENT_A` (or the new per-phase current signals) steps or ramps sharply on the monitored corridor line | Tests `PIPELINE_AC`'s ramp-limit filter and band-clip under a real disturbance, independent of fleet action |

Both frameworks keep the existing `start(anomaly_id, anomaly_type, target_kind, target_ref, params, start,
duration_s)` / `.tick(now)` interface unchanged — these are new entries in the existing enum and modifier dataclass,
not a new injection mechanism.

### 7.3 Config

`integration-sims/config/fleet.yaml` gains (mirrored in `dev/config/fleet.dev.yaml` and read by the same
single-source-of-truth pattern `seed.py` already uses):

```yaml
inverter_pq:
  freq_offset_std_hz: 0.01
  voltage_offset_std_pct: 0.5
  thd_current_median_pct: 2.0
  thd_current_p95_pct: 4.5
  harmonic_phase_lock: false   # false = diverse (cancellation regime), true = locked (stacking regime)
  ride_through_class_default: CATEGORY_III
```

### 7.4 Waveform generation and publication (owner amendment 2026-09-25)

Each simulated hub inverter generates a realistic per-cycle waveform from its seeded characterization (§7.1):
$v(t) = V_{nom}(1+\text{voltage\_offset\_pct}) \sin(2\pi(f_{nom}+\text{freq\_offset\_hz})t + \phi_0) +
\sum_{k=2}^{50} A_k\sin(2\pi k(f_{nom}+\text{freq\_offset\_hz})t + \theta_k)$, with $A_k/A_1$ and $\theta_k$ drawn from
the hub's `dominant_harmonics` (§7.1) and $i(t)$ generated analogously with the hub's `phase_angle_error_deg` applied
relative to $v(t)$. `ogsim.fleet` computes, at the sample rate/window of §6.4(a):

- the **edge summary** every telemetry period (RMS, frequency, PF, THD, phase angle, harmonics 2–50 mag+angle) —
  published on `.../wave/.../summary`;
- a **raw waveform buffer** (rolling, always computed so a trigger has data ready) — published on `.../wave/.../raw`
  only when triggered, per the same policy as §6.4(b) (anomaly, on-demand request, rotating audit sample, or
  post-calibration verification).

This makes §3.2's aggregation formulas independently checkable in simulation: a test can compute the expected bank
vector-sum THD directly from the seeded per-hub harmonics and compare it against what the orchestrator's measured
pipeline (§6.5) derives from the *published* summaries — closing the loop between the modelled cross-check and the
measured pipeline (TS-envelope-compliance, §8, extended).

### 7.5 Calibration and asset-health simulation (owner addition 2026-09-25)

**New fleet anomaly types** (`ogsim.fleet.anomalies.FLEET_ANOMALY_TYPES`, additive to §7.2's list):

| New type | Behaviour |
|---|---|
| `calibration_drift_correctable` | `freq_offset_hz`/`voltage_offset_pct`/`phase_angle_error_deg` drift gradually toward a configured target; a subsequent `CalibrationCommand` (§6.7) fully or partially removes the injected drift, proportional to the command's `correction` fields — demonstrates the ladder's `CORRECTED`/`IMPROVED` outcomes |
| `calibration_drift_hardware` | Same drift symptom, but a `CalibrationCommand` has **no effect** on the underlying offset (simulates a failed component, e.g. a degraded DC-link capacitor) — every calibration attempt against this anomaly resolves `NO_CHANGE`, so the sim exercises the escalation path (§5.5.4) to `DEGRADED` → work order → `INVERTER_REPLACED` → `RECOMMISSIONING` deterministically |

**Control-plane actions** (additive to the existing scenario-control mechanism, `scenario_control.schema.json`'s
`type` enum): `CALIBRATION_DRIFT_CORRECTABLE`, `CALIBRATION_DRIFT_HARDWARE` (start the corresponding anomaly), and a
new **`REPLACE_INVERTER`** action — `{hub_id, new_serial, new_firmware}` — which resets the target hub's
`freq_offset_hz`/`voltage_offset_pct`/`phase_angle_error_deg`/`thd_current_pct`/`dominant_harmonics` to freshly-drawn
"new unit" values (as if seeded at `t=0`) and clears any active `calibration_drift_*` anomaly on that hub, so a test
can drive the full `QUARANTINED → AWAITING_REPLACEMENT → RECOMMISSIONING → OK` sequence end-to-end. `ogsim` also
accepts inbound `CalibrationCommand` messages on `.../cmd/cal/<hub_id>` and publishes `CalibrationAck` on
`.../ack/cal/<hub_id>`, applying the correction per the anomaly's correctable/hardware behaviour above.

---

## 8. Stories and tests

### 8.1 Stories

**ES11 — Pipeline operator buys AC-mitigation service with a harmonic cap.**
*Given* a `PIPELINE_AC` contract with `thd_current_limit_pct = 3` on the monitored corridor line,
*when* the allocator would otherwise assign a bank of firmware-identical (harmonic-phase-locked) inverters to meet
the requested band,
*then* the allocator excludes or spreads the locked-phase inverters per §3.2b/§5.2, the delivered band is reduced if
necessary rather than breaching THD_I, and the reduction is traced as `PQ_CAPABILITY_REDUCED`, never a silent
envelope violation.

**ES12 — Data center buys firm bridging capacity with a tight PQ envelope.**
*Given* a `DATA_CENTER` contract with `voltage_band_pct = 2`, `max_phase_imbalance_pct = 1.5`,
`ride_through_class = CATEGORY_III`,
*when* a bridging event is declared,
*then* only hubs meeting the envelope-derived per-unit thresholds (§5.2 step 1) are eligible, the fast inner loop
reaches committed kW within `response_time_s`, phase imbalance stays within limit throughout the bridge, and the
settlement's `pq_compliance_pct` reflects any interval where the envelope was not fully met.

**ES13 — Arbitrage dispatch is not PQ-gated beyond grid-code minimum.**
*Given* an `ERCOT_ENERGY` price-responsive obligation with the fleet-default `PowerQualityEnvelope`,
*when* the allocator selects hubs for a price-responsive discharge,
*then* hubs excluded from a concurrent `DATA_CENTER`/`PIPELINE_AC` contract on PQ grounds remain eligible for this
obligation (grid-code minimum only), and the diversity-weighting term of §5.2 step 2 does not reduce the arbitrage
obligation's delivered kW (K13 unaffected: PQ preference is a tie-break among otherwise-eligible hubs, never a
capacity reduction on a committed obligation).

### 8.2 Tests

| ID | Type | Covers |
|---|---|---|
| TS-11a | Property test | Random fleet of $N$ inverters with i.i.d. frequency/voltage offsets: aggregate std dev matches $\sigma/\sqrt N$ within tolerance (§3.2a) across $N\in\{5,50,500\}$ |
| TS-11b | Property test | Random per-hub harmonic phase angles: aggregate THD falls with $N$ in the diverse regime and stays flat (linear stacking) in the `harmonic_phase_lock: true` regime (§3.2b) |
| TS-11c | Property test | Random single-phase-home phase assignment: computed $\text{Imb}\%$ matches the direct three-phase current calculation (§3.2c) on 1000 random fleets |
| TS-12a | Fixture (ES11) | Locked-phase bank assigned to a `PIPELINE_AC` contract with a tight THD cap → allocator reduces band or reassigns, G-22 never sees a batch exceeding the cap reach the guardian's sign step |
| TS-12b | Fixture (ES12) | `DATA_CENTER` bridging event with a deliberately-injected `harmonic_injection`/`amplitude_deviation` anomaly on one candidate hub → that hub is excluded from the eligible set (§5.2 step 1), the remaining fleet still meets `response_time_s` |
| TS-12c | Negative test | A batch that would breach `max_phase_imbalance_pct` is rejected by G-21 even when it would otherwise satisfy the requested kW — batch is VETOed, not signed |
| TS-13a | Fixture (ES13) | Arbitrage dispatch draws from PQ-excluded-for-others hubs without any G-2x veto (fleet-default envelope only) |
| TS-13b | Regression | Existing K1–K13 property tests and guardian negative tests (per `00-invariants.md`) still pass unchanged with `og.service_profile`/`og.pq_envelope` present but unpopulated (backward compatibility of the additive schema) |
| TS-envelope-compliance | Property test | For 1000 random (fleet composition, envelope tightness, obligation mix) draws, the allocator's self-check (§5.2 step 3) and the guardian's G-21..G-23 independent recomputation never disagree on pass/fail (the "primary → independent check" pattern holds) |

### 8.3 Additional stories — continuous monitoring, waveform ingestion, asset health (owner amendments 2026-09-25)

**ES14 — A committed data-center delivery drifts and is corrected without an outage.**
*Given* a `DATA_CENTER` obligation `DELIVERING` inside its `PowerQualityEnvelope`,
*when* one assigned hub's measured phase-angle error drifts past the WARN threshold and then BREACH (§5.4),
*then* the ladder tries rebalance → substitution → (if eligible) remote recalibration → reactive/PF adjustment →
exclusion, in that order, the committed kW is never reduced (K13 unaffected), and if compliance is restored before
step 6 the obligation never reaches `AT_RISK`; every step is traced before act.

**ES15 — A hub's waveform is queried end-to-end.**
*Given* a hub publishing periodic PQ summaries and supporting on-demand raw capture,
*when* an operator calls `POST /hubs/{hub_id}/waveform-capture`,
*then* the orchestrator publishes a `.../wave/.../request`, the simulated hub responds on `.../wave/.../raw` within
the expected latency, the capture is stored per §6.5's retention policy, and `GET /hubs/{hub_id}/waveform` returns it.

**ES16 — Calibration drift is detected and remotely corrected.**
*Given* a hub under the `calibration_drift_correctable` anomaly (§7.5),
*when* the persistent-drift detector (§5.5.1) confirms the drift past the observation window,
*then* the asset moves `OK → WATCH`, a `CalibrationCommand` is issued (guardian-signed, G-25-checked), the
post-calibration waveform verifies `CORRECTED`, and the asset returns to `OK` — with no maintenance work order opened.

**ES17 — Uncorrectable drift escalates to a hardware replacement.**
*Given* a hub under the `calibration_drift_hardware` anomaly,
*when* a calibration attempt resolves `NO_CHANGE`,
*then* the asset moves to `DEGRADED`, is immediately excluded from `DATA_CENTER`/`PIPELINE_AC` eligibility while
remaining eligible for `ERCOT_ENERGY` if still within grid-code minimum, a maintenance work order opens with the
drift evidence attached, and — once `REPLACE_INVERTER` is issued on the control plane — the asset event records the
old/new serials and firmware, the unit enters `RECOMMISSIONING`, and only returns to `OK` after a passing
verification capture.

**ES18 — A calibration command is never sent to a hub mid-delivery on a sensitive contract.**
*Given* a hub currently the sole source of a committed `PIPELINE_AC` obligation and also flagged `WATCH`,
*when* the ladder reaches the remote-recalibration step,
*then* guardian check G-25 refuses to sign the calibration command until substitution (§5.4 step 2) has moved that
obligation's delivery to another hub; the drift proceeds toward escalation on its own timeline rather than blocking.

### 8.4 Additional tests

| ID | Type | Covers |
|---|---|---|
| TS-14a | Fixture (ES14) | Injected phase-angle drift on one `DATA_CENTER` hub → ladder order observed exactly as specified; committed kW unchanged throughout |
| TS-14b | Property test | Random drift severities and durations: the obligation reaches `AT_RISK` if and only if steps 1–5 all fail to restore compliance within the configured cycle budget |
| TS-15a | Fixture (ES15) | On-demand capture round-trip latency and stored-blob retrievability via the API |
| TS-15b | Property test | For random seeded per-hub harmonics, the orchestrator's measured aggregation (§6.5) from published summaries matches the simulator's ground-truth vector sum within quantization tolerance (closes the §7.4 loop) |
| TS-16a | Fixture (ES16) | `calibration_drift_correctable` → `CalibrationCommand` → verified `CORRECTED` → asset state returns to `OK`; `calibration_attempt` row recorded |
| TS-16b | Negative test | A calibration command exceeding configured bounds, or issued twice within the 24 h rate limit, is refused by G-25 (not signed) |
| TS-17a | Fixture (ES17) | `calibration_drift_hardware` → `NO_CHANGE` → `DEGRADED` → work order opened → `REPLACE_INVERTER` → `RECOMMISSIONING` → passing verification → `OK`; each transition traced with `ASSET_STATE_TRANSITION` |
| TS-17b | Negative test | A hub in `QUARANTINED`/`AWAITING_REPLACEMENT`/`RECOMMISSIONING` never appears in any obligation's eligible set (§5.5.3 table), for any profile except `HOME` |
| TS-18 | Negative test (ES18) | G-25 refuses a calibration command while the target hub carries an active committed grant for a non-default-envelope obligation; passes once substitution clears it |

### 8.5 DDL — asset health, calibration and maintenance (migration `0010_asset_health.sql`, additive only)

```sql
ALTER TABLE og.hub_inverter_pq
    ADD COLUMN asset_state text NOT NULL DEFAULT 'OK'
        CHECK (asset_state IN ('OK','WATCH','DEGRADED','QUARANTINED','AWAITING_REPLACEMENT','RECOMMISSIONING')),
    ADD COLUMN asset_state_since timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN consecutive_correctable_drifts int NOT NULL DEFAULT 0,
    ADD COLUMN last_recalibration_at timestamptz;

-- Measured waveform summaries (§6.5), one row per hub per telemetry period.
CREATE TABLE og.pq_waveform_summary (
    hub_id              text NOT NULL REFERENCES og.hub(hub_id),
    ts                  timestamptz NOT NULL,
    v_rms_a numeric(8,3), v_rms_b numeric(8,3), v_rms_c numeric(8,3),
    i_rms_a numeric(8,3), i_rms_b numeric(8,3), i_rms_c numeric(8,3),
    freq_hz             numeric(7,4),
    pf_a numeric(4,3), pf_b numeric(4,3), pf_c numeric(4,3),
    thd_v_pct_a numeric(5,2), thd_v_pct_b numeric(5,2), thd_v_pct_c numeric(5,2),
    thd_i_pct_a numeric(5,2), thd_i_pct_b numeric(5,2), thd_i_pct_c numeric(5,2),
    phase_angle_deg_a numeric(6,2), phase_angle_deg_b numeric(6,2), phase_angle_deg_c numeric(6,2),
    harmonics_v         jsonb,   -- {"2": {"mag_pct":..,"angle_deg":..}, ..., "50": {...}}
    harmonics_i         jsonb,
    sync_source         text CHECK (sync_source IN ('ptp','gps','ntp_disciplined')),
    sync_quality_ns     numeric(10,1),
    PRIMARY KEY (hub_id, ts)
);

-- Raw waveform captures are blob-stored; this indexes them.
CREATE TABLE og.pq_waveform_raw_index (
    capture_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hub_id              text NOT NULL REFERENCES og.hub(hub_id),
    ts                  timestamptz NOT NULL,
    trigger_reason      text NOT NULL CHECK (trigger_reason IN
        ('PQ_DEVIATION','API_REQUEST','ROTATING_AUDIT','CALIBRATION_VERIFICATION')),
    blob_ref            text NOT NULL,
    channels            int NOT NULL,
    sample_rate_hz      numeric(8,2) NOT NULL DEFAULT 7680,
    cycles              int NOT NULL DEFAULT 10,
    retain_until        timestamptz,   -- NULL = default 30-day TTL; set when evidentiary (K11-style retention class)
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE og.calibration_attempt (
    calibration_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hub_id                      text NOT NULL REFERENCES og.hub(hub_id),
    requested_at                timestamptz NOT NULL DEFAULT now(),
    reference_phase_deg         numeric(7,3) NOT NULL,
    reference_freq_hz           numeric(7,4) NOT NULL,
    reference_amplitude_v       numeric(8,2) NOT NULL,
    measured_offset_freq_hz     numeric(6,4),
    measured_offset_voltage_pct numeric(6,4),
    measured_offset_phase_deg   numeric(6,3),
    correction_freq_hz          numeric(6,4),
    correction_voltage_pct      numeric(6,4),
    correction_phase_deg        numeric(6,3),
    command_batch_id            uuid REFERENCES og.command_batch(command_batch_id),
    outcome                     text NOT NULL DEFAULT 'PENDING' CHECK (outcome IN
        ('PENDING','IMPROVED','CORRECTED','NO_CHANGE','WORSE_ROLLED_BACK','FAILED_NO_ACK')),
    verified_at                 timestamptz,
    created_at                  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_calibration_attempt_hub ON og.calibration_attempt(hub_id, requested_at DESC);

CREATE TABLE og.maintenance_work_order (
    work_order_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hub_id            text NOT NULL REFERENCES og.hub(hub_id),
    severity          text NOT NULL CHECK (severity IN ('LOW','MEDIUM','HIGH','URGENT')),
    evidence          jsonb NOT NULL,   -- waveform-summary IDs, calibration_attempt IDs, drift history
    status            text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','IN_PROGRESS','CLOSED','CANCELLED')),
    opened_at         timestamptz NOT NULL DEFAULT now(),
    closed_at         timestamptz,
    technician_notes  text,
    created_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE og.asset_event (
    asset_event_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hub_id               text NOT NULL REFERENCES og.hub(hub_id),
    work_order_id        uuid REFERENCES og.maintenance_work_order(work_order_id),
    event_type           text NOT NULL CHECK (event_type IN
        ('STATE_TRANSITION','INVERTER_REPLACED','RECOMMISSIONED')),
    from_state           text,
    to_state             text,
    old_inverter_serial  text,
    new_inverter_serial  text,
    old_firmware         text,
    new_firmware         text,
    reason_code          text,
    occurred_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_asset_event_hub ON og.asset_event(hub_id, occurred_at DESC);
```

`og.trace.decision_type` (existing CHECK enum, `03 §10`) gains `'ASSET_STATE_TRANSITION'` and `'CALIBRATION_ATTEMPT'`
— an additive change to the existing constraint, not a new table.

---

## 9. Build plan

### 9.1 Staged MVP-S+ increment

| Stage | Scope | Feasible now (fast) vs. later |
|---|---|---|
| 1 | `og.pq_envelope`, `og.service_profile`, `og.hub_inverter_pq` DDL (migration `0009`); JSON Schemas (§1.4, §6.3) | Fast — pure schema, no behavior change, additive |
| 2 | Telemetry/SCADA schema deltas (§6.1, §6.2); `ogsim` inverter imperfection model + config (§7.1, §7.3) | Fast — additive fields, sim-only until consumed |
| 3 | Aggregation formulas as pure functions in `opengrid.core` (mirroring `core.physics`/`core.limits` style): imbalance, harmonic vector-sum, offset-averaging (§3.2) | Fast — pure math, directly unit-testable (TS-11a/b/c) before any allocator wiring |
| 4 | Guardian G-21..G-24 (§5.3), following the existing `check_g<NN>` plain-function pattern in `checks.py`, wired into `GuardianService._run_checks` | Medium — depends on stage 3's pure functions; no new registry mechanism needed |
| 5 | Allocator eligibility filter + diversity weighting (§5.2 steps 1–2) as a pre-stage to `water_fill`/`realize_obligation` | Medium — the riskiest stage; must preserve K13 (never reduces a committed obligation's kW) — build behind a feature flag, conformance-tested against the existing substitution fixtures before enabling |
| 6 | `DATA_CENTER` profile end-to-end (§4.b) as a genuinely new dispatch profile through the existing `03 §2.7` activation gate (schema validation → static rules → simulation conformance → risk-tiered gate → Tier 2 approval) | Later — it is a new service type by configuration per FR-DE-131, so it inherits that full gate, not a shortcut |
| 7 | `PIPELINE_AC` harmonic-respecting extension (§4.a) on top of the existing profile (no gate — tightening an existing profile's admission check is a "tighten-only" change per `03 §2.7` versioning rule, golden-week gate only) | Later, but lighter-weight than stage 6 |
| 8 | ogsim new anomaly types (§7.2) and property/fixture tests (§8.2) | Parallel to 4–7 once stage 1–3 land, since anomalies only need the schema and pure functions to be meaningful |
| 9 | Waveform transport + ingestion (§6.4, §6.5): new MQTT topics/schemas, `pq_waveform_summary`/`pq_waveform_raw_index` DDL, the ingestion service, and `ogsim` waveform generation (§7.4) | Medium — the summary path (fast/harmonic split) can land independently of the raw/on-demand path; raw capture + object storage is the heavier half |
| 10 | Measured aggregation pipeline (§6.5 step 3) replacing the modelled fallback in the allocator/guardian read path; continuous PQ monitoring loop and corrective-action ladder steps 1, 2, 4, 5, 6 (§5.4, excluding remote recalibration) | Medium — depends on stage 9's summaries; reuses stage 3's pure functions on measured inputs instead of characterized defaults |
| 11 | Asset-health state machine, drift detection, work orders, asset events, recommissioning (§5.5.1–3, 5–7); DDL `0010` (§8.5) | Medium — depends on stage 9 (needs measured summaries to detect drift); independent of remote calibration (stage 12) |
| 12 | Remote calibration: `CalibrationCommand`/`CalibrationAck` schemas, guardian G-25, ladder step 3 wiring into §5.4, `ogsim` calibration handling and `REPLACE_INVERTER` control-plane action (§7.5) | Later — depends on stage 11 (asset states) and stage 9 (verification captures); the highest-safety-sensitivity stage after stage 5 |
| 13 | Query API and UI (§6.6, §6.7's asset-health API): waveform/spectrum/PQ endpoints, asset-health and work-order views, the two-step-confirm operator calibrate action | Later — a thin read/action layer over stages 9–12; can start against mocked data once those stages' schemas are fixed |

### 9.2 Ownership split for parallel agents

- **Agent A — schema/DDL/interfaces.** Stages 1, 2 (schema half). Touches `interfaces/mqtt/*.schema.json`,
  new `interfaces/contracts/*.schema.json`, `orchestrator/migrations/0009_service_profile.sql`. No dependency on
  other agents; unblocks everyone else.
- **Agent B — core PQ math.** Stage 3. Touches `opengrid.core` only (new pure-function module, e.g.
  `opengrid.core.pq`), consistent with the existing `core.physics`/`core.limits` split. Depends only on Agent A's
  DDL field names (for type/unit consistency), not on schema completion.
- **Agent C — guardian.** Stages 4, 12 (guardian half: G-25). Touches `guardian/checks.py`, `guardian/service.py`.
  Depends on Agent B's pure functions for stage 4 (imports them, does not reimplement), and on Agent I's asset-state
  ledger read path for stage 12 (G-25's "no active committed grant" check).
- **Agent D — allocator.** Stages 5, 10 (monitoring-loop half). Touches `allocator/waterfill.py`,
  `allocator/substitution.py`, new `allocator/pq_eligibility.py`, new `allocator/pq_monitor.py` (the §5.4 ladder,
  steps 1/2/4/5/6). Depends on Agent B; must be reviewed against the existing K13 property tests
  (`allocator/substitution.py` fixtures) before merging, since this is the stage most likely to regress commitment
  lock. Coordinates with Agent I on ladder step 3 (remote recalibration is Agent I/C's concern; the allocator only
  invokes it and reacts to the outcome).
- **Agent E — ogsim (core PQ).** Stages 2 (sim half), 8. Fully independent of Agents A–D (per memory: ogsim is
  independent), needs only the schema's field *names* from Agent A to stay wire-compatible.
- **Agent F — DATA_CENTER profile authoring + Saturday-plan test fixtures.** Stage 6–7, plus TS-12a/b/c, TS-13a/b.
  Depends on A–E all landing; naturally one of the later agents to start.
- **Agent G — waveform ingestion and transport.** Stage 9. Touches `interfaces/mqtt/calibration_*` is out of scope
  here (that's Agent C/I) but the new `.../wave/...` topics and schemas are this agent's (coordinate with Agent A on
  the schema files themselves); new orchestrator ingestion service (e.g. `opengrid.wave_ingest`); `og.pq_waveform_summary`
  / `og.pq_waveform_raw_index` DDL; the object-storage integration for raw captures. Depends on Agent A for hub-side
  schema shapes; unblocks Agents D (stage 10) and I (stage 11).
- **Agent H — ogsim waveform generation.** Stage 9 (sim half), §7.4. Extends Agent E's inverter model to emit real
  per-cycle waveforms and publish summaries/raw captures on the new topics. Depends on Agent G's schema, otherwise
  independent (ogsim remains a separate, independent element per the project's build-phase memory).
- **Agent I — asset health and calibration.** Stages 11, 12 (ladder/state-machine half). Touches a new
  `opengrid.assets` module (state machine, drift detection consuming Agent G's summaries), `og.hub_inverter_pq`
  additions, `og.calibration_attempt`/`og.maintenance_work_order`/`og.asset_event` DDL (migration `0010`, §8.5), and
  the `CalibrationCommand`/`CalibrationAck` schemas (coordinate with Agent A on schema-file placement). Depends on
  Agent G (needs measured summaries) and coordinates tightly with Agent C on G-25.
- **Agent J — API and UI.** Stage 13. Touches the new `/api/v1/hubs/.../waveform|spectrum|pq`,
  `/api/v1/banks/.../pq`, `/api/v1/contracts/.../pq-compliance`, `/api/v1/hubs/.../asset-health|calibration-history|calibrate`
  and `/api/v1/maintenance/work-orders` endpoints, plus the corresponding dashboard views. Depends on Agents G, I for
  real data but can build against fixture/mocked responses in parallel once their schemas are frozen; naturally the
  last agent to fully land.

### 9.3 Risks

- **Waveform-derived measurement is only as good as edge computation and time sync.** The measured pipeline (§6.5)
  trusts each hub's own edge FFT/THD computation; the background audit job (§6.5 step 2) exists specifically because
  a mis-calibrated edge computation would otherwise look like "measured truth." Sync quality (`sync_quality_ns`) must
  be surfaced everywhere a cross-hub phase comparison is made (§3.2b's vector sum on measured data is only valid if
  the phase references are actually aligned) — treat a stale or low-quality sync exactly like stale telemetry (K1's
  "stale → exclude" pattern), never as good data with a caveat attached after the fact.
- **Allocator eligibility filtering (stage 5) and the monitoring loop (stage 10) could silently erode K13** if either
  is implemented as a filter/ladder that also touches already-committed obligations' *quantity*. Mitigation: stage 5
  only filters the *candidate pool for new/uncommitted headroom*; stage 10's ladder may change *which hub* delivers
  (substitution, K13's `R-SUBSTITUTION`) but must never reduce a `COMMITTED`/`DELIVERING` obligation's committed kW
  except through the existing L0/L1/L2/infeasibility exceptions — exactly the K13 boundary already drawn in
  `06-first-principles-review.md` §3.2.
- **Remote calibration is the highest new safety surface in this document.** A calibration command changes an
  in-service inverter's output characteristics in real time. G-25 (bounds, rate limit, no-active-sensitive-delivery)
  is the independent check, but the *primary* discipline is in the ladder itself (§5.4 step 2 must complete —
  substitution off the hub — before step 3 is ever attempted) and in the automatic rollback (§5.5.4). Build and test
  stage 12 with the same rigor as a guardian change, not as an allocator convenience feature; TS-16b and TS-18 are
  the minimum bar, not the ceiling — add fuzzed-bounds and rapid-repeat-request negative tests before enabling in
  any environment with real inverters.
- **Firmware-homogeneous fleets are a real, not simulated, risk.** If Base's actual inverter fleet is a single
  vendor/firmware batch, §3.2b's "diverse phase → cancellation" benefit may not exist in the field even though the
  simulator can model both regimes; the `DATA_CENTER` profile's viability partly depends on measured
  `dominant_harmonics` diversity once real units are characterized (§7.1's `last_estimated_at` field exists so this
  is tracked as data, not assumed). The same homogeneity risk applies to calibration: if a whole batch shares one
  firmware defect, expect a wave of near-simultaneous `DEGRADED` transitions and work orders, not isolated ones —
  size the maintenance/work-order operational process for that possibility rather than for independent failures.
- **Bandwidth is a design choice, not a hard limit, and should be revisited against real network capacity.** §6.4's
  math (≈3–8 Mbps for periodic summaries at 10,000 hubs, depending on the fast/harmonic-detail split chosen) is
  illustrative against an assumed SCADA network capacity; confirm against Base's actual field network before fixing
  the harmonic-detail cadence and the rotating-audit-sample rate as defaults.
- **Schedule risk.** Stage 6 (a genuinely new profile) inherits the full `03 §2.7` activation gate including a
  full-ERCOT-year replay for anything that is not tighten-only; this is correctly *not* a fast-track item and should
  not be compressed to fit an arbitrary date — the existing gate exists precisely to prevent an under-tested new
  service type from reaching production (§6 finding #2 of the first-principles review, scope/schedule realism).
  Stages 9–13 (waveform ingestion through API/UI) are a comparable amount of new work and should be sequenced and
  resourced accordingly, not treated as an afterthought to stages 1–8.
- **Guardian check numbering collisions.** G-07/08/10-12/16-18 are unused but not confirmed permanently free; before
  Agent C claims G-21+ or G-25, re-confirm against `checks.py` at merge time (numbers could shift if another
  in-flight workstream claims them first).

---

*Approved by owner 2026-09-25 (with amendments, recorded in the front matter). Approval promotes K14 to canonical in
`00-invariants.md` (guardian checks G-21..G-24, and G-25 for calibration-command safety), and authorizes the
migration/schema/guardian/allocator/ingestion/asset-health/API work of §9 to proceed per the ownership split above.*
