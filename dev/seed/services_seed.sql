-- Dev/test seed: demo customers and contracts for PJM_CAPACITY, MOBILE_STORAGE and LARGE_LOAD (owner
-- decision 2026-09-26, 06-service-profiles-and-power-quality.md S4.b/S4.c pattern, config/
-- service_profiles/{pjm_capacity,mobile_storage,large_load}.toml). Not a migration and NOT run by CI or
-- any build step -- apply by hand, after migration 0025 registers the three service types:
--   psql "$DSN" -v ON_ERROR_STOP=1 -f dev/seed/services_seed.sql
-- Idempotent (ON CONFLICT DO NOTHING); one transaction. Follows the exact shape and id-block convention
-- of dev/seed/customer_services_seed.sql (DATA_CENTER/PIPELINE_AC) -- next free customer/contract id
-- suffixes in that file's 0xc/0xd/0xe/0xf block are c8/c9/ca, d8/d9/da, e8/e9/ea, f8/f9/fa.
--
-- Customer ids used by [api.roles.customer] and by the (not-yet-registered, see the SERVICES agent's
-- 2026-09-26 report) FLEET-SIM/customer simulator wiring for these three profiles:
--   og-cust-pjm     -> 00000000-0000-7000-8000-0000000000c8 (contract ...d8, PJM_CAPACITY)
--   og-cust-mobile  -> 00000000-0000-7000-8000-0000000000c9 (contract ...d9, MOBILE_STORAGE)
--   og-cust-largeld -> 00000000-0000-7000-8000-0000000000ca (contract ...da, LARGE_LOAD)
--
-- All three stay behind the S2.7 activation gate like DATA_CENTER/PIPELINE_AC: admission of their
-- opportunities is refused until [contracts.activation] enables the matching service type (contracts
-- owner, live-path, to wire alongside the migration 0025 rollout).

BEGIN;
SET search_path TO og;

-- ---- PJM_CAPACITY (config/service_profiles/pjm_capacity.toml), 5,000 kW simulated RPM commitment -----

INSERT INTO og.contract (
    contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, end_at,
    renomination_allowed, penalty_alpha, penalty_beta, penalty_theta, degradation_cost, status
) VALUES (
    '00000000-0000-7000-8000-0000000000d8', '00000000-0000-7000-8000-0000000000c8',
    'PJM_CAPACITY', 'RPM_CAPACITY_PERFORMANCE', 'T3', 'pjm-capacity-profile@1',
    now() - interval '1 day', NULL, true, 0.01, 0.30, 0.05, 0.03, 'ACTIVE'
) ON CONFLICT (contract_id) DO NOTHING;

INSERT INTO og.product_rule (
    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, duration_minutes, variable_kind
) VALUES (
    '00000000-0000-7000-8000-0000000000e8', '00000000-0000-7000-8000-0000000000d8',
    'CAPACITY_HOLD', 10, 1, false, 60, 'SEMI_CONTINUOUS'
) ON CONFLICT (contract_id, product_code) DO NOTHING;

-- Grid-code minimum only (S4.c pattern): only phase_config is set, everything else is the
-- og.pq_envelope column DEFAULT (matches the fleet-default envelope of 06 S2).
INSERT INTO og.pq_envelope (pq_envelope_id, customer_id, phase_config) VALUES (
    '00000000-0000-7000-8000-0000000000f8', '00000000-0000-7000-8000-0000000000c8', '3P'
) ON CONFLICT (pq_envelope_id) DO NOTHING;

-- Per-contract numbers for 5,000 kW (pjm_capacity.toml [instantiation]): ramp 0.1 kW/min per committed kW
-- = 500 kW/min (reaches 5,000 kW within the 10-minute Capacity Performance deadline), tolerance 5% =
-- 250 kW, deadband 2% = 100 kW; a 1 h declared emergency performance hour. feedback_signal_ref is NULL:
-- setpoint_source = ISO_INSTRUCTION, not MEASURED_FEEDBACK, so no feedback point is required or read
-- (lead fix 2026-09-26: a prior draft pointed this at a non-existent 'p_kw_net' site-meter field, which
-- failed resolution as malformed -- see config/service_profiles/pjm_capacity.toml's comment).
INSERT INTO og.service_profile (
    service_profile_id, contract_id, version, control_primitive, target_quantity, target_scope,
    setpoint_source, feedback_signal_ref, response_time_s, ramp_limit, ramp_limit_unit, sustain_duration_s,
    accuracy_tolerance, deadband, priority_tier, mv_method, settlement_metric, pq_envelope_id, failure_behaviour
) VALUES (
    '00000000-0000-7000-8000-0000000000a8', '00000000-0000-7000-8000-0000000000d8', 1,
    'CAPACITY_HOLD', 'KW', 'SITE_METER', 'ISO_INSTRUCTION', NULL,
    600.0, 500, 'kw_per_min', 3600, 250.0, 100.0, 'T3', 'DIRECT_HUB_METER', 'capacity_payment_x_pf',
    '00000000-0000-7000-8000-0000000000f8', 'HOLD_THEN_SCHEDULE'
) ON CONFLICT (service_profile_id) DO NOTHING;

-- ---- MOBILE_STORAGE (config/service_profiles/mobile_storage.toml), 500 kW trailer, first deployment ---

INSERT INTO og.contract (
    contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, end_at,
    renomination_allowed, penalty_alpha, penalty_beta, penalty_theta, degradation_cost, status
) VALUES (
    '00000000-0000-7000-8000-0000000000d9', '00000000-0000-7000-8000-0000000000c9',
    'MOBILE_STORAGE', 'MOBILE_DEPLOYMENT', 'T2', 'mobile-storage-profile@1',
    now() - interval '1 day', NULL, true, 0.015, 0.35, 0.03, 0.03, 'ACTIVE'
) ON CONFLICT (contract_id) DO NOTHING;

INSERT INTO og.product_rule (
    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, duration_minutes, variable_kind
) VALUES (
    '00000000-0000-7000-8000-0000000000e9', '00000000-0000-7000-8000-0000000000d9',
    'CAPACITY_HOLD', 10, 1, false, 240, 'SEMI_CONTINUOUS'
) ON CONFLICT (contract_id, product_code) DO NOTHING;

INSERT INTO og.pq_envelope (pq_envelope_id, customer_id, phase_config) VALUES (
    '00000000-0000-7000-8000-0000000000f9', '00000000-0000-7000-8000-0000000000c9', '3P'
) ON CONFLICT (pq_envelope_id) DO NOTHING;

-- Per-contract numbers for 500 kW (mobile_storage.toml [instantiation]): ramp 0.2 kW/min per committed kW
-- = 100 kW/min, tolerance 3% = 15 kW, deadband 1.5% = 7.5 kW; a 4 h deployment window at the first site
-- (site-warehouse-01, matches integration-sims/scenarios/svc-mobile-storage.yaml). Relocating this
-- deployment to a second site is a NEW og.service_profile version with a re-filled feedback_signal_ref
-- (the toml's per-deployment site_id rule) -- not represented as a second row here since this is a
-- point-in-time dev seed, not a full deployment history.
INSERT INTO og.service_profile (
    service_profile_id, contract_id, version, control_primitive, target_quantity, target_scope,
    setpoint_source, feedback_signal_ref, response_time_s, ramp_limit, ramp_limit_unit, sustain_duration_s,
    accuracy_tolerance, deadband, priority_tier, mv_method, settlement_metric, pq_envelope_id, failure_behaviour
) VALUES (
    '00000000-0000-7000-8000-0000000000a9', '00000000-0000-7000-8000-0000000000d9', 1,
    'EVENT_SCHEDULE_TRACKING', 'KW', 'SITE_METER', 'PLAN', 'site_meter:site-warehouse-01:p_kw',
    300.0, 100, 'kw_per_min', 14400, 15.0, 7.5, 'T2', 'AMI_INTERVAL', 'deployment_capacity_payment',
    '00000000-0000-7000-8000-0000000000f9', 'HOLD_THEN_SCHEDULE'
) ON CONFLICT (service_profile_id) DO NOTHING;

-- ---- LARGE_LOAD (config/service_profiles/large_load.toml), 2,000 kW crypto/data load, ride-through ----

INSERT INTO og.contract (
    contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, end_at,
    renomination_allowed, penalty_alpha, penalty_beta, penalty_theta, degradation_cost, status
) VALUES (
    '00000000-0000-7000-8000-0000000000da', '00000000-0000-7000-8000-0000000000ca',
    'LARGE_LOAD', 'LOAD_FOLLOWING_FIRMING', 'T2', 'large-load-profile@1',
    now() - interval '1 day', NULL, true, 0.02, 0.40, 0.02, 0.03, 'ACTIVE'
) ON CONFLICT (contract_id) DO NOTHING;

INSERT INTO og.product_rule (
    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, duration_minutes, variable_kind
) VALUES (
    '00000000-0000-7000-8000-0000000000ea', '00000000-0000-7000-8000-0000000000da',
    'CAPACITY_HOLD', 10, 1, false, 30, 'SEMI_CONTINUOUS'
) ON CONFLICT (contract_id, product_code) DO NOTHING;

INSERT INTO og.pq_envelope (pq_envelope_id, customer_id, phase_config) VALUES (
    '00000000-0000-7000-8000-0000000000fa', '00000000-0000-7000-8000-0000000000ca', '3P'
) ON CONFLICT (pq_envelope_id) DO NOTHING;

-- Per-contract numbers for 2,000 kW (large_load.toml [instantiation]): ramp 12 kW/min per committed kW =
-- 24,000 kW/min (reaches 2,000 kW within the 5 s ride-through response deadline), tolerance 2% = 40 kW,
-- deadband 1% = 20 kW; a 30-minute curtailment/ride-through event, matches integration-sims/scenarios/
-- svc-large-load.yaml.
INSERT INTO og.service_profile (
    service_profile_id, contract_id, version, control_primitive, target_quantity, target_scope,
    setpoint_source, feedback_signal_ref, response_time_s, ramp_limit, ramp_limit_unit, sustain_duration_s,
    accuracy_tolerance, deadband, priority_tier, mv_method, settlement_metric, pq_envelope_id, failure_behaviour
) VALUES (
    '00000000-0000-7000-8000-0000000000aa', '00000000-0000-7000-8000-0000000000da', 1,
    'EVENT_SCHEDULE_TRACKING', 'KW', 'SITE_METER', 'CUSTOMER_API',
    'site_meter:large-load-crypto-01:p_kw', 5.0, 24000, 'kw_per_min', 1800, 40.0, 20.0, 'T2',
    'DIRECT_HUB_METER', 'capacity_payment_x_performance', '00000000-0000-7000-8000-0000000000fa',
    'HOLD_THEN_SCHEDULE'
) ON CONFLICT (service_profile_id) DO NOTHING;

COMMIT;
