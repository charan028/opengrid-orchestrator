# MQTT topic tree

Authoritative source: `02b-mvp-s-spec-platform.md` §6.1–6.2, `05-integrations-guide.md` "Device interface".

Broker: Mosquitto, plain TCP (loopback-only for MVP-S). Every topic lives under a **configurable root**,
`[mqtt].topic_root` in `orchestrator.toml` / `OG_MQTT_ROOT` env override. Production root: `og/v1`. Test
workspaces use `ogtest/<workspace>` (e.g. `ogtest/arch`) so agents never collide on the shared broker.
Everywhere below, `<root>` stands for that configured prefix.

| Topic | Direction | QoS | Retain | Publisher | Subscriber(s) | Schema |
|---|---|---|---|---|---|---|
| `<root>/tel/<zone>/<bank_id>/<hub_id>` | hub → orchestrator | 0 | No | `og_sim` (or a real hub) | `og_engine`, `og_api` (sampled SSE) | `telemetry.schema.json` |
| `<root>/cmd/<bank_id>/batch` | guardian → hubs | 1 | No | `og_guardian` | `og_sim` hub tasks for that bank | `command_batch.schema.json` |
| `<root>/ack/<hub_id>` | hub → orchestrator | 1 | No | `og_sim` (or a real hub) | `og_engine`, `og_guardian` | `ack.schema.json` |
| `<root>/stop/<scope>/<id>` | safestop → hubs | 1 | **Yes** | `og_safestop` | all hubs in scope, `og_api` | `stop.schema.json` |
| `<root>/lease/<hub_id>` | guardian → hub | 1 | **Yes** | `og_guardian` | that hub | `lease.schema.json` |
| `<root>/scada/<bank_id>` | SCADA → orchestrator | 0 | No | `og_sim` (simulated utility SCADA) | `og_engine` (`DIST_DEFERRAL` PI loop) | `scada_bank_signal.schema.json` |
| `<root>/scada/instruction/<bank_id>` | SCADA/utility → orchestrator | 1 | No | `og_sim` or utility system | `og_engine`, `og_guardian` (G-15) | `scada_utility_instruction.schema.json` |
| `<root>/scenario/cmd` | api/operator → sim | 1 | No | `og_api` (scenario panel) | `og_sim` | `scenario_control.schema.json` |

`<scope>` for `<root>/stop/*` is one of `fleet`, `zone/<zone>`, `bank/<bank_id>`; `<id>` is the `stop_event`
UUID. A retained **empty payload** on a given `<root>/stop/<scope>/<id>` clears that stop (release).

## QoS rationale

- **QoS 0** (`tel`, `scada`): high-frequency, loss-tolerant. A missed sample is covered 2s later and only
  feeds the freshness/health model, never a hard safety decision on its own.
- **QoS 1** (`cmd`, `ack`, `lease`, `stop`, `scenario`, `scada/instruction`): at-least-once; consumers
  de-duplicate by `batch_id`/`seq` (commands/acks) or by retained-message semantics (`lease`, `stop`).
- **Retained** (`lease`, `stop`): a hub that connects or reconnects mid-lease or mid-stop immediately gets
  the current state without waiting for the next publish.

## Client identities and ACLs

One MQTT user per process that touches the broker: `og_engine`, `og_guardian`, `og_safestop`, `og_sim`,
`og_api`. `feeds` and `settle` never touch MQTT. Each user's ACL is scoped to exactly the rows above (e.g.
`og_guardian` may publish `cmd/#` and `lease/#`, subscribe to nothing; `og_sim` may publish `tel/#`,
`ack/#`, `scada/#` and subscribe to `cmd/#`, `stop/#`, `lease/#`, `scenario/cmd`). No anonymous access, no
wildcard publish outside the configured root.

## Command freshness fields (K6)

Every `command_batch` carries `epoch` (strictly increasing per hub, replay/rollback rejection) and `seq`
(strictly increasing within an epoch). `issued_at` is the envelope's `valid_from`; `expires_at` is the
absolute deadline after which the batch is void even if delivered late (lease-bounded, `fleet.lease_ttl_s`
= 30s by default). A hub holds its last signed setpoint if its lease expires without a fresh batch (local
autonomy, §4.4 of `02b`).
