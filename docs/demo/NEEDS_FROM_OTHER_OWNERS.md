# DEMO: things the walkthrough needs from paths DEMO does not own

Found while writing `docs/demo/README.md` and the `demo-*.yaml` scenarios (branch `wp/fancyviper007-lane`).
Each item names the owner from `BUILD.md` §4 and the demo step that depends on it.

## 1. `integration-sims/tests/test_scenarios.py` hard-codes the scenario count (owner: market)

`test_all_six_shipped_scenarios_load_without_error` asserts `len(scenarios) == 6`. Adding any file to
`integration-sims/scenarios/` (WORKBOARD's DEMO path is exactly `integration-sims/scenarios/demo-*.yaml`)
fails it: with the four demo files, `pytest tests -q` reports `1 failed, 261 passed, 1 skipped`
(`assert 10 == 6`). Every demo file loads through `load_scenario` without error (checked with the same
function `app.py` uses). Please change the assertion to `>= 6`, or count only non-`demo-*` files.

## 2. `tampered_unsigned_command` has no observable effect yet (owner: sims; README step 13)

`ogsim.fleet.runtime.FleetRuntime.self_test_tampered_unsigned_command` builds the forged batch and proves
`BAD_SIGNATURE`, but nothing calls it: `AnomalyEngine._apply` in `fleet/anomalies.py` deliberately skips the
type ("applied by the runtime's ack path"), and `__main__.py` never invokes the self-test on anomaly start.
So injecting `demo-04-tampered-command` currently only registers an active anomaly. The README's Expect
column describes the intended behaviour (log line `fleet self-test: forged command correctly rejected
(BAD_SIGNATURE)` and a rejected ack on `og/v1/ack/hub-00142`). Needed: on `ActiveAnomalyStarted` for this
type, run the self-test for the target hub's bank and publish the rejected ack (`build_ack` already reports
`reject_reason`). Fallback for the presenter until then: the hub's power/last command id on the Fleet
drill-down do not change, and the invariant tiles stay 0.

## 3. Rejected acks are not surfaced in the orchestrator (owner: engine + ui-a; README step 13)

`opengrid.core.models.mqtt` knows `reject_reason: BAD_SIGNATURE | STALE_EPOCH | STALE_SEQ | EXPIRED`, but no
engine code consumes `ack/*` for display, and no UI widget or alert shows a rejected command. For the demo a
"Rejected commands" counter or row on the Fleet drill-down (or an `ALR-COMMAND-REJECTED` alert) would make
step 13 visible on screen instead of in `journalctl -u og-sim-fleet`.

## 4. Per-hub substitution rows cannot be written (owner: allocator/engine + architect; README step 11)

`EngineLedgerGateway.record_substitution` (`engine/gateways.py`) raises because `og.grant` has no
`reason_code` column, so `R-SUBSTITUTION` never reaches the Dispatch "Real-time grants & substitutions" table.
The demo shows substitution indirectly (the obligation's granted kW on bank-001 stays constant while
`hub-00001` is offline and the other 49 hubs' power rises on the Fleet screen). A `reason_code` column on
`og.grant` plus the gateway write would let the table show the swap explicitly.

## 5. The demo needs three committed customers on `bank-012` (owner: lead, L1 intake / D0 dev stack)

Steps 5-8 and 10 assume at least three customers with contracts (one of them `DIST_DEFERRAL`) and
opportunities selected and COMMITTED on `bank-012` for the demo window. `POST /og/api/contracts` and
`POST /og/api/opportunities` exist and the README shows the `curl`, but there is no seed script; the presenter
depends on the lead's L1 intake or a `dev/` seed to have them in place. A `tools/`/`dev/` seed for "3 demo
customers on bank-012" would remove the manual step. Also `profile_ref` has no documented accepted values;
the README uses `"demo"`.

## 6. Safe stop cannot be released (owner: safestop/guardian; README steps 15-16 and reset)

`POST /og/api/safestop/{scope}/{scope_id}/release` returns `501` by design (no Tier-2 co-signed path). Each
demo run therefore consumes one bank (`bank-022`) until the lead clears the stop on the server. A documented
lead-side reset command (or the Tier-2 path) is needed for back-to-back runs.

## 7. No "stop scenario" verb on the control plane (owner: market)

`POST /api/scenarios/{name}/run` starts a scenario; there is no endpoint to cancel the pending steps of a
running one (only `DELETE /api/anomalies/{id}` for already-injected anomalies). `demo-03`'s +90 s step
still fires after its first step is cancelled. A `POST /api/scenarios/{name}/stop` that cancels the task
and its active ids would make "Reset between runs" one call.

## Confirmed on a local live stack (2026-09-25)

8. **Steps 9-10 (overload alert) cannot fire yet**: SCADA readings are never persisted to `og.feed_obs`
   (`source='scada'`) and the health evaluator is never run by `og-settle`, so `ALR-SCADA-OVERLOAD` never
   opens even though the simulator reports 164% of rating. See `tests-e2e/chaos/NEEDS_FROM_OTHER_OWNERS.md`
   items 5 and 8. The DIST_DEFERRAL response in the engine is independent of this and may still show.
9. **Working live**: the price spike (step 7) lands in `og.feed_obs` at 5000 $/MWh within one feeds poll;
   manual command propose/confirm returns a real guardian verdict; bank-scoped safe stop engages and
   publishes the signed retained stop; the four demo scenarios are listed and runnable from the control
   plane.
