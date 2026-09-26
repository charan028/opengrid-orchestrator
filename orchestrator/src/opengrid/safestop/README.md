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
