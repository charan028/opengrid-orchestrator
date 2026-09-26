-- MVP-S+ WP-A (docs/orchestrator/07-delivery/06-service-profiles-and-power-quality.md S8.5, S9.2 Agent A).
-- Additive only. Renumbered from the spec's "0010_asset_health.sql" to 0011 because 0009 (the spec's
-- number for the service-profile/PQ migration) was already taken -- see 0010_service_profile.sql's header
-- note. Depends on 0010_service_profile.sql (og.hub_inverter_pq) and 0001_init.sql (og.hub, og.command_batch,
-- og.trace).

SET search_path TO og;

-- =====================================================================================================
-- 1. Asset-health state on the existing per-hub inverter characterization row (S5.5.2).
-- =====================================================================================================

ALTER TABLE og.hub_inverter_pq
    ADD COLUMN asset_state text NOT NULL DEFAULT 'OK'
        CHECK (asset_state IN ('OK','WATCH','DEGRADED','QUARANTINED','AWAITING_REPLACEMENT','RECOMMISSIONING')),
    ADD COLUMN asset_state_since timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN consecutive_correctable_drifts int NOT NULL DEFAULT 0,
    ADD COLUMN last_recalibration_at timestamptz;

-- =====================================================================================================
-- 2. Measured waveform summaries (S6.5), one row per hub per telemetry period.
-- =====================================================================================================

CREATE TABLE og.pq_waveform_summary (
    hub_id              text NOT NULL REFERENCES og.hub(hub_id),
    ts                  timestamptz NOT NULL,
    v_rms_a numeric(8,3), v_rms_b numeric(8,3), v_rms_c numeric(8,3),
    i_rms_a numeric(8,3), i_rms_b numeric(8,3), i_rms_c numeric(8,3),
    freq_hz             numeric(7,4),
    pf_a numeric(4,3), pf_b numeric(4,3), pf_c numeric(4,3),
    thd_v_pct_a numeric(5,2), thd_v_pct_b numeric(5,2), thd_v_pct_c numeric(5,2),
    thd_i_pct_a numeric(5,2), thd_i_pct_b numeric(5,2), thd_i_pct_c numeric(5,2),
    phase_angle_deg_a numeric(6,2), phase_angle_deg_b numeric(6,2), phase_angle_deg_c numeric(6,2),
    harmonics_v         jsonb,   -- {"2": {"mag_pct":..,"angle_deg":..}, ..., "50": {...}}
    harmonics_i         jsonb,
    sync_source         text CHECK (sync_source IN ('ptp','gps','ntp_disciplined')),
    sync_quality_ns     numeric(10,1),
    PRIMARY KEY (hub_id, ts)
);

-- Raw waveform captures are blob-stored; this indexes them.
CREATE TABLE og.pq_waveform_raw_index (
    capture_id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hub_id              text NOT NULL REFERENCES og.hub(hub_id),
    ts                  timestamptz NOT NULL,
    trigger_reason      text NOT NULL CHECK (trigger_reason IN
        ('PQ_DEVIATION','API_REQUEST','ROTATING_AUDIT','CALIBRATION_VERIFICATION')),
    blob_ref            text NOT NULL,
    channels            int NOT NULL,
    sample_rate_hz      numeric(8,2) NOT NULL DEFAULT 7680,
    cycles              int NOT NULL DEFAULT 10,
    retain_until        timestamptz,   -- NULL = default 30-day TTL; set when evidentiary (K11-style retention class)
    created_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_pq_waveform_raw_index_hub ON og.pq_waveform_raw_index(hub_id, ts DESC);

-- =====================================================================================================
-- 3. Calibration, maintenance work orders and asset events (S5.5.4-7).
-- =====================================================================================================

CREATE TABLE og.calibration_attempt (
    calibration_id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hub_id                      text NOT NULL REFERENCES og.hub(hub_id),
    requested_at                timestamptz NOT NULL DEFAULT now(),
    reference_phase_deg         numeric(7,3) NOT NULL,
    reference_freq_hz           numeric(7,4) NOT NULL,
    reference_amplitude_v       numeric(8,2) NOT NULL,
    measured_offset_freq_hz     numeric(6,4),
    measured_offset_voltage_pct numeric(6,4),
    measured_offset_phase_deg   numeric(6,3),
    correction_freq_hz          numeric(6,4),
    correction_voltage_pct      numeric(6,4),
    correction_phase_deg        numeric(6,3),
    command_batch_id            uuid REFERENCES og.command_batch(command_batch_id),
    outcome                     text NOT NULL DEFAULT 'PENDING' CHECK (outcome IN
        ('PENDING','IMPROVED','CORRECTED','NO_CHANGE','WORSE_ROLLED_BACK','FAILED_NO_ACK')),
    verified_at                 timestamptz,
    created_at                  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_calibration_attempt_hub ON og.calibration_attempt(hub_id, requested_at DESC);

CREATE TABLE og.maintenance_work_order (
    work_order_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hub_id            text NOT NULL REFERENCES og.hub(hub_id),
    severity          text NOT NULL CHECK (severity IN ('LOW','MEDIUM','HIGH','URGENT')),
    evidence          jsonb NOT NULL,   -- waveform-summary IDs, calibration_attempt IDs, drift history
    status            text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN','IN_PROGRESS','CLOSED','CANCELLED')),
    opened_at         timestamptz NOT NULL DEFAULT now(),
    closed_at         timestamptz,
    technician_notes  text,
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_maintenance_work_order_hub ON og.maintenance_work_order(hub_id, status);

CREATE TABLE og.asset_event (
    asset_event_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    hub_id               text NOT NULL REFERENCES og.hub(hub_id),
    work_order_id        uuid REFERENCES og.maintenance_work_order(work_order_id),
    event_type           text NOT NULL CHECK (event_type IN
        ('STATE_TRANSITION','INVERTER_REPLACED','RECOMMISSIONED')),
    from_state           text,
    to_state             text,
    old_inverter_serial  text,
    new_inverter_serial  text,
    old_firmware         text,
    new_firmware         text,
    reason_code          text,
    occurred_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_asset_event_hub ON og.asset_event(hub_id, occurred_at DESC);

-- =====================================================================================================
-- 4. og.trace.decision_type gains ASSET_STATE_TRANSITION and CALIBRATION_ATTEMPT (S5.5.7, S8.5).
--    Additive change to the existing CHECK constraint, not a new table. "trace_decision_type_check" is
--    Postgres's default auto-generated name for the unnamed column-level CHECK in 0001_init.sql
--    (`<table>_<column>_check`).
-- =====================================================================================================

ALTER TABLE og.trace DROP CONSTRAINT trace_decision_type_check;
ALTER TABLE og.trace ADD CONSTRAINT trace_decision_type_check CHECK (decision_type IN
    ('DA_PLAN','ID_PLAN','ADMISSION','COMMITMENT','RENOMINATION','RT_ALLOCATION',
     'SUBSTITUTION','GUARDIAN_VERDICT','SAFE_STOP','SHORTFALL','OPERATOR_ACTION',
     'FEED_CHANGE','ALERT','SETTLEMENT','ASSET_STATE_TRANSITION','CALIBRATION_ATTEMPT'));

-- =====================================================================================================
-- 5. Retention policy rows for the new trace decision types and waveform data classes (K11 pattern).
-- =====================================================================================================

INSERT INTO og.retention_policy (event_class, retention_days, prune_after_checkpoint) VALUES
    ('ASSET_STATE_TRANSITION', 1825, true),
    ('CALIBRATION_ATTEMPT',    1825, true),
    ('PQ_WAVEFORM_RAW',          30, true),
    ('PQ_WAVEFORM_SUMMARY',    2555, true)
ON CONFLICT (event_class) DO NOTHING;
