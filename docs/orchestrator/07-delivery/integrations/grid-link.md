# Utility grid-control link (D-34)

As of 2026-09-26 (release r3.4.1)

The grid-control link lets a utility's control system (EMS) talk to the orchestrator, with Austin Energy
first. Over it the EMS:

- sends **D-29 toll calls**: discharge only, capped at 90 minutes;
- sends the existing **L2 LIMIT and BLOCK** instructions, per bank or per zone;
- reads **status and telemetry** back.

The link uses a real control-system protocol. It **ships disabled**.

| | |
| --- | --- |
| Code | `orchestrator/src/opengrid/integrations/grid_link/` |
| Process | og-engine (the process that owns SCADA ingest) |
| Point list contract | `interfaces/grid_link/opengrid-gridlink-v1.json` |
| Simulators | `ogsim.protocols.dnp3_master` (the EMS side), `ogsim.scada.grid_link` (L2 from the SCADA sim), `ogsim.utility_aen.channels.grid_link` (toll calls from the Austin Energy sim) |
| Decision | `11-decision-log.md` D-34 |

## 1. Protocol choice

**The link runs DNP3 (IEEE 1815-2012) over TCP with mutual TLS (the IEC 62351-3 profile). OpenGrid is the
outstation and the utility EMS is the master.** It uses the orchestrator's own IEEE 1815 codec
(`integrations/scada_dnp3/codec.py`). **No new dependency is added.**

This choice was checked against the PyPI policy (maintained, permissive licence, pinned) on 2026-09-26.

| Option | Candidates | Finding |
| --- | --- | --- |
| (a) ICCP / TASE.2 | none | No Python TASE.2 client exists on PyPI. `pyiec61850-ng` 1.6.1.10 (MMS through libiec61850) is **GPLv3**, and MMS alone does not give bilateral-table data values or device control. The MZ Automation, SISCO and Triangle TASE.2 stacks are commercial. |
| (b) IEC 60870-5-104 | `c104` 2.2.1, `pyiec104` 21.6.20 | `c104` (Fraunhofer FIT) is **GPLv3**. `pyiec104` is an MIT-classified ctypes wrapper around FreyrSCADA's **proprietary** binary library. Neither is permissive. |
| DNP3 (library) | `dnp3py` 0.4.0 (MIT), `yadnp3` 3.2.1.2 (Apache-2.0), `nfm-dnp3` 1.0.1 (MIT), `dnp3-python` 0.3.0b2 | The owner has already declined `dnp3py` as young and unvetted (`protocol-adapters.md` §3.1). `yadnp3` is a one-person experimental fork of opendnp3, which reached end of life in 2022. `nfm-dnp3` is master-only and has a single release. `dnp3-python` has no Python 3.13 wheels. |
| **DNP3 (in-house)** | `opengrid.integrations.scada_dnp3.codec` | **Chosen, and approved by the lead on 2026-09-26.** The codec is already reviewed and in production code for the DNP3 SCADA master. The grid link adds the outstation application layer: READ, SELECT/OPERATE, DIRECT_OPERATE, CROB and analog output. |

**Real utility EMS links in Texas are usually ICCP (TASE.2).** This implementation is DNP3. The adapter is
**protocol-neutral** so that ICCP can be added without changing the service (§8). If Austin Energy asks
for ICCP, the missing piece is a licensed TASE.2 stack, as it is for the SCADA adapter
(`protocol-adapters.md` §5.4).

## 2. Architecture

```
utility EMS (DNP3 master) --TLS--> og-engine: Dnp3GridLinkServer --> OutstationSession (DNP3 app layer)
                                                  |                          |
                                                  | GridCommand              | LinkStatus (points)
                                                  v                          |
                                        GridLinkService (protocol-neutral) --+
                                           | toll calls          | L2 levels            | telemetry
                                           v                     v                      v
                                  opengrid.calls (core)   L2Book -> MqttBridgeSink    opengrid.fleet twin
                                  (operator route and     <root>/scada/instruction/#
                                   customer API use the   (og-engine ingest + guardian G-15)
                                   same function)
```

- **Toll calls** go through `opengrid.calls.issue_call`, `cancel_call` and `call_status`. This is the
  same core function that the operator route (`/og/api/dispatch/as-deployments`) and the utility customer
  API call. The grid link adds no call logic of its own. The core owns:
  - validation against the tolling obligation, and the 90-minute cap;
  - overlap and idempotency checks;
  - the `og.as_deployment` row and the `og.dispatch_call` ledger;
  - the trace, the operator_action row and the operator alert.

  The link passes `origin = GRID_LINK`, principal `grid_link:<utility_id>` and idempotency key
  `ems:<ems_call_id>`. It converts the link's discharge magnitude to the core's signed kW (`-setpoint`).
- **L2 LIMIT and BLOCK** become `ScadaUtilityInstruction`s. They are built through the shared
  `InstructionTracker`, which gives deterministic ids and the lift convention `expires_at == issued_at`.
  They are published on the **existing** topic `<root>/scada/instruction/<bank_id>` by MQTT user
  `og_gridlink`. og-engine's ingest loop and the guardian's independent L2 port receive them exactly as
  they receive SCADA-feed instructions (K5, G-15).
- **Telemetry** comes from the in-process fleet twin:
  - available kW is `fleet.capability().max_discharge_kw`, with health, SoC and active L2 already
    applied;
  - delivered kW is the discharge of the online hubs;
  - SoC is stored energy over capacity of the online hubs.

## 3. Commands and status (protocol-neutral)

| Command | Meaning |
| --- | --- |
| `TollCall(ems_call_id, setpoint_kw, duration_min)` | Discharge `setpoint_kw` (a magnitude, > 0) for `duration_min` (1–90) from now, on the utility's tolling obligation. |
| `CancelCall(ems_call_id \| None)` | End that call now. With no id, end the utility's current call. |
| `L2LimitValue(target, kW)`, `L2Limit(target, on/off)` | The L2 discharge ceiling for a bank or zone target. |
| `L2Block(target, on/off)` | L2 block for a target. |
| `Heartbeat()` | The EMS is alive. |

Outbound `LinkStatus` fields:

- available kW and delivered kW;
- call state, with codes IDLE 0, ACCEPTED 1, ACTIVE 2, ENDED 3, REJECTED 4;
- the call id, the reason code (numeric, §4.3) and the call's own delivered kW;
- SoC % for the utility and per bank;
- the heartbeat count;
- the link-healthy, telemetry-stale and L2-active flags;
- per-target L2 echoes and per-bank L2 ceilings.

## 4. DNP3 point list "opengrid-gridlink-v1"

Both sides hard-code these indices. Each side has a test that asserts its constants equal the contract
file `interfaces/grid_link/opengrid-gridlink-v1.json`.

In the tables, `t` is a target's position in the utility's `l2_targets` list and `b` is a bank's position
in its `banks` list.

### 4.1 Inbound (EMS → OpenGrid)

| Object | Index | Role | Rule |
| --- | --- | --- | --- |
| AO g41v1/v2/v3 | 0 | CALL_SETPOINT_KW | Stages a value in `(0, max_setpoint_kw]`. |
| AO | 1 | CALL_DURATION_MIN | Stages an integer from 1 to `max_duration_min` (≤ 90, D-29). |
| AO | 2 | CALL_ID | Stages an integer from 1 to 2³¹−1. The EMS's own id, used as the idempotency key. |
| AO | 16 + t | L2_LIMIT_KW | The limit value for target t, from 0 to 1,000,000 kW. |
| CROB g12v1 | 0 | CALL_EXECUTE | ON only. Builds `TollCall` from the three staged values, then clears them. |
| CROB | 1 | CALL_CANCEL | ON only. Uses the staged CALL_ID if it is fresh; otherwise the current call. |
| CROB | 2 | HEARTBEAT | Any code. |
| CROB | 16 + 2t | L2_LIMIT_ACTIVE | ON sets the limit; OFF lifts it. |
| CROB | 17 + 2t | L2_BLOCK | ON sets the block; OFF lifts it. |

- ON codes are PULSE_ON (0x01), LATCH_ON (0x03) and CLOSE (0x41). OFF codes are PULSE_OFF (0x02),
  LATCH_OFF (0x04) and TRIP (0x81).
- Staged values are held **per association** and expire after `select_timeout_s` (10 s). A stale or
  missing value makes CALL_EXECUTE answer TIMEOUT or FORMAT_ERROR. An old setpoint can therefore never be
  executed later.

### 4.2 Outbound (OpenGrid → EMS)

| Object | Index | Point |
| --- | --- | --- |
| AI g30v5 (float), or g30v1 on request | 0–7 | AVAILABLE_KW, DELIVERED_KW, CALL_STATE, CALL_ID, CALL_REASON, CALL_DELIVERED_KW, SOC_PCT, HEARTBEAT_COUNT |
| AI | 16 + t | The L2 limit in force for target t, or −1 when there is none. |
| AI | 100 + 4b + {0,1,2,3} | Bank b: SOC_PCT, AVAILABLE_KW, DELIVERED_KW, and the L2 ceiling (−1 when there is none). |
| BI g1v2 | 0–6 | LINK_HEALTHY, CALL_ACTIVE, ALARM_HEARTBEAT_LOST, ALARM_CALL_REJECTED, ALARM_TELEMETRY_STALE, ALARM_L2_ACTIVE, TOLL_CALLS_ENABLED |
| BI | 16 + 2t, 17 + 2t | Echoes of target t's LIMIT and BLOCK. |

- A bank with no telemetry is served with the COMM_LOST flag. Its value is never invented.
- **CALL_DELIVERED_KW (AI 5) is the call's MEASURED delivery** (r3.4.3, D-38). The value comes from the core call status, which reads `og.delivery_record`, written by og-settle every 15 s in 30 s buckets. It lags real time by about 30–60 s. While the call is unmeasured or its data is stale, AI 5 is served with the COMM_LOST flag and is never invented. It was always COMM_LOST in r3.4.1 and r3.4.2, when only granted kW existed. DELIVERED_KW (AI 1) remains the fleet-twin discharge over the utility's banks.
- An unknown SoC is served as −1.
- Static data only. Class 1/2/3 polls return empty, so the EMS scans with integrity (Class 0) polls at its
  own rate, typically every 2–4 s.

### 4.3 Codes

**Control status (IEEE 1815 Table 11-4):**

| Code | Status | When |
| --- | --- | --- |
| 0 | SUCCESS | Accepted for processing. The outcome appears on CALL_STATE. |
| 1 | TIMEOUT | Staged values are stale. |
| 2 | NO_SELECT | OPERATE did not match a SELECT (same object bytes, sequence select+1, within 10 s). |
| 3 | FORMAT_ERROR | Nothing staged. |
| 4 | NOT_SUPPORTED | The index is not in this utility's point list (the allow-list). |
| 9 | NOT_AUTHORIZED | The utility's `accept_toll_calls` or `accept_l2` is off, or `sbo_only` refused a direct execute. |
| 10 | AUTOMATION_INHIBIT | The heartbeat was lost. |
| 12 | OUT_OF_RANGE | A value is outside its range. |

**CALL_REASON:**

| Code | Reason |
| --- | --- |
| 0 | None |
| 1–12 | The core codes, in order: R-CALL-NOT-FOUND, NOT-DEPLOYABLE, STATE, NO-PRODUCT-DURATION, DURATION-CAP, OVERLAP, CHARGE-REFUSED, OVER-COMMITTED, OUTSIDE-WINDOW, IDEMPOTENCY-CONFLICT, RATE-LIMIT, FLEET-WIDE |
| 50 | R-GL-LINK-DOWN |
| 51 | R-GL-CORE-TIMEOUT (re-read on the next status refresh) |
| 52 | R-GL-INTERNAL (for example, the trace could not be written, so the call is not issued, K10) |
| 99 | Unknown |

### 4.4 Supported DNP3 subset

- READ with qualifier 0x06, for g60v1..4, g30v0/1/5 and g1v0/2.
- SELECT, OPERATE, DIRECT_OPERATE and DIRECT_OPERATE_NR, for g12v1 and g41v1/2/3, with qualifiers
  0x17/0x28.
- Application CONFIRM. A multi-fragment response waits for the master's CONFIRM before sending the next
  fragment.
- Link services: RESET_LINK_STATES, TEST_LINK_STATES, REQUEST_LINK_STATUS, and confirmed and unconfirmed
  user data.

Anything else is answered with IIN2 bits (function or object unknown, or parameter error) and is never
guessed.

**Not implemented:** event buffers and unsolicited responses, time sync, and DNP3 Secure Authentication.
The link relies on TLS instead.

## 5. Security and failure modes

### 5.1 Admission (per association)

A refused association is closed and traced as `AUTHZ_DENY / GRID_LINK_DENY`.

Checks, in order:

1. **Peer allow-list.** The peer IP must be in `allowed_peers` (IP/CIDR). This list is required.
2. **Mutual TLS** (IEC 62351-3 profile).
   - The server requires a client certificate issued by `client_ca_file`.
   - The certificate CN must be in `allowed_peer_cns`.
   - A listener on a non-loopback address is **refused at config load** unless TLS is enabled and the CN
     list is non-empty.
3. **At most `max_associations`** concurrent associations (default 2: primary and backup control centre).
4. **Link addressing.** Frames must come from `master_address` to `outstation_address`. Anything else is
   dropped.

### 5.2 Authorisation of controls

The point list itself is the allow-list: an index outside it answers NOT_SUPPORTED.

The per-utility switches `accept_toll_calls` and `accept_l2` narrow it further. When one is off, the
matching controls answer NOT_AUTHORIZED.

Control mode is `sbo_or_direct` (the default) or `sbo_only`. `sbo_only` requires SELECT-before-OPERATE
for CALL_EXECUTE and CALL_CANCEL.

**Deny tracing is rate-limited** to one trace per peer per reason every 5 s, so a hostile peer cannot
flood `og.trace`.

### 5.3 Tracing

**Every inbound command except a heartbeat is traced before it takes effect** (K10), as:

- stream `grid_link:<utility_id>`;
- decision `OPERATOR_ACTION`, event `GRID_LINK_COMMAND`;
- a payload with `origin: "GRID_LINK"`, the peer and the command fields.

Heartbeats are counted, not traced. Heartbeat **transitions** are traced as `GRID_LINK_STATE`
(`HEALTHY` / `HEARTBEAT_LOST`).

If the trace write fails:

- a toll call is **not** issued (R-GL-INTERNAL);
- L2 levels are still applied, because a safety constraint beats audit completeness. The failure is
  logged.

The core adds its own `DISPATCH_CALL*` trace rows for each call.

### 5.4 Heartbeat loss (fail safe)

The link is healthy only while heartbeats arrive within `heartbeat_timeout_s` (default 30 s). It starts
unhealthy until the first heartbeat.

While it is not healthy:

- **New toll calls are refused** with AUTOMATION_INHIBIT, and ALARM_HEARTBEAT_LOST is on.
- **Cancels are accepted.** Reducing discharge is always safe.
- **A running call continues** to its own end. It is bounded by its product rule (90 min, D-29), and the
  guardian, energy and PQ checks apply to it as to any obligation.
- **L2 levels stay exactly as last received.** No lift is emitted, and the instructions carry
  `expires_at = null`. This is the existing SCADA rule for a silent feed.

### 5.5 Other failure modes

| Failure | Behaviour |
| --- | --- |
| Core call function slow (> `core_timeout_s`, 5 s) | CALL_STATE shows REJECTED with reason 51. The next status refresh re-reads the call by its idempotency key and corrects the state. The EMS may re-send the same id; it is idempotent. |
| MQTT bridge down | An L2 instruction is kept and re-sent on each status tick until it is delivered. It is never lost, even though the tracker has moved on. |
| Telemetry missing | The affected banks' points are served with COMM_LOST and ALARM_TELEMETRY_STALE is on. Nothing is invented. |
| og-engine restart | **L2 levels are restored (r3.4.3).** Before it listens, the link replays its own traced L2 commands from `og.trace` in sequence order: stream `grid_link:<utility>`, `GRID_LINK_COMMAND`, kept 400 days as operator actions. It then re-delivers the resulting instructions, and traces `GRID_LINK_STATE` = `L2_RESTORED`. There is no extra table: the audit trail is the persisted state. Refused commands were never traced, so they are never replayed. If the read fails (10 s bound), the failure is logged and the link starts without restored levels. Call state is re-read from the core by the EMS's call id. |
| TLS or bind error at start | That utility's link is logged and stays down. og-engine keeps running. |
| Unknown `utility_id` (not in `UTILITY_IDS`) | That entry is skipped and logged. The other utilities start. |

## 6. Ports (grid-link internals)

| Port | Implementation | Contract |
| --- | --- | --- |
| `TollCallPort` | `calls_port.CoreTollCallPort` over `opengrid.calls` | A refusal is returned as an outcome, never raised. |
| `TelemetryPort` | `fleet_telemetry.FleetTelemetryPort` | Per-bank figures from the twin. Unknown banks are omitted. |
| `TraceAppend` | `TraceStore.append` | |
| `BanksOfZone` | `fleet.known_bank_ids()` filtered by `fleet.bank_zone()` | Expands zone targets. |

## 7. Configuration (`[grid_link]`)

The block ships in `orchestrator/config/orchestrator.toml` with every switch off. It holds three
utilities, all disabled:

- AUSTIN_ENERGY (port 20001, outstation 10, bank-040..049 plus bank-sub-LZ_AEN-00);
- LCRA (port 20002, outstation 11);
- RAYBURN (port 20003, outstation 12). LCRA and RAYBURN have no contract yet (D-37).

Peers and CNs are placeholders: 192.0.2.0/28 is a documentation range.

To enable a utility:

1. Fill `allowed_peers`, `allowed_peer_cns`, certificate paths and addresses from the signed point-list
   agreement.
2. Install the certificates under `/etc/opengrid/certs/` (mode 640).
3. Provision MQTT user `og_gridlink`.
4. Open the firewall port for the EMS's addresses only.
5. Set `[grid_link].enabled = true` and the utility's `enabled = true`.
6. Restart og-engine.

**Loopback enablement (r3.4.3, owner request).** The owner asked for the link to be operational after
deploy, so `deploy/scripts/grid_link_enable_loopback.sh` (run as root by the release manager) enables
AUSTIN_ENERGY on **127.0.0.1:20001 only**. No firewall change is made and nothing listens off-host. It runs
as a dry run by default; `--apply` makes the changes.

What `--apply` does:

1. It generates a test PKI in `/etc/opengrid/certs`:
   - CA `gridlink-test-ca`, whose key is 0600 root:root;
   - server `og-gridlink` with SAN IP:127.0.0.1;
   - client `aen-ems-loopback`.

   The other keys and certificates are 0640 root:opengrid. Keys are never printed.
2. It creates `OG_MQTT_GRIDLINK_PASSWORD` in `secrets.env` when it is absent.
3. It creates MQTT user `og_gridlink`, publish-only on `og/v1/scada/instruction/#`, through
   `deploy/mosquitto/provision_grid_link_user.py`. That tool backs up the broker files, reloads mosquitto,
   and restores the backups if mosquitto is not active afterwards.
4. It writes the host override `/etc/opengrid/grid_link.toml`. og-engine reads it through
   `OG_GRID_LINK_CONFIG`, set by the drop-in `og-engine.service.d/grid-link.conf`. The override replaces
   the release `[grid_link]` table, so the release config is never edited and the enablement survives
   deploys.
5. It restarts og-engine and waits for the listener.

`--test-call` issues one 5-minute AUSTIN_ENERGY call through the Austin Energy sim's `grid_link` channel and
cancels it straight away. Outside the tolling window the core refuses the call (for example
R-CALL-OUTSIDE-WINDOW). That refusal still proves the TLS link, the outstation and the core call function end
to end.

`--disable --apply` switches the link off again by removing the drop-in and the override.
| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | false | Master switch. |
| `mqtt_username`, `mqtt_password_env` | `og_gridlink`, `OG_MQTT_GRIDLINK_PASSWORD` | Publish-only on `<root>/scada/instruction/#`. |
| `utilities[].utility_id` | | Must be in `opengrid.core.models.market.UTILITY_IDS`. |
| `.enabled` | false | Per-utility switch. |
| `.listen_host`, `.listen_port` | 127.0.0.1, required | A non-loopback host requires TLS. |
| `.allowed_peers` | required | IP/CIDR allow-list. |
| `.allowed_peer_cns` | [] | Required with TLS. |
| `.banks` | required (≤ 200) | Per-bank telemetry points, in order. |
| `.l2_targets[]` | [] (≤ 64) | `{name, bank_id \| zone}`, in order. |
| `.accept_toll_calls`, `.accept_l2` | true | Per-utility authorisation. |
| `.heartbeat_timeout_s` | 30 | Fail-safe window. |
| `.max_setpoint_kw` | required | Upper bound of CALL_SETPOINT_KW. |
| `.max_duration_min` | 90 | ≤ 90 (D-29). |
| `.status_refresh_s` | 2 | Telemetry and call-status refresh. |
| `.core_timeout_s` | 5 | Bound on each core call. |
| `.tls` | disabled | `cert_file`, `key_file`, `key_password_env`, `client_ca_file`, `ciphers`, `minimum_version`. |
| `.dnp3` | | `outstation_address` 10, `master_address` 1, `select_timeout_s` 10, `control_mode` `sbo_or_direct` \| `sbo_only`, `max_fragment_size` 2048, `max_associations` 2. |

The simulators:

- **`scada.yaml` `grid_link:`** holds `enabled`, `host`, `port`, the addresses, `heartbeat_s`,
  `targets {bank: t}`, `tls`, and `also_mqtt`. It makes `ogsim.scada` send its L2 LIMIT/BLOCK over the
  link. ESTOP stays on MQTT: it is not a grid-link point.
- **`utility_aen.yaml` `channel: grid_link`**, with `channels.grid_link: {...}`, makes the Austin Energy
  simulator issue its toll calls over the link instead of the customer API (r3.4.3,
  `ogsim.utility_aen.channels.grid_link`). The sub-table keys are `host`, `port`, `master_address` (1),
  `outstation_address` (10), `heartbeat_s` (5), `sbo` (true), `tls {ca_file, cert_file, key_file,
  server_hostname}`, `timeout_s` (5) and `status_wait_s` (5). How the channel behaves:
  - it heartbeats from first use until the runtime calls `aclose()`;
  - the EMS call id is the numeric `call_ref`, or a stable CRC-32 of it;
  - a call starts on receipt, because the link has no scheduled start;
  - a charge call (kW > 0) is sent as-is and refused by the outstation as R-CALL-CHARGE-REFUSED;
  - shortening a call to a later `end_at` is refused locally as R-GL-SHORTEN-UNSUPPORTED;
  - `status` of any call other than the link's latest reads UNKNOWN.

  The call points always show the latest call event, a cancel included.

## 8. Adding ICCP (TASE.2) later

The service, the ports and the L2 book are protocol-neutral. An ICCP transport needs four things:

1. A licensed TASE.2 client/server stack. The candidates are the same as in `protocol-adapters.md` §5.4.
2. A bilateral table that maps the roles in §4 onto data values (setpoints as `Data_Real`, executes as
   device-control `Command` objects, status as a DS transfer set).
3. A server class beside `dnp3_server.Dnp3GridLinkServer` that decodes those operations into
   `GridCommand`s and calls `GridLinkService.offer(command, peer)`, and encodes
   `GridLinkService.status()` into transfer reports.
4. Setting `protocol = "iccp"` on the utility. `protocol` is the switch the runner reads (only `dnp3` is
   accepted today).

Nothing in `service.py`, `l2.py`, `calls_port.py` or the core changes.

## 9. Tests

| Suite | Covers |
| --- | --- |
| `orchestrator/tests/unit/integrations/grid_link/test_grid_link_points.py` | Point mapping. Constants equal the contract file; roles and status encoding. |
| `.../test_grid_link_session.py` | The DNP3 application layer as pure bytes: class 0 read, IIN errors, SBO and direct operate, staging and its expiry, allow-list and range statuses, `sbo_only`, multi-fragment reads with CON/FIN. |
| `.../test_grid_link_service.py` | Validation, the toll call through the core port with an `origin=GRID_LINK` trace, core refusal codes, heartbeat-loss fail safe, L2 zone/bank folding and fail-closed limit, delivery retry, core timeout recovery, deny rate limit, config security rules, the shipped config being disabled, unknown ids. |
| `.../test_grid_link_loopback.py` | Real DNP3 on localhost between the ogsim EMS master and the outstation: the e2e toll call and cancel, heartbeat loss over the wire, L2 from the SCADA sim bridge, peer allow-list refusal, mutual TLS with CN allow-list (throw-away PKI under the test's temp dir), association limit. |
| `integration-sims/tests/test_protocols_gridlink_master.py` | The ogsim master on its own: contract constants, object decoding, SBO sequence against a tiny outstation. |
| `integration-sims/tests/test_scada_grid_link.py` | Instruction-to-control mapping, config, ESTOP stays on MQTT, and `run_scada` routing. |
| `integration-sims/tests/test_utility_aen_grid_link_channel.py` | The Austin Energy sim channel: issue, cancel and status mapped onto the point list. |

## 10. Open points

- **Event classes and unsolicited responses are not implemented.** The EMS uses integrity polls.
- **DNP3 Secure Authentication (SAv5) is not implemented.** Mutual TLS is required on every non-loopback listener instead.
- **ICCP needs a licensed TASE.2 stack** (§8).
- **Firewall and MQTT ACL changes for enabling a utility are deploy steps for the lead and owner.** Nothing is opened by default.
