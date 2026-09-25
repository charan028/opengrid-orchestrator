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

## 7. Pre-existing mypy finding (not introduced by this pass)

`mypy orchestrator/src/opengrid/fleet` reports one pre-existing error unrelated to the A3 additions:
`fleet/__init__.py`'s `flush()` passes `classification if classification != "offline" else "stale"`
(a `str`) where `HubState.health` expects `Literal["online","stale","fault"]`. Not touched here since
it predates this pass and isn't one of A1-A7; flagging for whoever owns the next `opengrid.core.models`
lint pass.
