# OpenGrid Orchestrator -- module interfaces (MVP-S)

This is the fixed public-interface summary for every stub package the builder agents fill in. Each
package's `__init__.py` has the authoritative signatures + docstrings; this file is the index. Do not
change a signature listed here without the architect's approval (BUILD.md S4/S5a "Definition of done").

## Foundation (architect-owned, implemented, not a stub)

| Package | Purpose | Key entry points |
|---|---|---|
| `opengrid.core.physics` | SoC step, P/E/kVA capability, ramp | `soc_step`, `hub_capability`, `bank_capability`, `recharge_headroom`, `apply_ramp_limit` |
| `opengrid.core.limits` | Envelope checks (K1/K2/K4/K13) | `check_reserve_floor`, `check_hub_power`, `check_bank_kva`, `check_hub_ramp`, `check_fleet_ramp_cap`, `check_feeder_ramp_ceiling`, `check_one_buyer`, `check_commitment_lock` |
| `opengrid.core.products` | Product-rule quantity rounding | `derive_variable_kind`, `round_quantity`, `is_feasible` |
| `opengrid.core.crypto` | Ed25519 + JCS | `canonicalize_json`, `sign_payload`, `verify_payload`, `sha256_hex_of_json` |
| `opengrid.core.tracehash` | Hash chain | `compute_next_hash`, `verify_chain`, `checkpoint_hash` |
| `opengrid.core.timeutil` | Intervals, freshness, clock quality | `floor_to_interval`, `interval_bounds`, `is_stale`, `check_command_freshness`, `clock_offset_ok` |
| `opengrid.core.models.mqtt` | Wire contracts | `Telemetry`, `CommandBatch`, `Ack`, `StopEvent`, `Lease`, `ScadaBankSignal`, `ScadaUtilityInstruction`, `ScenarioControl` |
| `opengrid.core.models.engine` | `og.*` engine row shapes | `Contract`, `ProductRule`, `Opportunity`, `Obligation`, `Commitment`, `Reservation`, `Grant`, `Verdict`, `TraceRow`, ... |
| `opengrid.core.models.platform` | Fleet/health row shapes | `Hub`, `Bank`, `HubState`, `Heartbeat`, `Alert`, `FeedObs`, `FeedStatus` |
| `opengrid.platform.config` | TOML + `OG_CONFIG`/`OG_DB`/`OG_MQTT_ROOT` | `load_config`, `Config`, `resolve_secret` |
| `opengrid.platform.db` | psycopg pool + migrations | `make_pool`, `migrate_sync`, CLI `python -m opengrid.platform.db migrate` |
| `opengrid.platform.mqtt` | Client factory + schema validation | `build_client`, `topic`, `validate_payload` |
| `opengrid.platform.metrics` | Prometheus catalogue (02b S6.6) | module-level `Counter`/`Gauge`/`Histogram` objects |
| `opengrid.platform.log` | JSON logging | `configure_logging` |
| `opengrid.platform.heartbeat` | Heartbeat writer | `write_heartbeat` |
| `opengrid.platform.process` | Async main-loop + graceful shutdown | `run_forever` |
| `opengrid.trace` | Hash-chained trace store | `TraceStore.append/exists_preimage/verify/checkpoint/prune`, backend contract `TraceBackend` |

## Stub packages (signatures fixed, bodies `raise NotImplementedError` -- builders fill in)

| Package | Owner (BUILD.md S4) | Spec section | Public interface |
|---|---|---|---|
| `opengrid.feeds` | feeds | 02b S2 | `latest(series)`, `window(series, t0, t1)`, `run_feeds_process(cfg)` |
| `opengrid.forecast` | feeds | 02b S3 | `scenarios(horizon_start, horizon_end)` |
| `opengrid.contracts` | selector | 02a S1-S2 | `admit(...)`, `get_contract(id)`, `product_rules_for(id)`, `AdmissionError` |
| `opengrid.selector` | selector | 02a S3 | `run_gate(gate_kind, contract_scope=None)`, `load_frozen_commitments(...)` |
| `opengrid.fleet` | allocator | 02b S4 | `capability(bank_id, interval_start)`, `ingest_telemetry(payload)` |
| `opengrid.ledger` | allocator | 02a S4 | `reserve(...)`, `release(...)`, `ledger_version()`, `ReservationError` |
| `opengrid.allocator` | allocator | 02a S5 | `run_cycle(cycle_id)`, `substitute_hub(...)` |
| `opengrid.engine` | allocator | 02b S1.2 | `main(cfg)` (og-engine process wiring) |
| `opengrid.guardian` | guardian | 02a S6 | `evaluate_and_sign(batch)` |
| `opengrid.safestop` | guardian | 02a S6.5 | `engage(scope, scope_ref, reason, initiator_ref)`, `release(...)` |
| `opengrid.settle` | settle | 02a S7 | `settle(obligation_id, interval_start, interval_end)`, `run_trace_pruning_cycle()` |
| `opengrid.health` | settle | 02b S6.4 | `evaluate_heartbeats()`, `evaluate_hub_health()`, `evaluate_alerts()` |
| `opengrid.api` | api | 02b S7 | `create_app()` |
| `opengrid.ui` | api | 02b S8 | `build_router()` |

## Wire contracts (language-neutral, `interfaces/`)

- `interfaces/mqtt/*.schema.json` -- JSON Schema (2020-12) for every MQTT message kind.
- `interfaces/mqtt/topics.md` -- topic tree under `<root>` (`og/v1` prod, `ogtest/<ws>` per workspace).
- `interfaces/crypto.md` -- Ed25519 + JCS signing format, keyed by `key_id`.
- `interfaces/http/market-api.md` -- ERCOT/EIA/NWS request/response shapes the market simulator mimics.

## Process entry points (fixed, BUILD.md S4)

`python -m opengrid.feeds.main`, `opengrid.engine.main`, `opengrid.guardian.main`,
`opengrid.safestop.main`, `opengrid.settle.main`, `opengrid.api.main` -> systemd units `og-feeds`,
`og-engine`, `og-guardian`, `og-safestop`, `og-settle`, `og-api`. Each reads `OG_CONFIG` and the two
env files (`secrets.env`, `api_keys.env`); none of these `.main` modules exist yet -- each owning agent
adds `<package>/main.py` calling its package's stub entry point (e.g. `opengrid.engine.main`).
