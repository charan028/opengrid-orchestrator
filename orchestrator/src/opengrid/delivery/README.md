# opengrid.delivery: measured delivery verification (D-38)

**Purpose.** It checks that capacity is actually DELIVERED whenever a discharge service is called, measured from telemetry and never from grants. It covers:

- utility toll calls (D-29) and ERCOT AS deployments (`og.as_deployment`);
- operator manual discharge targets (MANUAL_TARGET trace rows).

It has one owner. The pure computation is `opengrid.core.delivery`; this package does the I/O and the live job.

## Interface (the only way other modules read delivery)

- `store.fetch_record(pool, call_id)` returns a `DeliveryRecord` or None.
  - `call_id` is the `og.as_deployment.deployment_id`, or the MANUAL_TARGET trace id.
  - Used by `opengrid.calls` for the call status (delivered kW/kWh, RAMPING/DELIVERING) and by the API.
- `store.list_records(pool, *, customer_id, utility_id, contract_id, service_type, call_kind, result, call_ids, since, until, limit)`.
- `store.fetch_live_points(pool, *, utility_id=None)` returns the `LiveDeliveryPoint`s the grid link publishes.
- `store.summary(pool, since=..., until=...)` gives compliance per contract per CT day.

**Sign convention.** Every kW value is +charge/-discharge. Energy is discharged kWh (>= 0).

## How it works

og-settle runs `job.DeliveryJob.run_once` every `[delivery].interval_s`. For each call that has started and has no final record yet, it does the following:

1. **Resolves the banks.** An obligation's banks are its reserved or granted banks; a manual target's are its hubs' banks. A bank is independently metered if it belongs to an `og.asset` SUBSTATION or is listed in `[delivery].meter_bank_ids`.
2. **Reads only the new 30 s buckets**, since `evaluated_to`, lagging `telemetry_lag_s` behind now. `series.read_slice` reads:
   - **delivered:** hub telemetry. For an obligation, the bank discharge is attributed by the obligation's share of the bank's granted kW (settlement's rule, `core.delivery.attributed_discharge_kw`). A manual target uses its own hubs' discharge.
   - **commanded:** the call's items in `RT_ALLOCATION` batches the guardian signed (verdict PASS). A vetoed batch commands 0 kW and is counted as vetoed.
   - **meter:** SCADA `REAL_POWER_KW` (GOOD) minus its pre-call baseline, next to the same banks' battery telemetry minus its baseline.
3. **Verifies the whole series** with `core.delivery.verify_delivery` (PASS/PARTIAL/FAIL, or IN_PROGRESS while running) and `corroborate_meter`.
4. **Upserts `og.delivery_record`.** The final record is traced as `DELIVERY_VERIFICATION` on stream `delivery`.
5. **Raises and clears the live alerts** `ALR-DELIVERY-RAMP-LATE`, `-SHORTFALL` and `-NONE` (`opengrid.health.delivery_rules`).
   - `ALR-DELIVERY-METER-MISMATCH` is raised when a final record is UNCORROBORATED. It clears once the same meter agrees again on a later call, or when an operator clears it with a reason (`POST /og/api/delivery/records/{call_id}/meter-mismatch/clear`).
   - On startup, the job reconciles the AT_RISK flags it set, which are held in memory only. A call still running short keeps its flag; other flags are cleared. Live alerts of calls that are no longer open are cleared.
   - While a SHORTFALL or NONE alert is open, the obligation is flagged AT_RISK through `contracts.set_obligation_at_risk` (R-DELIVERY-MEASURED-SHORTFALL). The flag is cleared when delivery recovers.
6. **Changes no dispatch** (K7, D-17). Mid-window SHORTFALL escalation remains the engine's.

## Config (`[delivery]`)

- `enabled`, `interval_s`, `bucket_s`, `telemetry_lag_s`, `lookback_s`, `baseline_s`;
- tolerances: `target_frac`, `sustain_pass_pct`, `shortfall_alert_s`, `none_alert_s`, `meter_tolerance_frac`, `meter_floor_kw`;
- `meter_bank_ids`;
- `[delivery.ramp_time_s]` per product (TOLLING, ECRS, RRS, REGUP, REGDN, NSPIN, MANUAL).

## Tests

- Unit: `tests/unit/core/test_delivery.py`, `tests/unit/delivery/`, `tests/unit/customer_api/test_delivery_routes.py`, `tests/unit/ui/test_dispatch_delivery.py`.
- Integration (server, Postgres): `tests/integration/test_delivery_db.py`.
