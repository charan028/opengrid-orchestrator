-- 0050: per-call delivery verification records (D-38, 2026-09-27). Additive only.
--
-- One row per discharge call: a utility toll call or an ERCOT AS deployment (call_id = og.as_deployment.
-- deployment_id), or an operator manual discharge target (call_id = its MANUAL_TARGET trace id). Written
-- only by opengrid.delivery (og-settle's delivery job); read by the operator API, the customer API (a
-- utility's own calls), the calls status (D-33) and the grid link. Measured from telemetry, never grants.
-- Sign convention +charge/-discharge on every kW column; energy columns are discharged kWh (>= 0).
-- `series` holds the per-bucket values the job has evaluated so far ({t, c, m, d, p, v, md, bd}).

SET search_path TO og;

CREATE TABLE IF NOT EXISTS og.delivery_record (
    call_id              text PRIMARY KEY,
    call_kind            text NOT NULL CHECK (call_kind IN ('UTILITY_CALL', 'AS_DEPLOYMENT', 'MANUAL_TARGET')),
    deployment_id        uuid,
    dispatch_call_id     uuid,
    obligation_id        uuid,
    contract_id          uuid,
    customer_id          uuid,
    utility_id           text,
    service_type         text,
    product              text,
    bank_ids             text[] NOT NULL DEFAULT '{}',
    meter_bank_ids       text[] NOT NULL DEFAULT '{}',
    hub_ids              text[] NOT NULL DEFAULT '{}',
    window_start         timestamptz NOT NULL,
    window_end           timestamptz NOT NULL,
    stopped_at           timestamptz,
    committed_kw         double precision NOT NULL,
    commanded_kw_avg     double precision,
    delivered_kw_avg     double precision,
    delivered_kw_last    double precision,
    commanded_kw_last    double precision,
    ramp_time_s          double precision NOT NULL,
    time_to_target_s     double precision,
    sustained_pct        double precision,
    lowest_kw            double precision,
    lowest_at            timestamptz,
    lowest_run_s         double precision,
    discharged_kwh       double precision NOT NULL DEFAULT 0,
    committed_kwh        double precision NOT NULL DEFAULT 0,
    stale_frac           double precision NOT NULL DEFAULT 0,
    result               text NOT NULL CHECK (result IN ('IN_PROGRESS', 'PASS', 'PARTIAL', 'FAIL')),
    reasons              text[] NOT NULL DEFAULT '{}',
    meter_status         text NOT NULL DEFAULT 'NO_METER'
        CHECK (meter_status IN ('CORROBORATED', 'UNCORROBORATED', 'NO_METER', 'METER_STALE')),
    meter_mismatch_frac  double precision,
    meter_baseline_kw    double precision,
    battery_baseline_kw  double precision,
    series               jsonb NOT NULL DEFAULT '[]'::jsonb,
    evaluated_to         timestamptz,
    final                boolean NOT NULL DEFAULT false,
    trace_id             uuid,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_delivery_record_window ON og.delivery_record (window_start DESC);
CREATE INDEX IF NOT EXISTS ix_delivery_record_open ON og.delivery_record (window_end) WHERE NOT final;
CREATE INDEX IF NOT EXISTS ix_delivery_record_contract ON og.delivery_record (contract_id, window_start);
CREATE INDEX IF NOT EXISTS ix_delivery_record_utility ON og.delivery_record (utility_id, window_start);
CREATE INDEX IF NOT EXISTS ix_delivery_record_deployment ON og.delivery_record (deployment_id);
