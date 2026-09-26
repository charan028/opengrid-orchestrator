# DEMO: what the walkthrough needs from paths DEMO does not own

Kept next to `docs/demo/README.md` (DEMO-2, release R2). Each item names the owner lane the lead routed it to
and the demo step that depends on it. Items resolved since DEMO-1 are listed at the end for the record.

## Open

1. **A guardian veto shows FAILED, not VETOED** (owner: MERGE, UI; step 17). The API answers a veto with 409
   and `{"detail": {...}}`; `ui/routes/fleet.py` reads `outcome` from the outer object, so the result partial
   falls through to FAILED with the raw "409 Conflict" text. Seen live on the dev stack.
2. **"Run chain verify" reports FAIL** (owner: MERGE, UI; step 24). `ui/routes/billing_audit.py` relays to
   `POST /og/api/trace/verify` without the caller's identity, so the API answers 401. Seen live: the UI shows
   FAIL, the API itself returns `passed: true`. The step uses `curl` meanwhile.
3. **A slow release shows FAILED** (owner: MERGE, UI; step 20). The UI's POST timeout is 5 s; the API waits up
   to 10 s for the guardian's signed release, so a release signed after 5 s shows FAILED although it lands, and
   PENDING never renders.
4. **Export CSV fails** (owner: MERGE, UI; step 22). The UI relays From/To unchanged; the API needs both as plain
   dates, so a blank or datetime-local filter fails. The step uses `curl` meanwhile.
5. **The safe-stop dialog counts down from 60 s, og-safestop keeps a proposal for 30 s** (owner: live-path;
   step 19). The fix returns `expires_in_s` from `safestop.confirm_window_s`. Until then the step says "confirm
   within 30 s".
6. **The AS panel shows the ECRS award as `ERCOT_AS · 4h`** (owner: MERGE; step 9). The API's obligation rows
   carry no product or variant.
7. **The AS deployment is not scoped to one award or capped by its product** (owner: live-path; step 10). The
   form offers "all held AS awards" and one global 1-240 min bound.
8. **The simulator's stop ramp never completes** (owner: FLEET-SIM; step 19). `ogsim/fleet/runtime.py` ramps
   from the commanded setpoint every tick, so a stopped hub commanded above about half its rating holds a
   reduced setpoint until its lease lapses.
9. **Restoring the full commitment after a best-effort shortfall** (owner: engine, found by Q4; step 14). After
   the cause is lifted the obligation stays at the best-effort remainder (`tests-e2e/functional` Q4 strict
   xfail).
10. **The "Commitment-lock events (K13)" table is never populated** (owner: engine/ledger; steps 6, 8, 14). No
    code writes commitment rows with a lock reason or a `supersedes`; the demo points at the trace instead.
11. **Degraded modes are display-only** (owner: engine/selector, ES07-S02; step 18). Nothing acts on
    `NO_NEW_COMMITMENTS`; "SCADA silent" is never raised.
12. **Market anomalies need `og-feeds` on the simulator** (owner: lead; steps 7-8, 18). The server polls live
    ERCOT/EIA/NWS, so `/ogsim/` market anomalies do not reach it until `[feeds.*]` is pointed at `ogsim.market`.
13. **`bank_overload` does not change the SCADA sim's readings on the dev stack** (owner: sims, found by Q2;
    step 11). `ALR-SCADA-OVERLOAD` cannot be shown there.
14. **`tampered_unsigned_command` has no observable effect** (owner: sims; the forged-command moment is out of
    the script). Its self-test is never invoked on anomaly start, and no rejected ack reaches the orchestrator.
15. **Legacy scenario files use ids the simulator does not know** (owner: sims; not used by the script).
    `bank_overload_and_utility_limit`, `compound_stress` and `tampered_command` target `BANK_07`, `BANK_12`,
    `HUB_0501`, `HUB_0142`; the simulator's ids are `bank-NNN`/`hub-NNNNN`.
16. **Random mode resumes after a control-plane restart** (owner: sims; "Before you start" item 1). Pause is held
    in memory only.
17. **The control plane's web page calls `/api/...` by absolute path** (owner: sims; "Before you start" item 6).
    Behind Apache's `/ogsim/` the buttons probably fail; the script uses `curl`.
18. **No "stop scenario" verb** (owner: market). `POST /api/scenarios/{name}/run` has no matching stop; pending
    steps still fire after their first anomaly is cancelled.
19. **Three committed customers on bank-012** (owner: lead; steps 5-8, 12). There is no seed for the demo's
    customers; the presenter creates them ("Before you start" item 3).

## Resolved since DEMO-1

- **Safe-stop release** (was item 6): the two-person release is built (og-op-a requests, og-op-b approves,
  self-approval refused); steps 19-20.
- **Every automatic batch vetoed on G-14** and **opportunities without a value** (were items 10-11): the
  functional suite commits, delivers and signs on the dev stack (21 of 22 functional tests pass).
- **The scenario-count assertion** (was item 1): `integration-sims/tests` loads every scenario file.
