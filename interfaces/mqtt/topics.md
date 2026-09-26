# MQTT topic tree

Authoritative source: `02b-mvp-s-spec-platform.md` §6.1–6.2, `05-integrations-guide.md` "Device interface".

Broker: Mosquitto, plain TCP (loopback-only for MVP-S). Every topic lives under a **configurable root**,
`[mqtt].topic_root` in `orchestrator.toml` / `OG_MQTT_ROOT` env override. Production root: `og/v1`. Test
workspaces use `ogtest/<workspace>` (e.g. `ogtest/arch`) so agents never collide on the shared broker.
Everywhere below, `<root>` stands for that configured prefix.

| Topic | Direction | QoS | Retain | Publisher | Subscriber(s) | Schema |
|---|---|---|---|---|---|---|
| `<root>/tel/<zone>/<bank_id>/<hub_id>` | hub → orchestrator | 0 | No | `og_sim` (or a real hub) | `og_engine` (fleet twin), `og_guardian` (its own hub-state read, GUARD-02); `og_api` does not subscribe -- its SSE streams poll Postgres | `telemetry.schema.json` |
| `<root>/cmd/<bank_id>/batch` | guardian → hubs | 1 | No | `og_guardian` | `og_sim` hub tasks for that bank | `command_batch.schema.json` |
| `<root>/ack/<hub_id>` | hub → orchestrator | 1 | No | `og_sim` (or a real hub) | `og_engine`, `og_guardian` | `ack.schema.json` |
| `<root>/stop/<scope>/<id>` | safestop → hubs | 1 | **Yes** | `og_safestop` | all hubs in scope, `og_api` | `stop.schema.json` |
| `<root>/lease/<hub_id>` | guardian → hub | 1 | **Yes** | `og_guardian` | that hub | `lease.schema.json` |
| `<root>/scada/<bank_id>` | SCADA → orchestrator | 0 | No | `og_sim` (simulated utility SCADA) | `og_engine` (`DIST_DEFERRAL` PI loop) | `scada_bank_signal.schema.json` |
| `<root>/scada/instruction/<bank_id>` | SCADA/utility → orchestrator | 1 | No | `og_sim` or utility system | `og_engine`, `og_guardian` (G-15) | `scada_utility_instruction.schema.json` |
| `<root>/scenario/cmd` | api/operator → sim | 1 | No | `og_api` (scenario panel) | `og_sim` | `scenario_control.schema.json` |
| `<root>/scada/wave/<zone>/<bank_id>/<hub_id>/summary` | hub → orchestrator | 0 | No | `og_sim` (or a real hub) | `og_engine` (allocator PQ self-check), the wave-ingestion service | `pq_waveform_summary.schema.json` |
| `<root>/scada/wave/<zone>/<bank_id>/<hub_id>/raw` | hub → orchestrator | 1 | No | `og_sim` (or a real hub), triggered only | the wave-ingestion service, `og_guardian` (G-22 fallback evidence) | `pq_waveform_raw.schema.json` |
| `<root>/scada/wave/<zone>/<bank_id>/<hub_id>/request` | orchestrator → hub | 1 | No | `og_api` / `og_engine` (on-demand capture trigger) | `og_sim` | `waveform_capture_request.schema.json` |
| `<root>/cmd/cal/<hub_id>` | guardian → hub | 1 | No | `og_guardian` | `og_sim` hub task for that hub | `calibration_command.schema.json` |
| `<root>/ack/cal/<hub_id>` | hub → orchestrator | 1 | No | `og_sim` (or a real hub) | `og_engine`, `og_guardian` | `calibration_ack.schema.json` |
| `<root>/site/<customer_id>/<site_id>/meter` | customer operator → orchestrator | 0 | No | `og_sim_customer` (or a real customer meter) | `og_engine` (DATA_CENTER closed-loop controller) | `customer_site_meter.schema.json` |
| `<root>/corridor/<customer_id>/<corridor_id>/current` | customer operator → orchestrator | 0 | No | `og_sim_customer` (or a real corridor monitor) | `og_engine` (PIPELINE_AC closed-loop controller) | `pipeline_corridor_current.schema.json` |

`<scope>` for `<root>/stop/*` is one of `fleet`, `zone/<zone>`, `bank/<bank_id>`; `<id>` is the `stop_event`
UUID. Stop state changes **only** through a signature-verified `StopEvent` (crypto.md §2.3): `ENGAGE` signed
by the safestop key, `RELEASE` signed by the guardian key (Tier-2 approved); the event's signed `scope`/`scope_id`
govern, not the topic. A retained **empty payload** (zero-length, or any JSON that is not a non-empty object,
e.g. `{}`, `null`, `[]`, `0`, `false`) is broker housekeeping that clears the retained message; it **never**
releases or otherwise changes a stop (K8: an unsigned message must not be able to release a stop).

## QoS rationale

- **QoS 0** (`tel`, `scada`, `scada/wave/.../summary`): high-frequency, loss-tolerant. A missed sample is
  covered 2s later (or at the next telemetry period) and only feeds the freshness/health model or the PQ
  monitoring loop's rolling baseline, never a hard safety decision on its own.
- **QoS 1** (`cmd`, `ack`, `lease`, `stop`, `scenario`, `scada/instruction`, `scada/wave/.../raw`,
  `scada/wave/.../request`, `cmd/cal`, `ack/cal`): at-least-once; consumers de-duplicate by
  `batch_id`/`seq`/`calibration_id`/`request_id` (commands/acks/requests) or by retained-message semantics
  (`lease`, `stop`). A triggered raw waveform capture and a calibration command are each a one-shot,
  operationally significant exchange, so at-most-once (QoS 0) is not acceptable for them even though the
  periodic summary channel tolerates loss.
- **Retained** (`lease`, `stop`): a hub that connects or reconnects mid-lease or mid-stop immediately gets
  the current state without waiting for the next publish.

## Client identities and ACLs

One MQTT user per process that touches the broker: `og_engine`, `og_guardian`, `og_safestop`, `og_sim`,
`og_sim_customer`, `og_api`. `feeds` and `settle` never touch MQTT. Each user's ACL is scoped to exactly the
rows above (e.g. `og_guardian` may publish `cmd/#` and `lease/#`, subscribe to nothing; `og_sim` may publish
`tel/#`, `ack/#`, `scada/#` (including `scada/wave/#`) and `ack/cal/#`, and subscribe to `cmd/#` (including
`cmd/cal/#`), `stop/#`, `lease/#`, `scenario/cmd` and `scada/wave/+/+/+/request`; `og_sim_customer` (the
customer-operator simulators, `integration-sims/src/ogsim/customer/`) may publish `site/#` and `corridor/#`
only, and subscribes to nothing). No anonymous access, no wildcard publish outside the configured root.
`og_guardian`'s existing `cmd/#` publish scope already covers `cmd/cal/<hub_id>` -- no new ACL grant is
needed for the calibration command itself, only for the new `scada/wave/#` / `ack/cal/#` topics.

## Command freshness fields (K6)

Every `command_batch` carries `epoch` (strictly increasing per hub, replay/rollback rejection) and `seq`
(strictly increasing within an epoch). `issued_at` is the envelope's `valid_from`; `expires_at` is the
absolute deadline after which the batch is void even if delivered late (lease-bounded, `fleet.lease_ttl_s`
= 30s by default). A hub holds its last signed setpoint if its lease expires without a fresh batch (local
autonomy, §4.4 of `02b`).
