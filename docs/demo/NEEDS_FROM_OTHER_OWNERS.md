# DEMO: what the walkthrough needs from paths DEMO does not own

Kept next to `docs/demo/README.md` (DEMO-2, release R2). Each item names the owner lane the lead routed it to
and the demo step that depends on it. Status re-checked against R2 (`main` `6470cfa`); items resolved in R2 or
earlier are listed at the end for the record. Items 12-16 concern the simulators and were not re-checked at R2.

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
5. **"SCADA silent" is never raised** (owner: health, ES07-S05; step 18). `health/__init__.py:364` never passes
   the SCADA-silent input. (Feed stale, `NO_NEW_COMMITMENTS`, is enforced since R2.)
6. **Market anomalies need `og-feeds` on the simulator** (owner: lead; steps 7-8, 18).
   - The server polls live ERCOT/EIA/NWS, so `/ogsim/` market anomalies do not reach it until `[feeds.*]` points
     at `ogsim.market`.
   - Since R2 the server's price freshness window is 2,700 s, longer than the `feed_outage_and_stale` scenario.
     Step 18 therefore runs on the dev stack (600 s) unless the lead shortens the window for the run.
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
12. **`tampered_unsigned_command` has no observable effect** (owner: sims; the forged-command moment is out of
    the script). Its self-test is never invoked on anomaly start, and no rejected ack reaches the orchestrator.
13. **Legacy scenario files use ids the simulator does not know** (owner: sims; not used by the script).
    `bank_overload_and_utility_limit`, `compound_stress` and `tampered_command` target `BANK_07`, `BANK_12`,
    `HUB_0501` and `HUB_0142`; the simulator's ids are `bank-NNN`/`hub-NNNNN`. A fix is on `wp/scenario-target-ids`.
14. **Random mode resumes after a control-plane restart** (owner: sims; "Before you start" item 1). Pause is held
    in memory only.
15. **The control plane's web page calls `/api/...` by absolute path** (owner: sims; "Before you start" item 6).
    Behind Apache's `/ogsim/` the buttons probably fail; the script uses `curl`.
16. **No "stop scenario" verb** (owner: market). `POST /api/scenarios/{name}/run` has no matching stop, and
    pending steps still fire after their first anomaly is cancelled.

## Resolved (R2 and earlier)

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
