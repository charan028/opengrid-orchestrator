# opengrid.safestop

**Purpose.** `og-safestop`, the independent stop-only authority (02a §6.5, K8: "a scoped safe stop
works when the engine is down"). Imports only `opengrid.core`, `opengrid.platform` and `opengrid.trace`
-- no `opengrid.engine`, `opengrid.guardian`, `opengrid.ledger` or `opengrid.allocator` import,
anywhere in the package (enforced by `tests/unit/safestop/test_import_isolation.py`).

## Interface

- `opengrid.safestop.engage(scope, scope_ref, reason, initiator_ref)` -- sign (stop-only Ed25519 key)
  and publish a retained `<root>/stop/<scope>/<id>` MQTT message, and record an `og.stop_event` row and
  a `SAFE_STOP` trace record. `scope` is `"FLEET" | "ZONE" | "BANK"`.
- `opengrid.safestop.release(scope, scope_ref, approver_ref)` -- **always raises**
  `ReleaseNotPermittedError`. The stop-only key can never sign `RELEASE`; that requires the guardian's
  Tier-2 (two-person) signing path plus a fresh operator action, never this process.
- `opengrid.safestop.relay_guardian_release(event)` -- the only way a RELEASE reaches the hubs. og-guardian
  NOTIFYs `{"action": "PUBLISH_RELEASE", "event": <StopEvent>}` on the request channel; this process
  verifies it itself (guardian key from `[safestop].guardian_public_key_path`, default
  `/etc/opengrid/guardian_ed25519.pub`; `action="RELEASE"`; a `guardian-*` key id; `approver_ref` present
  and different from `issued_by`), then traces it, publishes it retained on the ENGAGE's own topic (the
  RELEASE reuses the ENGAGE's `stop_id`), and records the `og.stop_event` RELEASE row. No key file: no
  relay. Idempotent by signature. See `opengrid.guardian.stop_release` for the end-to-end path.
- Both delegate to whatever `SafestopService` `configure_service()` last installed; `main.py` wires the
  real Postgres/MQTT-backed service at process startup, tests wire an in-memory fake.
- Host CLI (K8, for when og-api or og-safestop itself is down):
  `python -m opengrid.safestop.cli engage --scope {FLEET,ZONE,BANK} --ref <id> --reason <text>
  --operator <id> --confirm <SCOPE>:<ref>`. `--confirm` must repeat `<SCOPE>:<ref>` exactly (FLEET:
  omit `--ref`, confirm `FLEET:FLEET`), else exit 2 with nothing signed. Same stop-only key, Postgres
  backends and `SafestopService.engage` as the daemon (trace -> `stop_event` row -> retained publish),
  `initiator_kind=OPERATOR`, `initiator_ref=operator:<id>@host-cli`, own MQTT client `og-safestop-cli`
  (user `og_safestop`, `OG_MQTT_SAFESTOP_PASSWORD`). Prints `stop_id=` and `topic=`. There is no
  `release` subcommand. Needs Postgres (K10: no stop without its trace pre-image).
- Utility L2 intake (K5/K8, `l2_intake.py`): the daemon subscribes to `<root>/scada/instruction/+` on a
  second MQTT client `og-safestop-l2` and engages a BANK stop on the named bank for every unexpired
  `BLOCK`/`ESTOP` `ScadaUtilityInstruction` (`LIMIT` is the guardian's). Reason
  `L2 <kind> <instruction_id> from <issued_by>`, `initiator_kind=UTILITY`, `initiator_ref=utility:<issued_by>`.
  At most once per `instruction_id` (in-memory set + `PgStopEventBackend.has_l2_engage` after restart); a
  new instruction id for an already-stopped bank still engages its own stop. A payload whose `bank_id`
  differs from the topic's bank level, or is malformed, is logged and skipped.

## Request intake (two-step confirmation)

`og-api` (not yet built) is expected to `NOTIFY` on Postgres channel
`opengrid.safestop.pg_backend.REQUEST_CHANNEL` (`og_safestop_request`) with a JSON payload:

- Step 1, arm: `{"action": "PROPOSE", "proposal_id": "<uuid>", "scope": "BANK", "scope_ref": "bank-07",
  "reason": "...", "initiator_ref": "operator:alice"}`
- Step 2, confirm (within `safestop.confirm_window_s`, default 30s):
  `{"action": "CONFIRM", "proposal_id": "<uuid>"}`

Only a matching CONFIRM within the window causes `og-safestop` to call `engage()`. A lone PROPOSE (or a
CONFIRM after the window) never stops anything (TS-10-03: "a single click never stops the fleet").
`og-safestop` holds proposals in memory only -- it is otherwise stateless (TS-C-03b), so a proposal lost
on restart simply requires the operator to re-arm.

## Publish outbox and metrics

Every accepted ENGAGE and relayed RELEASE is written with its `og.stop_outbox` entry in one transaction and
published (QoS 1, retained) until the broker acknowledges it, including after a reconnect. The drain keeps
acceptance order within a scope and gives ENGAGE priority only across scopes. Each drain reads the oldest 100
entries plus every entry of each scope with a queued ENGAGE, so a stop never waits behind a backlog. A
permanently failing entry holds back its scope's later RELEASEs, never an ENGAGE. An entry that fails
permanently `outbox_max_attempts` (5) times is dead-lettered and `ALR-STOP-PUBLISH-DEAD-LETTER` is raised (one
open alert per entry, re-raised on every drain while missing, traced when newly raised). A dead-lettered ENGAGE
is still retried on every drain after the live queue, and traced when it finally goes out. Known item (M6):
dead-letter alerts are one per entry, not coalesced into a single summary alert.

`/metrics` is served on `[metrics].safestop_port` (default **9106**, loopback `[metrics].bind_host`), e.g.
`og_mqtt_reconnects_total{client="safestop"|"safestop-l2"}` and `og_mqtt_connected`. The guardian's is
`[metrics].guardian_port` (9103).

## Keys

`python -m opengrid.safestop.keys keygen --key-id safestop-2026a --pubkey-out
<path-for-sims>/safestop-pub.txt` generates a fresh stop-only Ed25519 keypair, prints the seed once
(put it in `secrets.env` as `OG_SAFESTOP_SIGNING_SEED`) and writes only the public key to
`--pubkey-out` for `ogsim`'s verification config. This key is distinct from the guardian's signing key
and can only ever sign `action="ENGAGE"`.

## How to test

- Unit (no DB/MQTT): `pytest orchestrator/tests/unit/safestop -q`
- Import isolation: `pytest orchestrator/tests/unit/safestop/test_import_isolation.py -q`
- Server integration (`og_t_stop` / `ogtest/stop`, workspace `stop`):
  `powershell -File tools/remote.ps1 -Ws stop -Cmd "cd orchestrator && python -m pytest tests/integration/safestop -q"`
  -- publishes a stop, checks it is retained, a reconnecting subscriber receives it, and (TS-06-23,
  the K8 topology proof) that it still works with `og-engine`/`og-guardian` not running.
