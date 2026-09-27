# Console (UI #19) endpoints: maps, search, bulk commands, ledger, AS deployments and the utility toll, alerts, bid funnel, per-kW, LP value

The endpoints behind the operator console's map, fleet and market screens. Every example below is taken from the
fixtures the unit tests pin the response shape with (`orchestrator/tests/unit/api/fixtures/ui19/`), trimmed: lists
show their first items and `... N more`. Auth for all of them is the proxy-secret identity in
[auth-and-actions.md](auth-and-actions.md); reads need `viewer`, writes `operator`. Field-by-field parameter lists are
in the generated [reference.md](reference.md).

## Maps

### `GET /og/api/fleet/map`

Every hub with its position and live state, for the fleet map. Filters: `zone`, `bank`, `activity`
(`activity_counts` covers the filtered set). Each hub carries `hub_id, bank_id, zone, lat, lon, coord_source,
health, activity, kw, soc_kwh, soc_pct, reserve_kwh, rated_kw, home_load_kw, meter_kw, pv_kw, fault_code,
last_seen_at, serving_obligations[], can_serve_services[]`.

```
GET /og/api/fleet/map?zone=LZ_NORTH&activity=DELIVERING
```

```json
{
  "generated_at": "2026-09-26T19:35:14.531716+00:00",
  "count": 2000,
  "activity_counts": {
    "DELIVERING": 50,
    "IDLE": 1795,
    "HOME_USE": 50,
    "CHARGING": 50,
    "FAULT": 5,
    "OFFLINE": 50
  },
  "coord_sources": {
    "zone_centroid": 2000
  },
  "warnings": [],
  "hubs": [
    {
      "hub_id": "hub-00000",
      "bank_id": "bank-000",
      "zone": "LZ_NORTH",
      "lat": 32.682638,
      "lon": -96.959505,
      "coord_source": "zone_centroid",
      "health": "online",
      "activity": "DELIVERING",
      "kw": 5.0,
      "soc_kwh": 20.0,
      "soc_pct": 51.0,
      "reserve_kwh": 7.84,
      "rated_kw": 11.0,
      "home_load_kw": null,
      "meter_kw": null,
      "pv_kw": null,
      "fault_code": null,
      "last_seen_at": "2026-09-26T19:35:13.869496+00:00",
      "serving_obligations": [
        {
          "obligation_id": "...",
          "service_type": "...",
          "state": "...",
          "tier": "..."
        }
      ],
      "can_serve_services": [
        "ERCOT_ENERGY"
      ]
    },
    "... 5 more"
  ],
  "_sample_note": "hubs[] trimmed to 6 of 2000 (one per activity)"
}
```

### `GET /og/api/customers/map`

Customer sites with their latest metered reading. A metered site with no known position is returned with `lat`/`lon`
null. Positions come from `config/grid/customer_sites.json` until the customer seed carries them.

```json
{
  "count": 5,
  "consuming_count": 1,
  "sites": [
    {
      "customer_id": "00000000-0000-7000-8000-0000000000c6",
      "site_id": "site-dc-01",
      "name": "Data center (demo)",
      "service_type": "DATA_CENTER",
      "contract_services": [
        "DATA_CENTER"
      ],
      "lat": 32.948,
      "lon": -96.729,
      "coord_source": "placeholder",
      "kw": 2400.0,
      "consuming": true,
      "reading_ts": "2026-09-26T19:35:13.869496+00:00",
      "reading_quality": "GOOD",
      "has_active_contract": true
    },
    "... 4 more"
  ]
}
```

### `GET /og/api/grid/layers`

Grid context layers (zones with live load, weather zones, utility batteries, transmission lines by kV, connection
points, heat cells), only those requested. `layers` is a comma-separated list; `min_kv` filters transmission lines.

```
GET /og/api/grid/layers?layers=zones,transmission_lines&min_kv=345
```

```json
{
  "source": {
    "file": "grid_map_data.json",
    "sha256": "eba8e48c85767a29bd5c046c181a2592d53aebecf4cb4e0efb93ece7c559b94d",
    "note": "Real ERCOT/HIFLD/Tracking-the-Sun data, exported once via this script -- re-run periodi...",
    "attribution": "Transmission lines: Esri Living Atlas / HIFLD. Zone load: ERCOT public data. Storage si..."
  },
  "layers": [
    "zones",
    "... 5 more"
  ],
  "zones": [
    {
      "zone": "LZ_AEN",
      "lat": 30.267,
      "lon": -97.743,
      "weather_zones": [
        "southC"
      ],
      "load_mw": 11028.55,
      "load_ts": null,
      "live": false,
      "approximate_mapping": true
    },
    "... 7 more"
  ]
}
```

### `GET /og/api/fleet/search`

Typeahead for every id field: `{items: [{id, ...}]}` by case-insensitive prefix. `kind` is `hub` or `bank`, `q` the
prefix, `limit` the page size. Hubs carry bank, zone, live health and rated kW (the command form's setpoint hint);
banks carry their zone.

```
GET /og/api/fleet/search?kind=hub&q=hub-0004&limit=5
```

## Fleet bulk commands (two-step, sometimes three)

A manual setpoint for several hubs at once. Nothing is commanded until it is confirmed, and each hub's command is
judged by the guardian exactly like a single manual command.

### `POST /og/api/fleet/commands/bulk` (operator)

```json
{"hub_ids": ["hub-00001", "hub-00002", "hub-00003"], "p_kw_setpoint": 0.0, "reason": "feeder work"}
```

`202`: the proposal, and whether a second confirmation is needed and why:

```json
{
  "proposal_id": "fc038bc1-ac6d-4c90-b30e-845fbb777d63",
  "summary": "Set 3 hub(s) to 0.0 kW (feeder work)",
  "expires_in_s": 60.0,
  "hub_count": 3,
  "requires_double_confirm": true,
  "confirmations_required": 2,
  "reason_counts": {
    "SERVES_COMMITTED_OBLIGATION": 1,
    "HUB_FAULT": 1
  },
  "double_confirm_reasons": [
    {
      "hub_id": "hub-00000",
      "reasons": [
        "SERVES_COMMITTED_OBLIGATION"
      ],
      "obligations": [
        {
          "obligation_id": "...",
          "service_type": "...",
          "state": "...",
          "tier": "..."
        }
      ]
    },
    {
      "hub_id": "hub-00003",
      "reasons": [
        "HUB_FAULT"
      ],
      "obligations": []
    }
  ],
  "warnings": [],
  "trace_id": "78008670-83ba-49b3-9e7d-cba59050aeea"
}
```

### `POST /og/api/fleet/commands/bulk/{proposal_id}/confirm` (operator)

With `requires_double_confirm`, the first confirm only records itself (`AWAITING_SECOND_CONFIRM`); the second
executes. Execution returns per-hub outcomes (`PASS`, `VETOED`/`PARTLY_VETOED`/`TIMEOUT` from the guardian,
`NO_VERDICT` if it did not answer in time, `ERROR`). It is `200` even when some hubs were vetoed: read the counts.

First confirm:

```json
{
  "proposal_id": "fc038bc1-ac6d-4c90-b30e-845fbb777d63",
  "status": "AWAITING_SECOND_CONFIRM",
  "confirmations_required": 2,
  "confirmations_received": 1,
  "double_confirm_reasons": [
    {
      "hub_id": "hub-00000",
      "reasons": [
        "SERVES_COMMITTED_OBLIGATION"
      ],
      "obligations": [
        {
          "obligation_id": "...",
          "service_type": "...",
          "state": "...",
          "tier": "..."
        }
      ]
    },
    {
      "hub_id": "hub-00003",
      "reasons": [
        "HUB_FAULT"
      ],
      "obligations": []
    }
  ],
  "trace_id": "3aa81d40-371c-4844-a003-760f5657809e"
}
```

Second confirm (executed):

```json
{
  "proposal_id": "fc038bc1-ac6d-4c90-b30e-845fbb777d63",
  "status": "EXECUTED",
  "hub_count": 3,
  "outcome_counts": {
    "PASS": 3
  },
  "results": [
    {
      "hub_id": "hub-00000",
      "outcome": "PASS",
      "vetoed_rule_ids": [],
      "trace_id": "38fdf456-d5b5-4e50-a877-d82b5f138b84"
    },
    {
      "hub_id": "hub-00003",
      "outcome": "PASS",
      "vetoed_rule_ids": [],
      "trace_id": "0f52d1db-3de9-4de0-b257-1db77f9c3f07"
    },
    "... 1 more"
  ],
  "trace_id": "a813df3a-2e73-4acd-ae42-982d31c677f0"
}
```

## Dispatch

### `GET /og/api/dispatch/ledger`

Capacity against reservations and commitments over time, for the fleet or one bank. `level` is `fleet` (default) or
`bank` (with `id`); window defaults to 2 h back to 24 h ahead in `bucket_minutes` (default 60). Every point:
`t, capacity_kw, reserved_kw, committed_kw, uncommitted_capacity_kw, over_committed_kw, committed_by_service{}`
(plus `unallocated_committed_kw` at fleet level).

```
GET /og/api/dispatch/ledger?level=bank&id=bank-000&bucket_minutes=60
```

```json
{
  "level": "bank",
  "id": "bank-000",
  "from": "2026-09-26T17:35:15.652672+00:00",
  "to": "2026-09-27T19:35:15.652672+00:00",
  "bucket_minutes": 60,
  "hub_count": 50,
  "available_hub_count": 50,
  "labels": {
    "uncommitted_capacity_kw": "Uncommitted capacity"
  },
  "basis": "bank",
  "notes": [],
  "now": {
    "t": "2026-09-26T19:35:15.652672+00:00",
    "capacity_kw": 550.0,
    "reserved_kw": 40.0,
    "committed_kw": 40.0,
    "uncommitted_capacity_kw": 510.0,
    "over_committed_kw": 0.0,
    "committed_by_service": {
      "ERCOT_ENERGY": 40.0
    }
  },
  "timeline": [
    {
      "t": "2026-09-26T17:35:15.652672+00:00",
      "capacity_kw": 550.0,
      "reserved_kw": 0,
      "committed_kw": 0.0,
      "uncommitted_capacity_kw": 550.0,
      "over_committed_kw": 0.0,
      "committed_by_service": {}
    },
    "... 1 more"
  ],
  "children": [
    {
      "level": "hub",
      "id": "hub-00000",
      "hub_count": 1,
      "t": "2026-09-26T19:35:15.652672+00:00",
      "capacity_kw": 11.0,
      "reserved_kw": 0.8,
      "committed_kw": 0.8,
      "uncommitted_capacity_kw": 10.2,
      "over_committed_kw": 0.0,
      "committed_by_service": {
        "ERCOT_ENERGY": 0.8
      }
    },
    "... 2 more"
  ],
  "_sample_note": "timeline trimmed to 2 buckets, children to 3"
}
```

Fleet level (`children[]` holds one entry per bank):

```json
{
  "level": "fleet",
  "id": null,
  "from": "2026-09-26T17:35:15.638181+00:00",
  "to": "2026-09-27T19:35:15.638181+00:00",
  "bucket_minutes": 60,
  "hub_count": 2000,
  "available_hub_count": 1945,
  "labels": {
    "uncommitted_capacity_kw": "Uncommitted capacity"
  },
  "basis": "bank",
  "notes": [],
  "now": {
    "t": "2026-09-26T19:35:15.638181+00:00",
    "capacity_kw": 21395.0,
    "reserved_kw": 40.0,
    "committed_kw": 40.0,
    "uncommitted_capacity_kw": 21355.0,
    "over_committed_kw": 0.0,
    "committed_by_service": {
      "ERCOT_ENERGY": 40.0
    },
    "unallocated_committed_kw": 0.0
  },
  "timeline": [
    {
      "t": "2026-09-26T17:35:15.638181+00:00",
      "capacity_kw": 21395.0,
      "reserved_kw": 0,
      "committed_kw": 0.0,
      "uncommitted_capacity_kw": 21395.0,
      "over_committed_kw": 0.0,
      "committed_by_service": {},
      "unallocated_committed_kw": 0.0
    },
    "... 3 more"
  ],
  "children": [
    {
      "level": "zone",
      "id": "LZ_HOUSTON",
      "hub_count": 500,
      "t": "2026-09-26T19:35:15.638181+00:00",
      "capacity_kw": 5500.0,
      "reserved_kw": 0.0,
      "committed_kw": 0.0,
      "uncommitted_capacity_kw": 5500.0,
      "over_committed_kw": 0.0,
      "committed_by_service": {}
    },
    "... 3 more"
  ],
  "_sample_note": "timeline trimmed to 4 buckets"
}
```

### `POST /og/api/dispatch/as-deployments` (operator)

Deploys one held ERCOT_AS award now: while active, the allocator discharges it up to its committed kW (an AS award
is otherwise a 0 kW capacity hold). Traced before it takes effect (K10).

```json
{"obligation_id": "<uuid>", "duration_minutes": 30, "reason": "ERCOT deployment"}
```

| Result | When |
|---|---|
| `201` `{deployment_id, obligation_id, start_at, end_at, trace_id}` | Deployed |
| `404` | Unknown obligation |
| `409` | Not an ERCOT_AS award, not deployable now (e.g. settled), a duration over the product's cap (ECRS 60 min, Non-Spin 240 min), or `scope: "ALL"` (fleet-wide deployments are refused) |
| `422` | `obligation_id` missing |
| `403` | Not an operator |

`GET /og/api/dispatch/as-deployments` lists the active deployments; `DELETE .../{deployment_id}` ends one early.

### Utility toll call (TOLLING, decision D-29)

The Austin Energy toll (`REGULATED_CAPACITY`, variant `TOLLING`) is held at 0 kW (`R-GRANT-AS-HOLD`) until the utility
calls it, and a call is the same route with the toll's obligation:

```json
{"obligation_id": "<toll obligation>", "duration_minutes": 60, "reason": "Austin Energy call"}
```

A call discharges up to the committed kW for its duration. It is capped at the toll's 90-minute product (`409` above
it), and a second call that overlaps an active one is `409`. A fleet-wide ERCOT AS deployment never touches the toll.
The utility issues the same calls itself through [the utility customer API](utility-api.md) (D-33).

## Alerts

### `GET /og/api/alerts`

Paged alert list, newest first, with filters (`open_only`, `severity`, `rule`, `scope_kind`, `scope_ref`, `limit`,
`offset`). With `group=true` it also returns a count per rule and scope over the same filters, for the grouped view.

```json
{
  "total": 4,
  "limit": 2,
  "offset": 1,
  "alerts": [
    {
      "id": 2,
      "rule": "ALR-FEED-STALE",
      "severity": "warning",
      "summary": "ALR-FEED-STALE ERCOT:np6-905-cd",
      "opened_at": "2026-09-26T16:58:00+00:00",
      "acked_by": null,
      "scope_kind": "FEED",
      "scope_ref": "ERCOT:np6-905-cd"
    },
    {
      "id": 3,
      "rule": "ALR-PROCESS-DOWN",
      "severity": "critical",
      "summary": "ALR-PROCESS-DOWN engine",
      "opened_at": "2026-09-26T16:57:00+00:00",
      "acked_by": null,
      "scope_kind": "PROCESS",
      "scope_ref": "engine"
    }
  ]
}
```

Grouped (`group=true`):

```json
{
  "total": 4,
  "limit": 100,
  "offset": 0,
  "alerts": [
    {
      "id": 1,
      "rule": "ALR-FEED-STALE",
      "severity": "warning",
      "summary": "ALR-FEED-STALE ERCOT:np6-905-cd",
      "opened_at": "2026-09-26T16:59:00+00:00",
      "acked_by": null,
      "scope_kind": "FEED",
      "scope_ref": "ERCOT:np6-905-cd"
    },
    {
      "id": 2,
      "rule": "ALR-FEED-STALE",
      "severity": "warning",
      "summary": "ALR-FEED-STALE ERCOT:np6-905-cd",
      "opened_at": "2026-09-26T16:58:00+00:00",
      "acked_by": null,
      "scope_kind": "FEED",
      "scope_ref": "ERCOT:np6-905-cd"
    },
    "... 2 more"
  ],
  "groups": [
    {
      "rule": "ALR-FEED-STALE",
      "scope_kind": "FEED",
      "scope_ref": "ERCOT:np6-905-cd",
      "count": 2
    },
    {
      "rule": "ALR-HUB-OFFLINE",
      "scope_kind": "BANK",
      "scope_ref": "bank-007",
      "count": 1
    },
    "... 1 more"
  ]
}
```

### `POST /og/api/alerts/ack-bulk` (operator)

Acknowledges up to 500 alerts in one call, each through the same path as the single-alert ack. Acknowledging records
who saw an alert; it never clears it (clearing is `health`'s job when the condition resolves).

```json
{"alert_ids": [101, 102, 103]}
```

Per-id outcome: `acked`, `already_acked` (left as acknowledged by whoever did it first, never re-attributed) or
`not_found`; duplicate ids are answered once. `422` for an empty list or more than 500 ids, `403` for a viewer.

```json
{
  "results": [
    {
      "alert_id": 1,
      "outcome": "acked"
    },
    {
      "alert_id": 3,
      "outcome": "acked"
    },
    {
      "alert_id": 4,
      "outcome": "already_acked"
    },
    "... 1 more"
  ],
  "counts": {
    "acked": 2,
    "already_acked": 1,
    "not_found": 1
  }
}
```

## Markets and profitability

### `GET /og/api/markets/bid-funnel`

How opportunities move through `available, submitted, awarded, rejected, expired`, in total, per product and over
time, with rejection reasons. Default window: the last 7 days; `bucket` is `hour` (default up to 48 h) or `day`.

```json
{
  "from": "2026-09-19T19:35:15.680270+00:00",
  "to": "2026-09-26T19:35:15.680270+00:00",
  "bucket": "day",
  "totals": {
    "available": 6,
    "submitted": 3,
    "awarded": 3,
    "rejected": 3,
    "expired": 0
  },
  "by_product": [
    {
      "service_type": "DATA_CENTER",
      "product": "DATA_CENTER",
      "available": 1,
      "submitted": 0,
      "awarded": 0,
      "rejected": 1,
      "expired": 0
    },
    "... 2 more"
  ],
  "series": [
    {
      "bucket_start": "2026-09-26T19:00:00+00:00",
      "service_type": "DATA_CENTER",
      "product": "DATA_CENTER",
      "available": 1,
      "submitted": 0,
      "awarded": 0,
      "rejected": 1,
      "expired": 0
    },
    "... 2 more"
  ],
  "rejection_reasons": [
    {
      "reason_code": "R-GATE-REJECT",
      "count": 2,
      "by_product": {
        "ECRS": 2
      }
    },
    "... 1 more"
  ],
  "mms": null,
  "sources": {
    "available": [
      "og.opportunity",
      "... 1 more"
    ],
    "submitted": [
      "gate decisions"
    ],
    "awarded": [
      "og.obligation COMMITTED+"
    ],
    "rejected": [
      "og.opportunity/og.obligation REJECTED",
      "... 1 more"
    ]
  }
}
```

### `GET /og/api/profitability/per-kw`

$/kW-in vs $/kW-out economics (09 §4, 08 §3b/§3c) per contract, per market (`REGULATED`, `FREE`) and fleet-wide,
decimals as strings. Default period: the current settlement month in America/Chicago; `400` when `end <= start`.

```json
{
  "method_version": "per-kw-v1 (09 S4, 08 S3b/S3c)",
  "period_hours": "720",
  "hardware_view_usd_per_kw": "636.3636363636363636363636364",
  "target_payback_years": "3",
  "contracts": [
    {
      "scope_kind": "CONTRACT",
      "scope_ref": "00000000-0000-7000-8000-000000000d02",
      "market": "FREE",
      "kw_basis": "100",
      "period_hours": "720",
      "cost_in_usd_per_yr": "3832.500000000000000000000001",
      "revenue_out_usd_per_yr": "10950.00000000000000000000000",
      "wear_usd_per_yr": "243.3333333333333333333333334",
      "om_usd_per_yr": "1909.090909090909090909090908",
      "net_usd_per_yr": "4965.075757575757575757575760",
      "charging_energy_usd_per_yr": "3650.000000000000000000000001",
      "delivery_charge_usd_per_yr": "182.5000000000000000000000000",
      "demand_charge_usd_per_yr": "0E-26",
      "capacity_revenue_usd_per_yr": "0E-26",
      "energy_revenue_usd_per_yr": "10950.00000000000000000000000",
      "in_usd_per_kw_yr": "38.32500000000000000000000001",
      "out_usd_per_kw_yr": "109.50000000000000000000000",
      "net_usd_per_kw_yr": "49.6507575757575757575757576",
      "capex_usd": "63636.36363636363636363636364",
      "capex_usd_per_kw": "636.3636363636363636363636364",
      "effective_investment_usd_per_kw": "636.3636363636363636363636364",
      "payback_years": "12.81679610613527823128213735",
      "effective_payback_years": "12.81679610613527823128213735",
      "discounted_payback_years": 13,
      "npv_5y_usd": "-38810.98484848484848484848484",
      "npv_15y_usd": "10839.77272727272727272727276",
      "meets_target": false,
      "target_payback_years": "3",
      "notes": [
        "PLANNING capex/O&M: hardware view $7,000/11 kW, O&M 3%/yr (no og.asset_finance yet)"
      ]
    }
  ],
  "markets": {
    "REGULATED": {
      "scope_kind": "MARKET",
      "scope_ref": "REGULATED",
      "market": "REGULATED",
      "kw_basis": "0",
      "period_hours": "1",
      "cost_in_usd_per_yr": "0",
      "revenue_out_usd_per_yr": "0",
      "wear_usd_per_yr": "0",
      "om_usd_per_yr": "0",
      "net_usd_per_yr": "0",
      "charging_energy_usd_per_yr": "0",
      "delivery_charge_usd_per_yr": "0",
      "demand_charge_usd_per_yr": "0",
      "capacity_revenue_usd_per_yr": "0",
      "energy_revenue_usd_per_yr": "0",
      "in_usd_per_kw_yr": null,
      "out_usd_per_kw_yr": null,
      "net_usd_per_kw_yr": null,
      "capex_usd": "0",
      "capex_usd_per_kw": null,
      "effective_investment_usd_per_kw": null,
      "payback_years": null,
      "effective_payback_years": null,
      "discounted_payback_years": 0,
      "npv_5y_usd": "0",
      "npv_15y_usd": "0",
      "meets_target": null,
      "target_payback_years": "3",
      "notes": []
    },
    "FREE": {
      "scope_kind": "MARKET",
      "scope_ref": "FREE",
      "market": "FREE",
      "kw_basis": "100",
      "period_hours": "720",
      "cost_in_usd_per_yr": "3832.500000000000000000000001",
      "revenue_out_usd_per_yr": "10950.00000000000000000000000",
      "wear_usd_per_yr": "243.3333333333333333333333334",
      "om_usd_per_yr": "1909.090909090909090909090908",
      "net_usd_per_yr": "4965.075757575757575757575760",
      "charging_energy_usd_per_yr": "3650.000000000000000000000001",
      "delivery_charge_usd_per_yr": "182.5000000000000000000000000",
      "demand_charge_usd_per_yr": "0E-26",
      "capacity_revenue_usd_per_yr": "0E-26",
      "energy_revenue_usd_per_yr": "10950.00000000000000000000000",
      "in_usd_per_kw_yr": "38.32500000000000000000000001",
      "out_usd_per_kw_yr": "109.50000000000000000000000",
      "net_usd_per_kw_yr": "49.6507575757575757575757576",
      "capex_usd": "63636.36363636363636363636364",
      "capex_usd_per_kw": "636.3636363636363636363636364",
      "effective_investment_usd_per_kw": "636.3636363636363636363636364",
      "payback_years": "12.81679610613527823128213735",
      "effective_payback_years": "12.81679610613527823128213735",
      "discounted_payback_years": 13,
      "npv_5y_usd": "-38810.98484848484848484848484",
      "npv_15y_usd": "10839.77272727272727272727276",
      "meets_target": false,
      "target_payback_years": "3",
      "notes": []
    }
  },
  "fleet": {
    "scope_kind": "FLEET",
    "scope_ref": "fleet",
    "market": null,
    "kw_basis": "100",
    "period_hours": "720",
    "cost_in_usd_per_yr": "3832.500000000000000000000001",
    "revenue_out_usd_per_yr": "10950.00000000000000000000000",
    "wear_usd_per_yr": "243.3333333333333333333333334",
    "om_usd_per_yr": "1909.090909090909090909090908",
    "net_usd_per_yr": "4965.075757575757575757575760",
    "charging_energy_usd_per_yr": "3650.000000000000000000000001",
    "delivery_charge_usd_per_yr": "182.5000000000000000000000000",
    "demand_charge_usd_per_yr": "0E-26",
    "capacity_revenue_usd_per_yr": "0E-26",
    "energy_revenue_usd_per_yr": "10950.00000000000000000000000",
    "in_usd_per_kw_yr": "38.32500000000000000000000001",
    "out_usd_per_kw_yr": "109.50000000000000000000000",
    "net_usd_per_kw_yr": "49.6507575757575757575757576",
    "capex_usd": "63636.36363636363636363636364",
    "capex_usd_per_kw": "636.3636363636363636363636364",
    "effective_investment_usd_per_kw": "636.3636363636363636363636364",
    "payback_years": "12.81679610613527823128213735",
    "effective_payback_years": "12.81679610613527823128213735",
    "discounted_payback_years": 13,
    "npv_5y_usd": "-38810.98484848484848484848484",
    "npv_15y_usd": "10839.77272727272727272727276",
    "meets_target": false,
    "target_payback_years": "3",
    "notes": []
  },
  "illustrative_home_unit": {
    "scope_kind": "UNIT",
    "scope_ref": "08-S3c-home-unit",
    "market": "REGULATED",
    "kw_basis": "11",
    "period_hours": "8760",
    "cost_in_usd_per_yr": "361.49064000",
    "revenue_out_usd_per_yr": "2188.219200",
    "wear_usd_per_yr": "0.00",
    "om_usd_per_yr": "210.00",
    "net_usd_per_yr": "1616.72856000",
    "charging_energy_usd_per_yr": "361.49064000",
    "delivery_charge_usd_per_yr": "0.0",
    "demand_charge_usd_per_yr": "0",
    "capacity_revenue_usd_per_yr": "825",
    "energy_revenue_usd_per_yr": "1363.219200",
    "in_usd_per_kw_yr": "32.86278545454545454545454545",
    "out_usd_per_kw_yr": "198.9290181818181818181818182",
    "net_usd_per_kw_yr": "146.9753236363636363636363636",
    "capex_usd": "7000",
    "capex_usd_per_kw": "636.3636363636363636363636364",
    "effective_investment_usd_per_kw": "636.3636363636363636363636364",
    "payback_years": "4.329731145468228754491724944",
    "effective_payback_years": "4.329731145468228754491724944",
    "discounted_payback_years": 5,
    "npv_5y_usd": "1083.64280000",
    "npv_15y_usd": "17250.92840000",
    "meets_target": false,
    "target_payback_years": "3",
    "notes": []
  }
}
```

### `GET /og/api/profitability/lp-value`

The value the LP selector added over the rule baseline, per plan (mounted from R3).

```json
{
  "available": true,
  "plans": [
    {
      "plan_id": "00000000-0000-7000-8000-000000000000",
      "created_at": "2026-09-26T17:00:00Z",
      "lp_net_value": "1250.5000",
      "rule_net_value": "1100.2500",
      "value_added": "150.2500",
      "forgone_upside": "0.0000",
      "breakdown": {
        "energy": 120.25,
        "as_capacity": 30.0,
        "degradation": -0.0,
        "plan_index": 0
      }
    },
    "... 2 more"
  ]
}
```
