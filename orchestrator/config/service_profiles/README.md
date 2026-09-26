# Service profiles

One TOML file per service profile: the template for the per-contract `og.service_profile` and `og.pq_envelope`
rows (`migrations/0010_service_profile.sql`, `interfaces/contracts/service_profile.schema.json`,
`interfaces/contracts/pq_envelope.schema.json`). Spec: `docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md`
§1–§4 and the activation gate of `docs/orchestrator/02-architecture/03-decision-engine.md` §2.7.

| File | Profile | Status |
|---|---|---|
| `data_center.toml` | `DATA_CENTER` v1 (§4.b): firm bridging capacity in a tight PQ envelope | Defined and tested (WP-F); not yet registered as a service type |

## File layout

| Table | Contents |
|---|---|
| `[profile]` | `service_type`, `profile_ref` (`<name>@<version>`), `version`, `variant`, contract `tier` |
| `[service_profile]` | The `ServiceProfile` fields that do not depend on the contract (schema field names) |
| `[control]` | Control-law declarations the schema does not carry (loop type, integrator, feedback freshness gate) |
| `[instantiation]` | Rules that turn the contract's committed kW, site meter id and duration into per-contract numbers |
| `[eligibility]` | Parameters for the allocator's PQ eligibility filter (§5.2 step 1) and the §5.5.3 asset-state table |
| `[pq_envelope]` | The `PowerQualityEnvelope` fields (schema field names); per-contract defaults |
| `[settlement]` | Settlement lines, each naming the M&V output it is computed from |

Ids (`service_profile_id`, `contract_id`, `pq_envelope_id`, `customer_id`) are generated per contract and never
appear in a template. A contract may tighten a value; loosening one needs a new profile version (§2.7, V-27).

## How it is tested

`orchestrator/tests/unit/profiles/test_data_center_profile.py` instantiates the template for reference contracts
and checks the rows against the JSON Schemas and the core pydantic models (§2.7 stage 1), the §2.7 static
rules (stage 2), the §4.b control law, ES12's envelope, the §5.5.3 asset-state table, and the profile-level
preconditions of TS-12b (quality threshold) and TS-13a (a looser fleet-default envelope never rejects what
`DATA_CENTER` accepts). Simulation conformance, the risk-tiered replay and Tier 2 approval (stages 3–5) come
with the allocator and guardian work.
