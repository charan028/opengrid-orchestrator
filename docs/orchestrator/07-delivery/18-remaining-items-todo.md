# Remaining items: TODO (after r3.4.7)

Status as of 2026-09-27 06:45 CT. Production is r3.4.7 (`ad864a4`) on base. Each item has an owner lane and a target
release. Tick an item only when it is deployed on base and operationally verified.

Legend: **P1** blocks value or safety, **P2** needed for scale or completeness, **P3** hygiene.

## A. Today (2026-09-27)

- [ ] **P1 Toll ramp, live test.** 16:30 CT: a 3 MW, 8-minute toll-path call, with a 20 s engine hold and one induced SCADA
      LIMIT veto; verify the signed steps stay within one cycle's bound and that there are no K13 violations. 16:45 CT:
      leave the Austin Energy sim's 20 MW call untouched and verify its delivery record. Owner: release manager and the
      lead. Procedure: `/opt/opengrid/work/HANDOVER-r342.md` §8.
- [ ] **P1 Safe-stop broker-blip fix (e0b37d0), live test** on the dev stack: a 20 s broker outage must not restart
      og-safestop, and an outage longer than 60 s still must. Owner: workstation.
- [ ] **P2 Performance before/after addendum** (r3.4.1 → r3.4.3 → r3.4.5 with ingest decoupling off and on), PR
      `wp/perf-r343-addendum`. Owner: workstation.
- [ ] **P2 Executive demo:** 3-minute narrated MP4 plus slides in `D:\Projects\OpenGrid\demo\r343-exec-demo\`. Owner: DEMO.
- [ ] **P2 Docs and Help sync for r3.4.4–r3.4.7:** partial-call handling (`R-AS-PARTIAL-DEPLOYMENT`, the G-19 pro-rata
      floor), the safestop backoff and 60 s exit, the copilot screening changes, the test-database alert and guard,
      and the r3.4.5 utility-stop release. Owner: HELP/DOCS.
- [ ] **P3 Re-enable the simulator's random faults** after 18:00 CT. Owner: release manager.

## B. Next release (r3.5)

### B1. Contract onboarding interface for Base (NEW, owner request)
- [ ] **P1 Contract onboarding in the console and API**, so Base can enter real regulated and utility contracts, such as
      LCRA and Rayburn, without a database script.
  - **Utilities:** create or edit a utility (id, name, territory zones, TDSP/NOIE status, and the counterparties for
    member cities or co-ops).
  - **Contracts:** create a contract with its terms: service type and variant (for example REGULATED_CAPACITY/TOLLING),
    $/kW-yr, call length cap, reservation window and days, committed kW per zone or bank, start and end dates,
    charging rules (D-30 windows, solar), and settlement terms. Maintain supporting documents and references.
  - **Lifecycle:** DRAFT → PENDING_APPROVAL → ACTIVE → SUSPENDED/EXPIRED. Activation needs a two-person approval
    (Base operator plus approver), and every change is audited.
  - **Sample contracts:** replace a "Sample Contract" with a real one; samples can never become active (as today).
  - **Availability:** activating a real contract flips that zone's banks from UNAVAILABLE / REGULATED_NO_CONTRACT to
    AVAILABLE. The engine and selector pick the change up without a restart, K15/G-33 territory rules stay enforced,
    and a preview shows the capacity impact before activation.
  - **Utility access:** enable the utility's customer-API identity and grid-link point list once its contract is
    ACTIVE (they stay disabled until then).
  - **Validation:** capacity against fleet headroom, overlapping obligations, and the commitment lock (K13) on
    existing obligations.
  - **Tests:** API, UI (Playwright) and e2e. The e2e onboards a sample-to-real LCRA contract and checks that the banks
    become available and a utility call is accepted.
  - **Docs:** operator guide, Help playbook ("Onboard a regulated utility contract"), and decision-log entry D-39.
  - Owners: CONTRACTS/API (new lane), UI-FLEET for the screens, SAFETY to review the activation path.

### B2. Delivery and dispatch
- [ ] **P1 Home-battery ramp from the signed setpoint** (not lagging telemetry). ECRS calls deliver only 40–60% today.
      SAFETY's conditions apply: the guardian falls back per hub when a hub is not following its setpoint; the reload
      stays bounded (7,500); only published batches count; and a test covers the export cap. Owners: DISPATCH and SAFETY.
- [ ] **P2 Delivery verification:** score the final partial 30 s bucket at the end of the window, and flag a call cut
      short by a safe stop as STOPPED. Owner: DELIVERY-VERIFY.
- [ ] **P2 Re-test the stress scenarios** "DB lock recovery" and "bulk command followed" at a fleet size the engine
      sustains. Owner: workstation.

### B3. Scale (see the performance report, doc 17)
- [ ] **P1 Enable telemetry ingest decoupling** (`[ingest].telemetry_decoupled`) after the performance run and a
      dev-stack test. Owner: DISPATCH.
- [ ] **P2 Update the fleet snapshot incrementally, and scan banks with arrays,** so that allocator plus energy_check p99
      is at most 150 ms at 7,500 hubs. Owners: PERF-OPT with DISPATCH.
- [ ] **P2 Find the og-engine RSS growth** (+275 MB/h in the 7,509-hub soak) and fix it if it is a leak. Owner: PERF-OPT.
- [ ] **P3 Separate og-ingest process** (design document first). Owner: DISPATCH.

### B4. Copilot
- [ ] **P3 Answers other than fleet answers should degrade gracefully** when screening is unavailable. Measure the
      real token and latency figures after r3.4.5. Owner: COPILOT.

## C. Product gaps carried over from earlier releases

- [ ] **P1 Turn on the data-lifecycle timer** (retention, cold export and rollups). It is installed but disabled, so the
      database grows without bound. Owners: HEALTH and the release manager.
- [ ] **P2 Guardian checks that exist only as codes:** G-07, G-08, G-10–G-12 and G-16–G-18. Owner: SAFETY.
- [ ] **P2 Alert rules that are never raised:** ALR-METER-EXPORT-LIMIT and ALR-TEMPERATURE-LIMIT. Owner: HEALTH.
- [ ] **P2 K15 contract-admission check** (territory-bound energy checked at admission). Owners: SAFETY and CONTRACTS
      (ties into B1).
- [ ] **P2 G-31 field-name defect,** which means the peak-power allowance is never used. Owner: SAFETY.
- [ ] **P2 Rooftop PV size (`pv_rated_kw`) is never written:** device info should report it. A 10 kW default is used
      today. Owners: FOLLOWUPS and FLEET-SIM.
- [ ] **P2 Regional solar share needs a load-weighted split** (C9) before settlement uses it. Owner: FORECAST.
- [ ] **P2 Work orders:** create, assign and close them in the UI and API. Owner: ASSETS.
- [ ] **P3 Confirm the D-28 regional solar field names** against ERCOT NP4-745. Owner: FORECAST.
- [ ] **P3 Per-site TDSP attribute** (TNMP). Owner: MARKET-MODEL.

## D. Integrations and deployment

- [ ] **P2 Real ERCOT MMS/EWS adapter** to replace the simulator (the interface exists). Owner: AS-POLL.
- [ ] **P2 ICCP/TASE.2 transport** for the grid link (DNP3 today; needs a licensed stack). Owner: GRID-LINK.
- [ ] **P2 Kubernetes installer:** r3.4.3 container smoke test on a non-production machine, then a real-cluster
      deployment. Owner: K8S.
- [ ] **P3 Credentials file:** move the values into a separate 0600 file, leaving the names file free of secrets. Owner:
      the release manager.
- [ ] **P3 Parked:** the Fleet reason-code filter (`REGULATED_NO_CONTRACT`), which is built but unpushed on UI-FLEET's
      `integ/fix-ui-r344`.

## E. Owner decisions and inputs needed

- [ ] **Real LCRA and Rayburn contract terms** (and the actual counterparties). Enter them through B1 once it is built.
- [ ] **Grid-connection agreement figure** for the 20 MW set (export and service limit; placeholder 20,408 kW).
- [ ] **Enable the grid link for Austin Energy for real:** certificates, peer IPs and firewall port 20001.
- [ ] **Confirm the utility-stop release rule (Q10):** the utility lifts with the instruction id, then two operators
      release. It is implemented that way provisionally.
- [ ] **A real Kubernetes cluster** for the installer.
