# DEMO: what the walkthrough needs from paths DEMO does not own

Kept next to `docs/demo/README.md` (DEMO-2, release R2). Each item names the owner lane the lead routed it to
and the demo step that depends on it. Status re-checked against R2 (`main` `6470cfa`); items resolved in R2 or
earlier are listed at the end for the record. Items 13-14 concern the simulators and were not re-checked at R2.

## Open

1. **"Run chain verify" reports FAIL from an unfiltered page** (owner: MERGE, UI; step 24).
   - R2 fixed the identity: the button forwards it now.
   - But the button posts `?class=&from=&to=` (`ui/templates/billing_audit.html:116`), and the route relays the
     empty strings. `TraceVerifyRequest`'s datetime fields refuse `""` with a 422, which the page renders as FAIL.
   - The step sets From/To first or uses `curl`.
2. **The AS Deploy form still offers "all held AS awards" and 1-240 min** (owner: UI; step 10). og-api now
   refuses both (R2), with a raw 422 or 409 on the page. The lead reports the option removed on `integ/ui-ai`
   (`e64e818`) for R3. og-api also accepts a second deployment of an award that is already deployed (routed to
   FOLLOWUPS).
3. **Restoring the full commitment after a best-effort shortfall** (owner: DISPATCH, found by Q4; step 14).
   The root cause is in the code: og-engine passes the latest stored utility (L2) instruction to the allocator
   without checking `expires_at` (`orchestrator/src/opengrid/engine/gateways.py:518-527`). A lifted BLOCK or
   LIMIT therefore keeps the bank cut, and the delivery stays at the best-effort remainder
   (`tests-e2e/functional` Q4 strict xfail).
4. **The "Commitment-lock events (K13)" table is never populated** (owner: engine/ledger; steps 6, 8, 14). No
   code writes commitment rows with a lock reason or a `supersedes`; the demo points at the trace instead.
5. **"SCADA silent" is shown only** (owner: engine, ES07-S05; step 18). R2 hotfix v3 (`afb26c2`) raises
   `ALR-SCADA-SILENT` and `DIST_DEFERRAL_OPEN_LOOP` after 60 s without SCADA readings, but only the UI reads the
   mode; the DIST_DEFERRAL loop keeps the last reading. (Feed stale, `NO_NEW_COMMITMENTS`, is enforced since R2,
   and since hotfix v3 is set only by the price feed.)
6. **Market anomalies need `og-feeds` on the simulator** (owner: lead; steps 7-8, 18).
   - The server polls live ERCOT/EIA/NWS, so `/ogsim/` market anomalies do not reach it until `[feeds.*]` points
     at `ogsim.market`.
   - Since R2 the price freshness window is 2,700 s (server and dev stack), longer than the
     `feed_outage_and_stale` scenario. Step 18 therefore injects a 60-minute `stale_posting` during setup.
7. **"LP value added (latest selector gate)" is never available** (owner: MERGE, API; step 21).
   `orchestrator/src/opengrid/api/routers/lp_value.py` exists but og-api does not mount it
   (`api/app.py:130-171`).
8. **`bank_overload` on the dev stack** (owner: sims, found by Q2; step 11).
   - The dev-stack test for it is still a strict xfail at R2
     (`tests-e2e/functional/safety/test_ts06_guardian_and_safe_stop.py:290-301`), so `ALR-SCADA-OVERLOAD` may not
     show there. One live run will tell.
   - After 3 overloaded readings the SCADA simulator also issues a LIMIT at 90% of the rating with no expiry
     (`integration-sims/src/ogsim/scada/instructions.py:36-47`). og-engine keeps applying it (item 3), so the
     demo bank stays capped at 540 kW.
9. **One permanent `ALR-XFMR-UNMAPPED` warning per bank** (owner: data/guardian; "Before you start" item 2). No
   hub has a service-transformer mapping yet (`og.hub.transformer_id`, migration 0029), and the alert never
   clears by itself.
10. **The Hubs table shows at most 200 hubs** (owner: UI; step 3). The map draws every hub since R2.
11. **The System Health processes and feeds tables, the Control-room banner and "Guardian escalations" reflect
    page load** (owner: UI; steps 2, 16, 18).
12. **The control plane's page behind `/ogsim/`** (owner: deploy/FLEET-SIM; "Before you start" item 6). Since
    `afb26c2` the page honours `X-Forwarded-Prefix` or `OGSIM_CONTROL_BASE_PATH`
    (`integration-sims/src/ogsim/control/app.py:63-71` there). Neither `deploy/apache/opengrid.conf` nor
    `deploy/systemd/og-sim-control.service` sets them, so its calls still go to the domain root; the script uses
    `curl`.
13. **`tampered_unsigned_command` has no observable effect** (owner: sims; the forged-command moment is out of
    the script). Its self-test is never invoked on anomaly start, and no rejected ack reaches the orchestrator.
14. **Legacy scenario files use ids the simulator does not know** (owner: sims; not used by the script).
    `bank_overload_and_utility_limit`, `compound_stress` and `tampered_command` target `BANK_07`, `BANK_12`,
    `HUB_0501` and `HUB_0142`; the simulator's ids are `bank-NNN`/`hub-NNNNN`. A fix is on `wp/scenario-target-ids`.

## Resolved (R2 and earlier)

- **R2 hotfix v3 (`afb26c2`):**
  - **The random-mode pause is saved** across a control-plane restart.
  - **The control plane's page** has Run, Stop and Stop all buttons.
  - **Scenarios can be stopped:** `POST /api/scenarios/{name}/stop` and `/api/scenarios/stop-all` cancel the
    pending steps and end what was injected.
- **R2:**
  - **A guardian veto shows VETOED** with its rule ids (was: FAILED with "409 Conflict").
  - **A slow release shows PENDING**, not FAILED (the screen waits 15 s).
  - **Export CSV** always sends plain dates.
  - **The safe-stop dialog counts down from 30 s**, og-safestop's window.
  - **The AS panel shows the product** (`ECRS · 1 h hold`).
  - **og-api deploys one award, capped by its product** (the form still offers more; item 2).
  - **The simulator's stop ramp completes** in about 4 s.
  - **Feed stale is enforced**: og-engine skips intake and the selector commits nothing new.
  - **The power-quality, $/kW, bid-funnel, fleet-map and bulk-command APIs are in the release.**
- **The demo's committed customers:** `dev/scripts/seed_demo_customers.py` (dev-stack PR #35) commits the three
  seeded demo contracts through admission and the selector, idempotently, and names the demo bank the steps
  use.
- **Safe-stop release:** the two-person release is built (og-op-a requests, og-op-b approves, self-approval
  refused); steps 19-20.
- **Every automatic batch vetoed on G-14** and **opportunities without a value:** the functional suite commits,
  delivers and signs on the dev stack (21 of 22 functional tests pass).
- **The scenario-count assertion:** `integration-sims/tests` loads every scenario file.
