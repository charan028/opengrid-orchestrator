-- MVP-S+ WP-A (docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md S1.5, S9.2 Agent A).
-- Additive only: no existing table/column is altered or dropped. og.contract is unchanged; a contract may
-- own many og.service_profile versions (V-27 semantics carried over verbatim -- a running event keeps its
-- bound version).
--
-- Numbering note: the spec (S9 work packages) names this file "0009_service_profile.sql", but 0009 was
-- already taken by 0009_obligation_energy_status.sql (an earlier, unrelated merge-task migration) before
-- this work package landed. Renumbered to the next free slot, 0010; asset-health/calibration (spec's
-- "0010_asset_health.sql") is renumbered to 0011 accordingly. Reported to the lead per BUILD.md's WP-A
-- instruction to "use the spec's numbering only if free; otherwise the next free ones, and tell me."

SET search_path TO og;

-- =====================================================================================================
-- 1. PowerQualityEnvelope (S2) -- per customer; a residential aggregate uses a fleet-default envelope
--    shared by all HOME contracts unless the utility's interconnection agreement sets a tighter one.
-- =====================================================================================================

CREATE TABLE og.pq_envelope (
    pq_envelope_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    customer_id         uuid NOT NULL,
    phase_config        text NOT NULL CHECK (phase_config IN ('1P','SPLIT_PHASE','3P')),
    max_phase_imbalance_pct numeric(5,2) NOT NULL DEFAULT 3.0,
    voltage_band_pct    numeric(5,2) NOT NULL DEFAULT 5.0,        -- +/- % of nominal
    ride_through_class  text NOT NULL DEFAULT 'CATEGORY_III',      -- IEEE 1547-2018 style label
    current_limit_a     numeric(10,2),                             -- NULL = no per-line cap
    current_limit_scope text CHECK (current_limit_scope IN ('PER_PHASE','SPECIFIC_LINE',NULL)),
    freq_tolerance_hz   numeric(5,3) NOT NULL DEFAULT 0.5,
    rocof_limit_hz_s    numeric(5,3),
    pf_min              numeric(4,3) NOT NULL DEFAULT 0.90,
    reactive_requirement text,                                     -- free text: e.g. "unity +/-0.02" or "volt-var per IEEE 1547"
    thd_voltage_limit_pct numeric(5,2) NOT NULL DEFAULT 5.0,        -- IEEE 519 style
    thd_current_limit_pct numeric(5,2) NOT NULL DEFAULT 5.0,
    flicker_pst_limit   numeric(5,2),                               -- NULL = not applicable
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_pq_envelope_customer ON og.pq_envelope(customer_id);

-- =====================================================================================================
-- 2. ServiceProfile (S1) -- the per-contract instance of dispatch-profile elements "control" and
--    "performance" plus the pq_envelope binding. Not a new dispatch-profile element (S1.1).
-- =====================================================================================================

CREATE TABLE og.service_profile (
    service_profile_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_id         uuid NOT NULL REFERENCES og.contract(contract_id),
    version             int  NOT NULL DEFAULT 1,
    control_primitive   text NOT NULL CHECK (control_primitive IN
        ('OPEN_LOOP_SCHEDULE','CLOSED_LOOP_REGULATION','PRICE_RESPONSE','CAPACITY_HOLD',
         'EVENT_SCHEDULE_TRACKING','MODE_ISLAND_CONTROL')),
    target_quantity     text NOT NULL CHECK (target_quantity IN
        ('KW','KVAR','LINE_CURRENT_A','PIPE_TO_SOIL_V','BANK_KVA','NET_POWER_MW','NONE')),
    target_scope        text NOT NULL CHECK (target_scope IN ('HUB','BANK','FEEDER','CORRIDOR_LINE','ADER','SITE_METER')),
    setpoint_source      text NOT NULL CHECK (setpoint_source IN
        ('PLAN','MEASURED_FEEDBACK','ISO_INSTRUCTION','PRICE_FEED','CUSTOMER_API')),
    feedback_signal_ref  text,
    response_time_s      numeric(8,2) NOT NULL,
    ramp_limit           numeric(10,3) NOT NULL,
    ramp_limit_unit      text NOT NULL DEFAULT 'kw_per_min',
    sustain_duration_s   numeric(10,2),
    accuracy_tolerance   numeric(10,4) NOT NULL,
    deadband             numeric(10,4) NOT NULL,
    priority_tier        text NOT NULL,
    mv_method            text NOT NULL,
    settlement_metric    text NOT NULL,
    pq_envelope_id       uuid NOT NULL REFERENCES og.pq_envelope(pq_envelope_id),
    failure_behaviour    text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_feedback_required CHECK (
        setpoint_source <> 'MEASURED_FEEDBACK' OR feedback_signal_ref IS NOT NULL)
);
CREATE INDEX ix_service_profile_contract ON og.service_profile(contract_id, version DESC);

-- =====================================================================================================
-- 3. Inverter PQ characterization per hub (S3), refreshed from a periodic sim/estimation job, never
--    billed directly. `asset_state` and calibration-history columns are added additively by
--    0011_asset_health.sql once this table exists.
-- =====================================================================================================

CREATE TABLE og.hub_inverter_pq (
    hub_id               text PRIMARY KEY REFERENCES og.hub(hub_id),
    phase_connection     text NOT NULL CHECK (phase_connection IN ('A','B','C','AB','BC','CA','ABC')),
    kva_rating           numeric(8,2) NOT NULL,
    pf_min_leading       numeric(4,3) NOT NULL DEFAULT 0.90,
    pf_min_lagging       numeric(4,3) NOT NULL DEFAULT 0.90,
    freq_offset_hz       numeric(6,4) NOT NULL DEFAULT 0,     -- mean steady-state offset from nominal
    freq_offset_std_hz   numeric(6,4) NOT NULL DEFAULT 0.01,
    voltage_offset_pct   numeric(6,4) NOT NULL DEFAULT 0,
    voltage_offset_std_pct numeric(6,4) NOT NULL DEFAULT 0.5,
    thd_current_pct      numeric(5,2) NOT NULL DEFAULT 3.0,
    dominant_harmonics    jsonb,                              -- e.g. {"3": {"mag_pct": 1.2, "angle_deg": 40}, "5": {...}}
    phase_angle_error_deg numeric(6,3) NOT NULL DEFAULT 0,
    response_time_ms      numeric(8,2) NOT NULL DEFAULT 200,
    ride_through_class    text NOT NULL DEFAULT 'CATEGORY_III',
    quality_score         numeric(4,3) NOT NULL DEFAULT 1.0,  -- 0..1, derived, S5.1
    last_estimated_at      timestamptz
);

-- =====================================================================================================
-- 4. Retention policy rows for the new PQ/service-profile data classes (K11 pattern: retention is
--    configurable per event class with no code change). Waveform-specific classes are added by
--    0011_asset_health.sql alongside the tables/decision_types they cover.
-- =====================================================================================================

INSERT INTO og.retention_policy (event_class, retention_days, prune_after_checkpoint) VALUES
    ('PQ_ENVELOPE_CHANGE', 1825, true),
    ('SERVICE_PROFILE_CHANGE', 1825, true)
ON CONFLICT (event_class) DO NOTHING;
