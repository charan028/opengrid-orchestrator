# Team Notices

Newest first. **Every contributor and every AI agent must read this file at the start of each session.** Notices
change requirements or conventions and override older instructions in lane files.

---

## 2026-09-25 (evening): requirement changes

**1. Energy is checked continuously, not just capacity (all lanes).**
A home can have kW headroom but not enough charge. Every cycle, the remaining energy above reserve is compared with
each committed delivery; a shortfall risk sets AT_RISK and raises `ALR-ENERGY-SHORTFALL-RISK`. A missing or stale SoC
means **zero discharge**. The guardian projects SoC over the command lease (`G-01-ENERGY`). See `BUILD.md` §2a and
`docs/orchestrator/07-delivery/00-invariants.md` (K1/K13 additions).

**2. Base hardware is confirmed (all lanes; update any fixtures you write).**
Per battery unit: **39.2 kWh usable, 11 kW**. **20% of homes have 2 units: 78.4 kWh, 20 kW** (every 5th hub id,
`hub-00004`, `hub-00009`, …). Reserve 20% (7.84 kWh per unit). Banks are **~600 kVA feeder segments** (40 × 50 homes).
Do not use 13.5 kWh / 5 kW / 75 kVA anywhere.

**3. Service tailoring and power quality are coming next (MVP-S+; spec draft pending owner approval).**
Each customer gets a **ServiceProfile** (pipeline AC mitigation, data center and arbitrage are different services
with their own control, measurement and settlement) and a **PowerQualityEnvelope** (phase, voltage, current,
frequency, power factor, THD). Inverter imperfections (frequency/amplitude offsets, harmonics, phase error) are
modelled and aggregated.
Spec: `docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md` (draft).
**Do not implement it yet.** But do not hard-code per-service behaviour in new tests or UI: parametrise by service
type so profiles can plug in.

Lane impact:

- **rpagaria2000:** P1 property tests must include the energy-sufficiency invariants (no command implies discharge
  below reserve within its lease; Σ promised energy ≤ available energy). Q1 scenarios: add "energy exhausted
  mid-delivery → AT_RISK + substitution, never silent over-delivery".
- **fancyviper007:** U1 must cover the Dispatch screen's new energy margin / time-to-depletion columns. DEMO: add a
  step "energy runs low on a committed delivery → alert and substitution". Profiles and PQ screens come later.

**4. Process safety on the server (lead only, FYI).**
Test processes must never be stopped by pattern (`pkill -f`). See `BUILD.md` §6.

---

## 2026-09-25: team set-up

Lanes, work packages and the workflow are in `WORKBOARD.md`, `CONTRIBUTING.md` and `docs/team/lane-*.md`. The spec
set is under `docs/orchestrator/`.
