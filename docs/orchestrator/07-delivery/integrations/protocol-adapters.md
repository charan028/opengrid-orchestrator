# Protocol adapters: utility SCADA and ERCOT market submission

As of 2026-09-26

The owner decided on 2026-09-26 to build production-grade adapters for real grid protocols and for ERCOT
market submission. They sit behind two interfaces and are tested end to end against local simulators.
Moving to a real endpoint must need only credentials, certificates and the counterparty's agreed point
list. It must not need code.

Code: `orchestrator/src/opengrid/integrations/`. Simulators: `integration-sims/src/ogsim/protocols/`.
The two share no code (BUILD.md §1). The wire contracts in this document are the only coupling.

## 1. Status at a glance

| Adapter | Wire protocol in the adapter | Tested against | What "going real" needs |
| --- | --- | --- | --- |
| MQTT SCADA (default) | Existing `og/v1/scada/...` topics | Unchanged | Nothing. This is today's path. |
| DNP3 master | **Real DNP3** (IEEE 1815-2012: link, transport and application layers with CRC), over TCP or TLS | `ogsim` DNP3 outstation, real DNP3 on localhost | Utility's point list, IP/port, DNP3 addresses. TLS certificates if the utility uses DNP3 over TLS. |
| IEEE 2030.5 client | **Real 2030.5** resources (XML, `application/sep+xml`) over HTTPS with a client certificate | `ogsim` 2030.5 server | The utility's server URL, our device certificate (SERCA chain), and the LFDI/SFDI-to-bank mapping. |
| ICCP / TASE.2 | Bilateral-table mapping is real. **The transport is a simulator transport, not MMS.** | `ogsim` ICCP control-centre simulator | A **vendor TASE.2/MMS stack** bound into `MmsIccpTransport`, the bilateral table, and IEC 62351 certificates. See §5.4. |
| ERCOT MMS (QSE submission) | **Real EWS SOAP envelopes** with WS-Security X.509 signing (exclusive C14N) | `ogsim` MMS simulator, which verifies the signature | QSE registration, the ERCOT-issued client certificate, MOTE market trials, and confirmation of the spellings marked UNCONFIRMED in §6.2. |

Default behaviour is unchanged. With no `[integrations]` table, SCADA stays on MQTT and nothing is ever
submitted to a market.

## 2. Interfaces and selection

### 2.1 The two seams (`opengrid.integrations.interfaces`)

- **`ScadaSource`**: `run(sink)` (long-lived, reconnects itself), `poll_once(sink)`, `close()` and
  `backend`. Every backend emits the existing wire models `ScadaBankSignal` and
  `ScadaUtilityInstruction` (`opengrid.core.models.mqtt`) into a **`ScadaSink`**. Downstream consumers
  (fleet twin, guardian G-03/G-15, health) never learn which protocol delivered a reading.
- **`MarketSubmission`**: `submit_energy_offer`, `submit_as_offer`, `submit_three_part_offer`, `cancel`,
  `fetch_awards` and `fetch_dispatch_instructions`. It uses market-neutral pydantic types
  (`EnergyOffer`, `AsOffer`, `ThreePartSupplyOffer`, `SubmissionReceipt`, `Award`,
  `DispatchInstruction`). Offer curves are validated as monotonic, with at most 10 points for energy
  and 5 blocks for AS.

### 2.2 Sinks (`opengrid.integrations.sinks`)

- **`MqttBridgeSink`** (recommended in production). It republishes on the existing topics
  `<root>/scada/<bank_id>` (QoS 0) and `<root>/scada/instruction/<bank_id>` (QoS 1). Each message is
  validated against the same `interfaces/mqtt` schemas first. The engine, the guardian's independent L2
  instruction port and health keep their own subscriptions, so **guardian independence (K5) is
  preserved**.
- **`FleetScadaSink`**: in-process delivery into `opengrid.fleet`. The guardian does not see
  instructions on this path, so it is only acceptable together with a bridge for instructions.

### 2.3 Configuration (`config/orchestrator.toml`)

```toml
[integrations.scada]
backend = "mqtt"              # "mqtt" (default) | "dnp3" | "iccp" | "ieee2030_5"
sink = "mqtt_bridge"          # "mqtt_bridge" (default) | "fleet"

[integrations.scada.dnp3]     # only read when backend = "dnp3"
integrity_poll_s = 60         # Class 0/1/2/3
event_poll_s = 2              # Class 1/2/3
response_timeout_s = 5
breaker_open_blocks = true    # an open bank breaker is a utility BLOCK (fail safe)
deliver_bad_quality = false   # see 3.4
[[integrations.scada.dnp3.outstations]]
name = "sub-north"
host = "<utility RTU/FEP address>"
port = 20000
master_address = 1
outstation_address = 10
banks = ["bank-000", "bank-001"]   # standard layout, or give an explicit `points` table
[integrations.scada.dnp3.outstations.tls]
enabled = true
ca_file = "/etc/opengrid/certs/utility-dnp3-ca.pem"
cert_file = "/etc/opengrid/certs/og-dnp3.pem"
key_file = "/etc/opengrid/certs/og-dnp3.key"

[integrations.market]
backend = "none"              # "none" (default) | "ercot_mms"
enabled = false               # must ALSO be true before anything is submitted

[integrations.market.ercot_mms]
endpoint = "https://misapi.ercot.com/NodalAPI/EWS/"
qse_code = "<QSE short name>"
user_id = "<MIS user of the certificate>"
esr_resources = ["<ESR resource name>"]
[integrations.market.ercot_mms.tls]
enabled = true
ca_file = "/etc/opengrid/certs/ercot-ca.pem"
cert_file = "/etc/opengrid/certs/qse-ews.pem"
key_file = "/etc/opengrid/certs/qse-ews.key"
key_password_env = "ERCOT_EWS_KEY_PASSWORD"   # a secret: referenced by env-var name only

[integrations.market.contracts]   # which contract carries each resource's awards
energy = { "<ESR resource name>" = "<ERCOT_ENERGY contract uuid>" }
ancillary = { "<ESR resource name>:ECRS" = "<ERCOT_AS contract uuid>" }
```

Certificates and keys are files under `/etc/opengrid/certs/` (mode 640, never in git). Key passphrases
are secrets and are referenced by env-var name only.

## 3. DNP3 (`integrations/scada_dnp3`)

### 3.1 Implementation choice: our own minimal DNP3 subset, no DNP3 library

We checked the PyPI candidates on Python 3.13:

- `dnp3-python` and `pydnp3` have no Python 3.13 wheels.
- `nfm-dnp3` is master-only, untyped and synchronous.
- `dnp3py` 0.4 (MIT) installs and works, but the owner did not approve it (young and unvetted).

So DNP3 is **implemented in-house** as the minimal subset this adapter needs (`scada_dnp3/codec.py`,
about 400 lines):

- the **data link layer**: `0x05 0x64` frames with CRC-16/DNP (reflected polynomial 0xA6BC,
  complemented, little-endian) over the header and every 16-byte block, and resynchronisation on bad
  CRCs;
- the **transport function**: FIR/FIN and a 6-bit sequence, 249-byte segments, and reassembly that drops
  gaps;
- the **application layer**: READ of Class 0/1/2/3 (g60, qualifier 0x06), CONFIRM, and response
  parsing for g1v1/g1v2, g2v1-3, g30v1-6, g32v1-8, and the g50/g51 time objects. It handles qualifiers
  0x00/0x01/0x07/0x08/0x17/0x28. An unknown object stops the parse with an error rather than a guess.

`channel.py` is the async master: request/response, multi-fragment responses with application CONFIRM,
confirmation of unsolicited responses, and link ACK of confirmed user data.

The ogsim outstation has its **own, independent** outstation codec (`ogsim/protocols/dnp3_wire.py`),
so the end-to-end tests run two separate implementations of IEEE 1815 against each other. Both sides
check the CRC against the IEEE 1815 reference frame (`05 64 05 C0 01 00 00 04 -> E9 21`).

**No DNP3 package is a dependency.** The only packages added are `defusedxml` and `lxml`, listed in
§9.

### 3.2 Point layout "opengrid-dnp3-v1"

This is the layout OpenGrid proposes to utilities, and the one the `ogsim` outstation serves. For bank
position `b`:

| Type | Index | Signal | Scale (units per count) |
| --- | --- | --- | --- |
| AI (g30v1, events g32v1) | 16b+0 | APPARENT_POWER_KVA | 0.1 kVA |
| | 16b+1 | REAL_POWER_KW | 0.1 kW |
| | 16b+2 | VOLTAGE_PU | 0.0001 pu |
| | 16b+3 | CURRENT_A | 0.1 A |
| | 16b+4..6 | VOLTAGE_{A,B,C}_PU | 0.0001 pu |
| | 16b+7..9 | CURRENT_A_PHASE_{A,B,C} | 0.1 A |
| | 16b+10 | FREQUENCY_HZ | 0.001 Hz |
| | 16b+11, 12 | THD_V_PCT, THD_I_PCT | 0.01 % |
| | 16b+13 | UTILITY_LIMIT_KW | 0.1 kW |
| BI (g1v2, events g2v1) | 8b+0 | COMM_OK | |
| | 8b+1 | BREAKER_CLOSED | |
| | 8b+2 | UTILITY_BLOCK | |
| | 8b+3 | UTILITY_ESTOP | |
| | 8b+4 | UTILITY_LIMIT_ACTIVE | |

A utility with its own point list configures `points` explicitly, giving index, bank, role, scale and
offset per point. The outstation feeds from exactly the `(topic, message)` pairs `ogsim.scada` publishes
on MQTT (`apply_scada_tick`), so the DNP3 view and the MQTT view of a bank carry the same numbers.

### 3.3 Polling and failure

- An integrity poll (Class 1, 2, 3, 0) runs on connect and every `integrity_poll_s`.
- An event poll (Class 1, 2, 3) runs every `event_poll_s`.
- After each successful poll, every mapped value is delivered with `ts = now`, because a
  report-by-exception value is still current while the association is healthy.
- A failed poll delivers **nothing**. The association is closed and retried with capped exponential
  backoff. The existing staleness handling then applies, exactly as when MQTT SCADA goes silent.

### 3.4 Quality and instructions

- **Quality.** DNP3 flags map to signal quality:
  - COMM_LOST, or the bank's COMM_OK input off, becomes `comm_fail`.
  - RESTART (never updated) becomes `missing`.
  - Not ONLINE becomes `comm_fail`.
  - OVER_RANGE or REFERENCE_ERR becomes `out_of_range`.
- **Withholding.** Readings of quality `missing`, `comm_fail` or `stale` are **withheld** by default.
  Today's consumers (the fleet twin, and guardian G-03 through `og.feed_obs`) use `value` without looking
  at `quality`. Delivering a zero with bad quality could read as an empty bank. Section 8 has the request
  to the owners.
- **Instructions.** Utility instruction points become instructions **on change only**:
  - Precedence is ESTOP > BLOCK > LIMIT.
  - A LIMIT asserted with an unusable value is taken as 0 kW (fail closed).
  - A lifted level is sent as the previous kind with `expires_at == issued_at`. This is the convention
    the fleet and the guardian already honour.
  - Ids are UUIDv5, so re-reading the same level after a reconnect gives the same id.

### 3.5 Going real

- Agree the point list. Either the standard layout above, or the utility's list entered as `points`.
- Set the DNP3 addresses and the RTU or front-end address.
- If the utility requires it, set up DNP3 over TLS (IEC 62351-3) using `tls.*`.
- **Not supported:** DNP3 Secure Authentication (SAv5/SAv6). If the utility requires SA, that needs a
  stack with SA (a vendor library) behind `Dnp3MasterChannel`.
- The adapter only reads. It never operates utility controls.

## 4. IEEE 2030.5 (`integrations/ieee2030_5`)

### 4.1 Resource walk and mapping

The orchestrator is a 2030.5 **client** (an aggregator in CSIP terms). Each poll walks this resource
chain:

`/dcap` → EndDeviceList → EndDevice → FunctionSetAssignments → DERProgramList (by primacy) →
DERControlList and DefaultDERControl

The EndDevice is matched to a bank by **LFDI or SFDI**. The client then maps the controls:

- The highest-primacy program with an **active** event wins. Within that program, the newest event
  wins. If there is no active event, the program's DefaultDERControl applies as a standing limit.
- `opModEnergize=false` becomes ESTOP.
- `opModConnect=false` becomes BLOCK.
- `opModMaxLimW` (% of setMaxW, hundredths), `opModFixedW` (signed %) and `opModTargetW` (W) become a
  LIMIT. The most restrictive one wins. Percentages use the bank's configured `rated_kw`.
- The event interval end becomes `expires_at`. A cancelled or finished event lifts the instruction.
- When a control carries `replyTo` and a non-zero `responseRequired`, the client POSTs a
  **DERControlResponse**: status 1 (received), then 2 (started).
- If the utility's server is unreachable, the last instructions stay in force. A utility limit is never
  lifted because of our own comms failure.

This backend delivers instructions only. Bank loading keeps arriving over MQTT or DNP3.

### 4.2 Going real

- The utility's server URL.
- Our device certificate chaining to the utility's SERCA. The LFDI is SHA-256 of the DER certificate,
  first 160 bits (`identity.lfdi_from_certificate`); `sfdi_from_lfdi` derives the SFDI with its check
  digit.
- TLS settings: `tls.enabled = true`, `tls.minimum_version = "TLSv1_2"`, and
  `tls.ciphers = "ECDHE-ECDSA-AES128-CCM8"` (the 2030.5 mandatory suite, available in OpenSSL).
- The bank-to-LFDI/SFDI mapping with each bank's `rated_kw`.
- Our aggregator LFDI (`client_lfdi`).
- Not implemented: subscription/notification (we poll), and metering mirror uploads.

## 5. ICCP / TASE.2 (`integrations/scada_iccp`)

### 5.1 What is real and what is not

The **bilateral-table model and the data mapping are production code.** They cover association
parameters, domains, AP titles, and per-value name, scope, type and role, with TASE.2 typing and quality
semantics. **The transport used in tests is not ICCP on the wire.** Real TASE.2 runs over ISO/OSI MMS
(ISO 9506) on RFC 1006 (TCP 102), and in practice with IEC 62351-4/-6 security. No maintained, certified
Python TASE.2 client exists. `MmsIccpTransport` is the production seat, and it **raises
`NotImplementedError`** until a vendor stack is bound, so the gap cannot be silent.

### 5.2 Mapping

- **Reals** (`Data_Real`, `Data_RealQ`, `Data_RealQTimeTag`) are IEEE float32 engineering values. The
  optional `scale` converts units, for example MVA to kVA with 1000.
- **States** (`Data_State*`) are 2-bit values: 0 BETWEEN, 1 OFF, 2 ON, 3 INVALID. BETWEEN and INVALID
  are treated as not online.
- **Validity** maps as follows: VALID becomes good; HELD and SUSPECT become stale (withheld); NOTVALID
  becomes comm_fail (withheld).
- **Naming.** The proposed data-value naming is `<BANK>_<SUFFIX>`: KVA, KW, VPU, AMP, VA_PU, VB_PU,
  VC_PU, IA, IB, IC, HZ, THDV, THDI and LIMKW for reals; COMM, BRKR, BLOCK, ESTOP and LIMACT for states.
  An example is `BANK_000_KVA`. Values not in our bilateral table are ignored and never guessed.
- **Operation.** The adapter associates, defines and starts a DS transfer set over every agreed value,
  and applies each transfer report. If there is no report for 3× the interval, or the association is
  refused or dropped, it concludes and reconnects with backoff.

### 5.3 Simulator transport (`SimTcpIccpTransport` and `ogsim.protocols.iccp_server`)

Framing: a 4-byte big-endian length, then a UTF-8 JSON object with an `op` field, at most 1 MiB.

| Client → server | Server → client |
| --- | --- |
| `associate` {bilateral_table_id, tase2_version, local_domain, remote_domain, calling_ap_title, called_ap_title} | `associate_ok`, or `error` {code: association-rejected} |
| `read` {names} | `read_response` {values, errors: {name: object-non-existent \| object-access-denied}} |
| `start_transfer_set` {dataset, names, interval_s, rbe} | `transfer_set_started`, then periodic `transfer_report` {values} |
| `conclude` | `conclude_ok` |

A value is `{name, type, value, quality: {validity, current_source}}`.

### 5.4 Going real

1. Buy or license a TASE.2 client stack. Candidates include the Triangle MicroWorks TASE.2/ICCP SCL,
   the SISCO ICCP-TASE.2 toolkit, or the utility EMS vendor's stack. The open-source MMS libraries
   (libiec61850 for 61850) do not provide certified TASE.2 client services.
2. Implement `MmsIccpTransport` on that stack. This means five methods: associate, read, start transfer
   set, next report and conclude.
3. Fill `bilateral_table` with the utility's agreed table: ids, domains, AP titles and data values.
4. Get IEC 62351 certificates for the association.

The adapter, mapping and tests stay unchanged.

## 6. ERCOT MMS submission (`integrations/ercot_mms`)

### 6.1 Operations

| Operation | Verb / Noun | SOAPAction |
| --- | --- | --- |
| Energy offer, ESR bid/offer curve, three-part supply offer, AS offer | `create BidSet` | `http://www.ercot.com/Nodal/MarketTransactions` |
| Cancel | `cancel BidSet` | MarketTransactions |
| Awards | `get AwardSet` (Request: MarketType, TradingDate) | `http://www.ercot.com/Nodal/MarketInfo` |
| Verbal dispatch instructions | `get VDIs`; acknowledge with `change VDIs` | MarketInfo / MarketTransactions |

**Safety rules:**

- The client is OFF unless `enabled = true`.
- A submission is POSTed **exactly once**. Timeouts and 5xx responses become `ERROR` receipts and are
  never resent, because a resend could double-offer. The state is resolved through awards and
  notifications.
- GETs retry at most twice.
- ReplyCode `OK` means ERCOT accepted the BidSet for processing (status SUBMITTED). MMS validates
  asynchronously and reports results in BidSet notifications.
- ESRs are refused three-part offers, because under RTC+B an ESR submits a single bid/offer curve.

### 6.2 Message shapes and sources

These come from ERCOT's public developer portal (developer.ercot.com/applications/ews): the Appendix D
annotated SOAP message, Appendix E examples, the TPO, ASO, COP, AwardSet, VDI and Error Handling pages,
and market notice M-C110625-01 for endpoints.

- **Envelope:** SOAP 1.1. `RequestMessage` is in namespace
  `http://www.ercot.com/schema/2007-06/nodal/ews/message` and contains:
  - `Header`, with Verb, Noun, ReplayDetection {Nonce, Created}, Revision, Source = QSE, UserID and
    MessageID;
  - `Request`;
  - `Payload`, holding a `BidSet` in `http://www.ercot.com/schema/2007-06/nodal/ews`.
- **Replies:** `ResponseMessage/Reply/ReplyCode` = OK, ERROR or FATAL.
- **ThreePartOffer:**
  - `StartupCost` (hot, intermediate, cold);
  - `MinimumEnergy/cost`;
  - `EnergyOfferCurve/CurveData` (xvalue MW, y1value $/MWh, at most 10 points).
- **ASOffer:**
  - `asType` (e.g. `REGUP-RRS-ONNS`, `REGDN`);
  - `ASPriceCurve/OnLineReserves` rows with `xvalue`, price columns (REGUP, RRSPF, RRSFF, RRSUF, ECRS,
    ONNS) and `block`.
- **AwardSet:**
  - `AwardedEnergyOffer` (resource, startTime, endTime, awardedMWh);
  - `AwardedAS` (asType, awardedMW, mcpc).
- **VDI:** `Details/instructionType`, `notificationTime`, `vdiRefNum`.
- **Times** are sent in Central Prevailing Time with an explicit offset.

**UNCONFIRMED. These are config values, and each must be checked against ERCOT's EIS v2.0 XSD zip and in
MOTE:**

1. The XML element for the RTC+B **ESR Energy Bid/Offer Curve** (`esr_curve_element`, default
   `EnergyBidOfferCurve`).
2. The RRS column an ESR offers (`rrs_price_tag`, default `RRSFF`).
3. The row element for a REGDN price curve (sent as `OnLineReserves`).
4. The VDI acknowledgement element (`acknowledged`).
5. Whether the gateway accepts RSA-SHA256 (`signing_algorithm`). Appendix D shows RSA-SHA1/SHA1.
6. The production VDI `instructionType` values that mean an AS deployment or recall (`vdi_type_map`).
   The defaults `DEPLOY_AS` and `RECALL_AS` are the simulator's.
7. Whether the MOTE URL moved to `/NodalAPI/EWS/`.

### 6.3 Security

- Mutual TLS uses the QSE's ERCOT-issued client certificate. The trust anchor is "ERCOT CA", or "ERCOT
  TEST CA" for MOTE.
- **WS-Security** (`signing.py`) adds a `wsse:BinarySecurityToken` (X509v3) and a `wsu:Timestamp`, plus a
  `ds:Signature` over the Body and the Timestamp. It uses exclusive C14N (lxml) and RSA PKCS#1 v1.5
  (`cryptography`).
- The simulator verifies the same signature. It rejects missing signatures, altered bodies and
  signatures that do not cover the Body. The tests exercise the signed path end to end.

### 6.4 Endpoints

| Environment | Endpoint |
| --- | --- |
| Production (internet, after 2025-12-05) | `https://misapi.ercot.com/NodalAPI/EWS/` |
| Private WAN | `https://api.wan.ercot.com/NodalAPI/EWS/` |
| MOTE (market trials) | `https://testmisapi.ercot.com/2007-08/Nodal/eEDS/EWS/` |

ERCOT's public REST API (api.ercot.com) is public data only. Submissions remain on EWS.

### 6.5 Awards and instructions into the existing intake (`intake.py`)

- **Awards** become opportunities through **`opengrid.contracts.admit_priced`**, the same admission path
  as every other opportunity. That keeps product rules, activation gates and K13 intact.
  - `[integrations.market.contracts]` names the contract per resource (energy) and per resource and
    service (AS).
  - MW becomes kW (× 1000), and the award price becomes `value_per_mwh`.
  - An ESR **charging** award (negative MW) is not an obligation and is skipped for the charging model.
  - One rejection never blocks the rest.
- **AS deployments** (VDIs mapped to `AS_DEPLOYMENT`) become `og.as_deployment` rows (migration 0020),
  which already release held ERCOT_AS awards.
  - The simulator path uses `source = 'MARKET_SIM'`, which the CHECK allows.
  - **A real-ERCOT source value needs one additive migration at go-live** (a number to claim from the
    lead then). None is needed now.

### 6.6 Simulator (`ogsim.protocols.ercot_mms`)

- **Endpoint:** `POST /ews/`.
- **Validation:** it rejects an unknown QSE or resource, a reused nonce (replay), non-monotonic curves
  and an empty BidSet. With `require_signature`, it also rejects missing or invalid WS-Security.
- **Clearing** is deterministic against an admin-set SPP and per-service MCPCs:
  - an energy curve clears the largest MW priced at or below the SPP;
  - an ESR bid clears when its price is at or above the SPP;
  - an AS curve clears the largest MW priced at or below the MCPC.
- **Admin API:** `PUT /admin/prices`, `POST /admin/vdis`, `GET /admin/submissions`.

### 6.7 Going real

1. **QSE registration** with ERCOT (Registration and Qualification), with the ESR resources registered
   under the QSE.
2. **Digital certificates.** The QSE's User Security Administrator (USA) issues the EWS/API certificate
   through MPIM ("Digital Certificate User Guide"). Separate certificates exist for MOTE.
3. **MOTE market trials.** Confirm the seven UNCONFIRMED items in §6.2 and the signature algorithm.
4. **Configuration.** Set `endpoint`, `qse_code`, `user_id`, `tls.*`, `esr_resources`, the production
   `vdi_type_map` and `contracts`, then `enabled = true`.
5. **Migration.** Add an `og.as_deployment.source` value for ERCOT (additive).
6. **Deadlines.** DAM bids and offers are due by **10:00 CPT** the day before. Submission scheduling
   belongs to the selector/gate owner.

## 7. Tests

All tests run in-process. There is no shared broker, and the only network use is localhost ports that
the OS assigns (≥ 18000, asserted).

| Suite | Covers |
| --- | --- |
| `orchestrator/tests/unit/integrations/test_dnp3.py` | Real DNP3 over TCP against the ogsim outstation: scaling, per-phase values, events, instruction levels, comm-lost and breaker, withheld quality, the MQTT-tick feed, reconnect after an outstation restart, timeouts, and the CRC reference frame. |
| `.../test_ieee2030_5.py` | Resource walk, LIMIT/ESTOP/BLOCK mapping, scheduled vs active events, DefaultDERControl, DERControlResponse, XML entity rejection, LFDI/SFDI. |
| `.../test_iccp.py` | Association accept and reject, access denial, reads, transfer-set reports, reconnect, the MMS seat failing loudly, bilateral validation. |
| `.../test_ercot_mms.py` | Signed submission to award to intake, an unsigned request rejected, a tampered body rejected, validation errors, cancel, replay, ESR charging, the VDI to `as_deployment` flow, no-resend on transport failure. |
| `.../test_common.py` | Default MQTT with no market, config selection, the market staying OFF until enabled, bridge topics and schema validation, fleet sink, instruction tracker, fail-closed limit, TLS context. |
| `integration-sims/tests/test_protocols_*.py` | Each simulator on its own, with no opengrid import. |

## 8. Wiring and requests to other owners

The adapters are built and tested. They go live only when these lines are added by the owners of the
files concerned.

1. **Live-path (deploy):** add a new process, `og-scada-gw`, recommended over running inside og-engine:

   ```python
   from opengrid.integrations.config import build_scada_source, load_integrations_settings
   from opengrid.integrations.sinks import MqttBridgeSink

   settings = load_integrations_settings(cfg.get("integrations"))
   source = build_scada_source(settings.scada)
   if source.backend != "mqtt":
       async with build_client(cfg, username="og_scadagw", password=...) as client:
           await source.run(MqttBridgeSink(client, cfg.mqtt_topic_root))
   ```

   This needs an MQTT user `og_scadagw` with **publish** on `og/v1/scada/#`, plus a systemd unit. With
   `backend = "mqtt"` nothing starts.
2. **Market gate owner (optimizer/engine):** after DAM clearing, and every few minutes for VDIs:

   ```python
   market = build_market_submission(settings.market)          # None unless enabled
   if market is not None:
       awards = await market.fetch_awards(trading_date)
       await apply_awards(awards, settings.market.contracts, contracts.admit_priced)
       for row in deployments_for(await market.fetch_dispatch_instructions(since)):
           await store.insert_as_deployment(obligation_id=row.obligation_id, start_at=row.start_at,
                                            end_at=row.end_at, requested_by=row.requested_by, reason=row.reason)
   ```

   The offers themselves (`EnergyOffer`/`AsOffer`) are built from the selector's plan. What to offer,
   and the ledger reservation before offering (K3/K13), belong to the optimizer. The adapter only
   transports.
3. **API owner:** `ApiStore.insert_as_deployment` hard-codes `source = 'OPERATOR'`. Add a
   `source: str = "OPERATOR"` keyword so market-driven rows carry `MARKET_SIM` (later `ERCOT`).
4. **Safety and fleet owners:** guardian G-03 (`guardian/repo.py`, the `og.feed_obs` read) and
   `fleet.capability()` use SCADA `value` without checking `quality`. The adapters withhold bad-quality
   readings for this reason. Filter on `quality = 'good'` (or treat anything else as missing) so that
   `deliver_bad_quality = true` becomes safe.

## 9. Dependencies added

The live-path agent installs these on the server; they are pinned in `pyproject.toml`.

| Package | Pin | Licence | Where | Why |
| --- | --- | --- | --- | --- |
| `defusedxml` | `~=0.7.1` | PSF-2.0 | orchestrator, ogsim | Hardened parsing of every inbound XML document (2030.5 resources, EWS replies, SOAP requests in the simulator). It rejects DTDs and entities. |
| `lxml` | `~=6.1` | BSD-3-Clause | orchestrator, ogsim | Exclusive XML Canonicalization 1.0 for WS-Security signing (ERCOT EWS), and building our own SOAP envelopes. Untrusted XML is never parsed with lxml in the orchestrator. |
| `types-defusedxml` | `~=0.7.0` | Apache-2.0 | orchestrator dev | mypy stubs. |

DNP3 needs no package (§3.1). 2030.5 uses the existing `httpx`, and signing uses the existing
`cryptography`.

## Open points

- The seven UNCONFIRMED ERCOT spellings and parameters in §6.2, to settle in MOTE.
- DNP3 Secure Authentication is not supported (a TLS transport is).
- The ICCP production transport needs a vendor TASE.2 stack (§5.4).
- 2030.5 subscription/notification is not implemented; the client polls.
