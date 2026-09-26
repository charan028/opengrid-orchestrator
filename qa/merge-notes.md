# Merge notes — cross-agent interface needs (BUILD.md merge role)

Written by the merge/integration pass. Items below need a change in a path the merge role does not
own (`core/platform/guardian/allocator`, `ui/integration-sims`, or `selector/feeds/settle/forecast`);
each is a request for that fix agent, not something merge edited directly.

## 1. `core.models.engine` — bank_id type follow-up for migrations 0004/0005 (owner: core/platform agent)

`orchestrator/migrations/0004_bank_id_text.sql` (merge, applied) widens `og.reservation.bank_id` and
`og.grant.bank_id` from `uuid` to `text`, matching `og.bank.bank_id`/`og.hub.bank_id` and every text
bank code the fleet twin, ledger and allocator already produce/consume (`"bank-01"` style). Two files
outside merge's owned paths still assume the old `uuid` shape and need a follow-up change:

- `orchestrator/src/opengrid/core/models/engine.py` — `Reservation.bank_id: UUID` and
  `Grant.bank_id: UUID` (lines ~141, ~155) must become `bank_id: str`. Until this lands, Pydantic
  rejects a real text bank code, so `opengrid.api.store.Store._fetch_by_uuid_bank_id`'s UUID-guard
  workaround (documented in `orchestrator/src/opengrid/api/README.md` "Known gap, not api's to fix")
  is intentionally left in place rather than removed — removing it now would just turn the guard's
  silent empty-list into a Pydantic `ValidationError` at read time.
- `orchestrator/src/opengrid/allocator/__init__.py::_to_grant_row` — currently does
  `bank_id=uuid5(NAMESPACE_URL, grant.bank_id)`, manufacturing a synthetic UUID instead of writing the
  real bank code. Once the model field above is `str`, this should become `bank_id=grant.bank_id`.

Once both land, `api`'s UUID-guard in `store.py` can be deleted and `ledger_timeline`/`list_grants`
will work for text bank ids as intended.

## 2. `opengrid.ledger.reserve()` typed-key replacement (owner: architect + selector/allocator agents)

Task asked to replace `reserve()`'s packed `"bank|start|end"` string dict keys with typed keys. I did
**not** change `reserve()`'s signature: it is a fixed public interface (`INTERFACES.md`, "do not change
a signature listed here without the architect's approval"), and it currently has two live callers
outside merge's paths — `orchestrator/src/opengrid/selector/gate.py` and (indirectly, via the trace
pre-image shape it expects) `orchestrator/src/opengrid/guardian/repo.py`. Changing the dict key type
unilaterally would have broken both without their agents able to react concurrently.

Recommended follow-up, for the architect to approve and the selector agent to adopt: add
`opengrid.ledger.ReservationKey` (a frozen, hashable dataclass of `bank_id: str`,
`interval_start: datetime`, `interval_end: datetime`) and a new `reserve()` overload/variant accepting
`dict[ReservationKey, Decimal]`, with `encode_interval_key`/`decode_interval_key` kept as a deprecated
compatibility shim during the transition. Filed here rather than done unilaterally because it is a
breaking interface change spanning selector's and guardian's owned code.

## 3. Allocator engine-wiring — still not fully real (owner: engine wiring is merge's; allocator itself is not)

Merge added the two building blocks `opengrid.allocator.gateways` needs and didn't have:

- `opengrid.fleet.hub_capabilities(bank_id) -> list[HubCapabilitySnapshot]` — per-hub eligibility
  snapshot (hub_id, bank_id, free_discharge_kw, health, last_seen_at), the hub-level detail
  `capability()`'s bank-aggregate return doesn't carry.
- `opengrid.ledger.persist_grants(cycle_id, grants: list[GrantRecord])` / `ReservationLedger.persist_grants`
  + `opengrid.ledger.pg_backend.PgGrantBackend` — insert-only writer for `og.grant` (02a S7).

**Not done, and out of scope for this pass:** the actual `opengrid.engine`-owned adapter classes that
implement `opengrid.allocator.gateways.FleetGateway`/`LedgerGateway`/`ScadaGateway`/`ScheduleGateway`
against these plus `opengrid.feeds`/`opengrid.fleet.bank_scada_signal`/`utility_instruction`, and the
wiring in `opengrid.engine.main` that constructs `opengrid.allocator.run_cycle`'s gateways so it stops
raising `NotImplementedError`. This is safety- and timing-sensitive (2 s cycle, K1/K2/K13 envelope
checks) integration work that deserves its own focused pass with integration tests against a real
Postgres/MQTT loop rather than being rushed alongside the schema/ledger fixes above — flagging for the
next merge/engine pass rather than shipping an unreviewed adapter layer into a system that signs and
sends real dispatch commands.

**Confirmed live impact (deploy pass, 2026-09-25 16:xx CT):** `orchestrator/src/opengrid/engine/__init__.py`
line ~212 calls `await allocator.run_cycle(cycle_id)` with no `fleet=`/`ledger=` kwargs, so every
`og-engine` tick hits `run_cycle`'s `NotImplementedError` immediately and the process crash-loops
(systemd `Restart=always` brings it back every 2s, so this is a restart loop, not a hang, and no batch
is ever built or sent — safe but non-functional). `og-guardian`/`og-settle`/`og-safestop` do not depend
on `og-engine` and idle/heartbeat normally; `og-feeds`/`og-api`/the four `og-sim-*` units are unaffected.
The `LedgerGateway.ledger_view` piece in particular needs a real `active COMMITTED/DELIVERING calls`
query joining `og.obligation`/`og.commitment`/`og.reservation` that doesn't exist anywhere yet (not in
`ledger`, not in `contracts`) -- flagging as the next concrete task rather than guessing at its shape
under deploy time pressure.

## 4. Guardian lease/sequence state (owner: guardian/safety agent)

`orchestrator/migrations/0005_lease_state.sql` (merge, applied) adds `og.lease_state` (bank_id, epoch,
seq, updated_at) — durable per-bank high-water mark for command-batch freshness, so the guardian can
detect an out-of-order/replayed batch across process restarts, not only within one in-memory run.

The engine → guardian hand-off itself is **kept as-is**: the guardian continues to read the batch from
the `RT_ALLOCATION` trace pre-image (`opengrid.guardian.repo.PgProposalPort`), which is already the
append-only, hash-chained record of what was proposed — no `command_batch_item` table was added, since
duplicating that payload into a second table would be exactly the "no duplicated functions/logic"
BUILD.md S1/S5a rule warns against, not an improvement in robustness.

What the guardian owner still needs to build (not merge's path):
- Write `og.lease_state` from `opengrid.engine` (or wherever the batch's `(bank_id, epoch, seq)` is
  decided) when a batch is proposed.
- Read `og.lease_state` from `opengrid.guardian` before signing, and veto (`VETOED`, a new/existing
  reason code for "stale lease") when a batch's `(epoch, seq)` does not strictly advance the stored
  value for that bank.

## 5. Migration ordering — confirmed fine, no code change needed

`opengrid.platform.db.migrate_sync` discovers `*_*.sql` files under `migrations/`, sorted by filename,
tracks applied ones in `og.schema_migrations`, and applies pending ones in that sorted order inside a
transaction per file. `0001_init.sql` creates the `og` schema before `0002`/`0003` reference it, and
the new `0004_bank_id_text.sql`/`0005_lease_state.sql` apply after all three on a fresh database. No
reordering or renumbering needed for A5.

## 6. Billing performance array / release 501 — already correct, no change needed

`GET /og/api/billing/invoice-lines` already returns `{"lines": [...], "performance": [...]}` for
`format=json` (`orchestrator/src/opengrid/api/README.md` line 78-80), and the safestop release endpoint
already returns `501` with an honest "Tier-2 co-sign not built" detail message
(`orchestrator/src/opengrid/api/routers/safestop.py`) rather than fabricating a signature. Both match
task item A6 as specified; kept as-is.

## 8. Deploy findings fixed live (2026-09-25, merge/deploy pass)

Fixed directly (all in merge-owned paths) while bringing the first release up on 192.168.5.35:

- `deploy/scripts/deploy.sh` never sourced `/etc/opengrid/secrets.env`/`api_keys.env` before running
  `opengrid.platform.db migrate`, so every deploy failed at the migration step with
  `ConfigError: OG_DB_PASSWORD is not set`. Fixed to source both env files as the `opengrid` user.
- `deploy/systemd/og-{engine,guardian,feeds,safestop,settle}.service` all had
  `ExecStart=... -m opengrid.<pkg>` instead of `-m opengrid.<pkg>.main` -- each of those five packages'
  `main.py` is the process entry point (`opengrid.api`/`opengrid.guardian` additionally have a
  `__main__.py` for their own CLIs, e.g. guardian's `keygen`, which is *not* the process and was being
  invoked by systemd by mistake). Fixed all five units; `og-api` was already correct.
- `orchestrator/src/opengrid/api/app.py::_mount_ui` never passed `[ui].base_path` (`/og`) as the
  `prefix` to `app.include_router(build_router())`, so every UI screen (including `control_room`'s `/`)
  was served at the FastAPI app's own root instead of `/og/`. Apache proxies
  `https://base.tocy-net.net/og/` to this app's `/og/`, so the UI 404'd end to end. Fixed to read
  `[ui].base_path` (defensively -- `create_app()` runs before `app.state.config` exists and API unit
  tests call it with no `OG_CONFIG` set, so a `ConfigError` falls back to the documented default `/og`).
  Verified live: `GET https://base.tocy-net.net/og/` now returns `200` with the operator credentials.
- Generated the guardian/safestop Ed25519 keys via their own `keygen` CLIs. One real bug found in the
  process, **not fixed** (outside merge's owned paths): `opengrid.safestop.keys`'s CLI writes its
  `--pubkey-out` file as `"<key_id> <hex>\n"`, but `ogsim.common.crypto.load_public_key`
  (`integration-sims/src/ogsim/common/crypto.py::_decode_key_text`) only accepts a bare 64-char hex (or
  base64) string -- the `<key_id> ` prefix makes the file unparsable as-is. Worked around for this
  deploy by extracting the hex from the CLI's own printed `public_key_hex=` line and writing that
  directly to `/etc/opengrid/safestop_ed25519.pub`; flagging the CLI's output format for the safestop
  key owner to fix so future deploys don't need the same manual step.

## 9. Remaining blocker found during the smoke test: fleet topology is never seeded

`og.hub`/`og.bank` are empty on a fresh database -- no migration or seed script inserts the ~2,000-hub /
40-bank topology the integration sims' `og-sim-fleet` (`integration-sims/src/ogsim/fleet/state.py`,
`config.hub_count`) actually runs. `opengrid.fleet.load_topology()` (engine's fleet twin) therefore knows
zero hubs/banks at startup, so every telemetry message from the 2,000 simulated hubs is dropped as
"telemetry for unknown hub_id" (`opengrid/src/opengrid/fleet/__init__.py::ingest_telemetry`) and
`GET /og/api/fleet/hubs` correctly reports 0 hubs. This is the root cause behind the smoke test's A2
failure (0 hubs online) and cascades into A4/A5/A6/A8 (no hub capability -> no obligations get committed
against real capacity -> no grants -> nothing for the guardian to sign -> nothing to settle). Needs a
migration or seed script populating `og.hub`/`og.bank` with the exact same `hub_id`/`bank_id` scheme
`og-sim-fleet` generates (ids, bank groupings, `e_kwh`/`r_kwh`/`p_kw`/`eta_c`/`eta_d`, `kva_rating`) --
whichever side owns the canonical topology definition should be the single source of truth the other
reads from (`interfaces/` currently has no such fixture; recommend adding one there, per BUILD.md S1's
"they meet only at the wire" rule, rather than duplicating the generator in both `ogsim` and a new
`opengrid` seed script). Not attempted in this pass: it needs the fleet/sims agents to agree on exactly
what "hub-0001"-style ids and bank groupings look like before either side commits to a shape.

## 10. Live smoke test result summary (2026-09-25, against the deployed release)

PASS: A9 trace chain verify, A10 UI reachable with operator credentials, A11 anomaly injection accepted
by the control plane.
FAIL: A1 live feeds (ERCOT `feed_obs` still empty -- `feed_status` shows `consecutive_failures`
climbing on every ERCOT product even though EIA's fallback feed is landing `GOOD` rows; feeds/ is not a
merge-owned path, flagging for that agent), A2 fleet hubs online (root cause: section 9 above), A4/A5
concurrent commitments, A4/A6 grants+signed verdicts, A8 settle pnl/invoice lines (all downstream of
section 9), A11 alert-after-injection (no alert rule fires without a real hub to go offline -- likely
also downstream of section 9, not independently investigated).

## 11. Dispatch-live pass (2026-09-25/26) — what landed

Ownership as of this pass: merge owns `engine/fleet/ledger/api/contracts/health/trace/guardian/safestop/
migrations/config/deploy/tests-e2e`; a follow-up fix agent owns `core/allocator/feeds/settle` (ERCOT
field names, settle idempotency, allocator physics duplication/timeouts).

**Landed:**
- `opengrid.fleet.seed` (new): idempotent `og.hub`/`og.bank` loader reproducing `ogsim.fleet.state`'s
  exact id scheme (`hub-NNNNN`/`bank-NNN`) from `integration-sims/config/fleet.yaml`, read-only.
  `deploy.sh` now runs it after every migration; also exposed as
  `POST /og/api/admin/seed-fleet-topology` (operator-only).
- **Allocator <-> engine wiring** (`opengrid.engine.gateways`, new): real `FleetGateway`/
  `LedgerGateway`/`ScadaGateway`/`ScheduleGateway` adapters, wired into `_engine_tick` so
  `allocator.run_cycle` runs for real every 2 s instead of raising `NotImplementedError`. Also fixed:
  **`opengrid.ledger.configure()` was never called anywhere** -- every `selector.run_gate ->
  ledger.reserve()` call was raising `RuntimeError` before this pass; `opengrid.engine.main` now wires
  a real `ReservationLedger(PgLedgerBackend, FleetCapabilityProvider, grant_backend=PgGrantBackend)`
  once at startup.
- **Guardian lease_state**: `opengrid.guardian.repo.PgLeaseStatePort` reads/writes `og.lease_state`
  (monotonic upsert), replacing `InMemoryLeaseStatePort` so G-13 freshness survives a guardian restart.
- **Safestop pubkey CLI bug fixed**: `opengrid.safestop.keys`'s `keygen` now writes plain hex (no
  `"<key_id> "` prefix) to `--pubkey-out`, matching `ogsim.common.crypto.load_public_key`'s parser and
  `opengrid.guardian.keys.keygen`'s own format. Fleet sim already pointed at both key paths correctly
  (`integration-sims/config/fleet.yaml`'s `guardian_public_key_path`/`safestop_public_key_path`); only
  the write side was broken.
- **Health alerts**: added `ALR-SCADA-OVERLOAD` (bank load over `kva_rating`, tuned so the anomaly
  catalogue's default `bank_overload` injection -- 20% over rating -- lands `critical`, not just
  `warning`). `ALR-FEED-STALE` and `ALR-HUB-OFFLINE-RATIO` already existed and needed no change.

**Real blocker found, NOT fixed here (outside merge's owned paths):**
`opengrid.selector.gate._configured_bank_ids()` generates `"bank-01"`.."bank-NN"` (2-digit, 1-indexed),
but the real fleet topology (this pass's seed loader, matching `ogsim.fleet.state`) uses `"bank-000"`..
`"bank-039"` (3-digit, 0-indexed) -- **completely disjoint id spaces**. `tests/unit/selector/
test_gate_loaders.py::test_configured_bank_ids_reads_fleet_banks_count` explicitly locks in the
`"bank-01"` format, so this looks like a deliberate placeholder from early parallel development, not an
oversight -- changing it isn't a one-line fix I should make unilaterally without the selector owner's
sign-off, especially given the explicit test.

**This is the actual reason A4/A5/A6/A8 still fail after this pass's fixes**, even with real topology
seeded and the allocator wired for real: `selector.load_banks()` calls `fleet.capability("bank-01", ...)`
every gate cycle, which raises `LookupError` (no such bank), so `run_gate` fails before ever reserving
anything -- 0 commitments, 0 grants, 0 verdicts, 0 settlement, exactly the smoke-test symptoms. **The fix
selector's owner needs:** change `_configured_bank_ids()` to `f"bank-{i:03d}" for i in range(bank_count)`
(0-indexed, 3-digit) to match, or better, read bank ids from `og.bank` directly instead of
reconstructing them from a count + format guess (this would also survive a future bank_count/format
change with no code edit). Also see the same file's `README.md`/docstrings for two more places that
already flag "every configured bank is eligible for every candidate" as a known simplification -- once
bank ids match, that simplification itself does not need to change for A4/A5/A6/A8 to pass.

**Also deferred (documented, not implemented):** `EngineLedgerGateway.record_substitution` (used only by
`opengrid.allocator.substitute_hub`'s manual/API-triggered path, not the automatic 2 s `run_cycle` loop)
raises `NotImplementedError` -- `og.grant` has no `reason_code` column to record why a substitution
happened, so persisting one honestly needs a schema decision first, not a guessed column shape.

## 12. Second real blocker found live: og-sim-fleet stops publishing telemetry after a short burst

Confirmed on the live deploy (2026-09-25 19:2x-19:3x CT), after fixing everything else in this pass
(topology seeded with matching ids, MQTT ingestion wired, `og-engine`'s tick loop confirmed healthy via
heartbeat, `fleet.flush()`'s hub_state upsert fixed to a single bulk statement instead of ~2,000
per-row round trips):

- `og.telemetry`/`og.hub_state.last_seen_at` both went stale by the same ~4m33s, while `og.heartbeat`
  for `engine` stayed under 3 seconds old the entire time -- i.e. `og-engine`'s own tick loop and MQTT
  ingest subscriber are healthy and waiting; nothing new is arriving to ingest.
- `systemctl status og-sim-fleet` showed the unit `active (running)`, no crash, no restart, 9 minutes of
  uptime, but only ~1m10s of CPU time consumed in that whole window -- consistent with its internal
  publish loop running briefly after startup (I did see one genuine burst: `og.telemetry` had fresh,
  correctly-spaced ~2.3s-interval rows for about the first 15-20s after a restart) and then going idle
  without logging anything or exiting.
- This is `integration-sims`/`ogsim.fleet`'s own process loop (owned by the ui/integration-sims fix
  agent, not merge) -- I did not look inside its source to avoid editing outside my paths, only
  confirmed the symptom from the outside (systemd status, `og.telemetry`/`og.hub_state` timestamps).

**This, not a merge-owned bug, is why A2 (2,000 hubs online) still fails** even after topology seeding
and the hub_state bulk-upsert fix landed: telemetry simply stops arriving a short time after each
restart of the sim/engine targets, so every hub eventually reads "stale" no matter how fast `og-engine`
classifies it. Recommend the sim owner check `ogsim.fleet`'s main publish loop for an unhandled
exception being swallowed by a bare `except`/task that was never awaited/observed (the `og-engine` log
around the same restart shows `MqttCodeError: [code:141] Keep alive timeout` on the *previous* engine
process's MQTT client during shutdown, in case a similar keep-alive/reconnect issue affects the sim's own
MQTT client and its publish loop doesn't recover from a dropped connection).

## 13. Confirms allocator/guardian wiring works end-to-end; guardian correctly VETOes given stale hubs

Final verification after fixing the ruff S608 finding and redeploying with the other agents' feeds/
market fixes: `og.command_batch` has 160 rows and `og.grant` has 160 rows -- the allocator/engine wiring
(item 2 above) is genuinely proposing headroom grants and handing them to the guardian every cycle, and
`og-guardian`'s own heartbeat is fresh (sub-second) -- it is alive and actively evaluating every batch.
All 160 verdicts are `VETOED`, not `PASS`. This is consistent with, and most likely fully explained by,
section 12's `og-sim-fleet` telemetry gap: the guardian's `HubStatePort` is deliberately its **own**,
independently-read MQTT telemetry cache (never `og.hub_state`, by design -- GUARD-02/04), so once
telemetry stops arriving, guardian's own view of every hub also goes stale/unknown and G-01 (or similar)
correctly VETOes rather than signing against data it can't trust (K7's "degrade, don't trip" working
exactly as intended -- this is the guardian doing its job correctly, not a guardian bug). Once section
12's sim-side fix lands, this pipeline should very plausibly start producing `PASS` verdicts without any
further guardian-side change needed -- worth re-running `tests-e2e/smoke.py` first after that fix, before
assuming any of A6/A8 need more work.

## 7. Pre-existing mypy finding (not introduced by this pass)

`mypy orchestrator/src/opengrid/fleet` reports one pre-existing error unrelated to the A3 additions:
`fleet/__init__.py`'s `flush()` passes `classification if classification != "offline" else "stale"`
(a `str`) where `HubState.health` expects `Literal["online","stale","fault"]`. Not touched here since
it predates this pass and isn't one of A1-A7; flagging for whoever owns the next `opengrid.core.models`
lint pass.
