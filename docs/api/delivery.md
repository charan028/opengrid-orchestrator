# Delivery verification API (D-38)

These endpoints report the measured delivery of every discharge call: utility toll calls, ERCOT AS deployments and operator manual discharge targets.

- The values come from hub telemetry, aligned to the call window. They are never allocator grants.
- og-settle's delivery job (`opengrid.delivery`) writes the records every 15 s, in 30 s buckets. Values trail real time by about 30-60 s.
- Sign convention: kW is +charge/-discharge, so a 20 MW discharge is `-20000`. Energy is discharged kWh (>= 0).
- `call_id` is the `og.as_deployment.deployment_id` of a toll call or AS deployment. For a manual target, it is the MANUAL_TARGET trace id.

## Operator (viewer role)

### `GET /og/api/delivery/records`

Returns records newest first, without the per-bucket series.

| Filter | Values |
|---|---|
| `service_type` | Service type of the call |
| `contract_id` | Contract of the call |
| `call_kind` | `UTILITY_CALL`, `AS_DEPLOYMENT` or `MANUAL_TARGET` |
| `result` | `IN_PROGRESS`, `PASS`, `PARTIAL` or `FAIL` |
| `utility_id` | Utility of the call |
| `call_ids` | Comma-separated |
| `since`, `until` | Bounds on the window start |
| `limit` | Up to 1000 |

```
GET /og/api/delivery/records?service_type=REGULATED_CAPACITY&since=2026-09-27T00:00:00Z
[
  {
    "call_id": "6f0c...", "call_kind": "UTILITY_CALL", "utility_id": "AUSTIN_ENERGY",
    "service_type": "REGULATED_CAPACITY", "product": "TOLLING", "bank_ids": ["bank-sub-LZ_AEN-00"],
    "window_start": "2026-09-27T21:30:00Z", "window_end": "2026-09-27T22:00:00Z",
    "committed_kw": -20000.0, "commanded_kw_avg": -19120.4, "delivered_kw_avg": -18840.2,
    "delivered_kw_last": -19710.0, "ramp_time_s": 600.0, "time_to_target_s": 240.0,
    "sustained_pct": 98.6, "lowest_kw": -18600.0, "lowest_run_s": 30.0,
    "discharged_kwh": 9420.1, "committed_kwh": 10000.0, "result": "PASS", "reasons": [],
    "meter_status": "CORROBORATED", "meter_mismatch_frac": 0.012, "final": true,
    "sign_convention": "+charge/-discharge"
  }
]
```

### `GET /og/api/delivery/records/{call_id}`

Returns one record with its `series`. Each entry is one bucket:

| Field | Meaning |
|---|---|
| `t` | Bucket start |
| `s` | Bucket length (seconds) |
| `c` | Committed kW |
| `m` | Commanded kW (signed batches) |
| `d` | Delivered kW (`null` means no telemetry) |
| `p`, `v` | Command cycles proposed and vetoed |
| `md` | Meter change from its pre-call baseline, on metered banks |
| `bd` | Battery change from its pre-call baseline, on metered banks |

The response is 404 when there is no record yet.

### `GET /og/api/delivery/summary?days=7`

Returns, per contract and CT day, over final records:

- `calls`, `passed`, `partial`, `failed`;
- `compliance_pct` (the share of calls that passed) and `sustained_pct_avg`;
- `uncorroborated` (calls whose meter disagreed);
- `discharged_kwh` and `committed_kwh`.

## Utility (role `utility`, action `utility.read`)

- `GET /og/api/customer/v1/utility/delivery-records` lists the caller's own calls only. It takes the filters `since`, `until`, `result` and `limit`.
- `GET /og/api/customer/v1/utility/delivery-records/{call_id}` returns one of the caller's own calls with its series. Another utility's call is 404, exactly like a missing one.
- `GET /og/api/customer/v1/utility/calls/{call_id}` is the call status. It carries the measured values:
  - `delivered_kw`, `delivered_kwh`;
  - `delivery_measured`;
  - `delivery_state`: the result, or `UNMEASURED`;
  - `delivery_reasons`, `meter_status`, `delivery_as_of`;
  - `state`: `RAMPING` or `DELIVERING`, from measured kW against the call's target.

A customer (role `customer`) reads `GET /og/api/customer/delivery-records[/{call_id}]`, limited to its own contracts.

## Grid link

The DNP3 call delivered-kW point (AI 5) is served from the call status's measured `delivered_kw`. It is flagged COMM_LOST while that value is unmeasured or stale. In-process readers use `opengrid.delivery.store.fetch_live_points(pool, utility_id=...)`.
