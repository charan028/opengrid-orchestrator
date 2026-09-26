-- Dev/test seed: the DATA_CENTER and PIPELINE_AC customers the customer simulator drives
-- (06-service-profiles-and-power-quality.md S4.a/S4.b). Not a migration: apply by hand after migrations
-- 0001-0026 (0025 registers PIPELINE_AC as a service type):
--   psql "$DSN" -v ON_ERROR_STOP=1 -f dev/seed/customer_services_seed.sql
-- Idempotent (ON CONFLICT DO NOTHING); one transaction.
--
-- Customer ids used by [api.roles.customer] and by the simulator's topics and payloads:
--   og-cust-ercot   -> 00000000-0000-7000-8000-000000000c02 (contract ...d02, ERCOT_ENERGY, 0002 seed)
--   og-cust-dist    -> 00000000-0000-7000-8000-000000000c04 (contract ...d04, DIST_DEFERRAL, 0002 seed)
--   og-cust-partner -> 00000000-0000-7000-8000-000000000c05 (contract ...d05, PARTNER_CAPACITY, 0002 seed)
--   og-cust-dc      -> 00000000-0000-7000-8000-0000000000c6 (contract ...d06, DATA_CENTER, below)
--   og-cust-pipe    -> 00000000-0000-7000-8000-0000000000c7 (contract ...d07, PIPELINE_AC, below)
--
-- Both contracts stay behind the S2.7 activation gate: admission of their opportunities is refused
-- until [contracts.activation].data_center = true (the PIPELINE_AC contract carries variant
-- 'PIPELINE_AC', which that gate keys on).

BEGIN;
SET search_path TO og;

-- ---- DATA_CENTER (config/service_profiles/data_center.toml), 400 kW firm bridging --------------------

INSERT INTO og.contract (
    contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, end_at,
    renomination_allowed, penalty_alpha, penalty_beta, penalty_theta, degradation_cost, status
) VALUES (
    '00000000-0000-7000-8000-0000000000d6', '00000000-0000-7000-8000-0000000000c6',
    'DATA_CENTER', 'BRIDGING', 'T1', 'data-center-profile@1', now() - interval '1 day', NULL,
    true, 0.02, 0.40, 0.05, 0.03, 'ACTIVE'
) ON CONFLICT (contract_id) DO NOTHING;

INSERT INTO og.product_rule (
    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, duration_minutes, variable_kind
) VALUES (
    '00000000-0000-7000-8000-0000000000e6', '00000000-0000-7000-8000-0000000000d6',
    'CAPACITY_HOLD', 10, 1, false, 240, 'SEMI_CONTINUOUS'
) ON CONFLICT (contract_id, product_code) DO NOTHING;

INSERT INTO og.pq_envelope (
    pq_envelope_id, customer_id, phase_config, max_phase_imbalance_pct, voltage_band_pct,
    ride_through_class, freq_tolerance_hz, pf_min, reactive_requirement, thd_voltage_limit_pct,
    thd_current_limit_pct
) VALUES (
    '00000000-0000-7000-8000-0000000000f6', '00000000-0000-7000-8000-0000000000c6', '3P', 1.5, 2.0,
    'CATEGORY_III', 0.5, 0.95, 'pf >= 0.95 at the site meter during a bridge', 2.5, 2.5
) ON CONFLICT (pq_envelope_id) DO NOTHING;

-- Per-contract numbers for 400 kW (data_center.toml [instantiation]): ramp 30 kW/min per committed kW
-- = 12,000 kW/min, tolerance 2 % = 8 kW, deadband 1 % = 4 kW; a 4 h bridge.
INSERT INTO og.service_profile (
    service_profile_id, contract_id, version, control_primitive, target_quantity, target_scope,
    setpoint_source, feedback_signal_ref, response_time_s, ramp_limit, ramp_limit_unit, sustain_duration_s,
    accuracy_tolerance, deadband, priority_tier, mv_method, settlement_metric, pq_envelope_id, failure_behaviour
) VALUES (
    '00000000-0000-7000-8000-0000000000a6', '00000000-0000-7000-8000-0000000000d6', 1,
    'CLOSED_LOOP_REGULATION', 'KW', 'SITE_METER', 'MEASURED_FEEDBACK', 'site_meter:site-dc-01:p_kw',
    2.0, 12000, 'kw_per_min', 14400, 8.0, 4.0, 'T1', 'DIRECT_HUB_METER', 'capacity_payment_x_pf',
    '00000000-0000-7000-8000-0000000000f6', 'HOLD_THEN_SCHEDULE'
) ON CONFLICT (service_profile_id) DO NOTHING;

-- ---- PIPELINE_AC (S4.a), corridor-pipe-01, 15 A limit --------------------------------------------------

INSERT INTO og.contract (
    contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, end_at,
    renomination_allowed, penalty_alpha, penalty_beta, penalty_theta, degradation_cost, status
) VALUES (
    '00000000-0000-7000-8000-0000000000d7', '00000000-0000-7000-8000-0000000000c7',
    'PIPELINE_AC', 'PIPELINE_AC', 'T4', 'pipeline-ac-profile@1', now() - interval '1 day', NULL,
    false, NULL, NULL, NULL, 0.03, 'ACTIVE'
) ON CONFLICT (contract_id) DO NOTHING;

INSERT INTO og.product_rule (
    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, duration_minutes, variable_kind
) VALUES (
    '00000000-0000-7000-8000-0000000000e7', '00000000-0000-7000-8000-0000000000d7',
    'CAPACITY_HOLD', 0, 1, false, 240, 'SEMI_CONTINUOUS'
) ON CONFLICT (contract_id, product_code) DO NOTHING;

-- S4.a: current cap on the monitored line, tight THD_I, grid-code minimum otherwise.
INSERT INTO og.pq_envelope (
    pq_envelope_id, customer_id, phase_config, current_limit_a, current_limit_scope, thd_current_limit_pct
) VALUES (
    '00000000-0000-7000-8000-0000000000f7', '00000000-0000-7000-8000-0000000000c7', '3P', 15.0,
    'SPECIFIC_LINE', 2.5
) ON CONFLICT (pq_envelope_id) DO NOTHING;

-- r_A 30 A/min (S4.a default); NEUTRAL_ON_SIGNAL_LOSS; M&V achieved line-current delta.
INSERT INTO og.service_profile (
    service_profile_id, contract_id, version, control_primitive, target_quantity, target_scope,
    setpoint_source, feedback_signal_ref, response_time_s, ramp_limit, ramp_limit_unit,
    accuracy_tolerance, deadband, priority_tier, mv_method, settlement_metric, pq_envelope_id, failure_behaviour
) VALUES (
    '00000000-0000-7000-8000-0000000000a7', '00000000-0000-7000-8000-0000000000d7', 1,
    'CLOSED_LOOP_REGULATION', 'LINE_CURRENT_A', 'CORRIDOR_LINE', 'MEASURED_FEEDBACK',
    'corridor:corridor-pipe-01:i_ac_a', 60.0, 30, 'a_per_min', 0.5, 0.2, 'T4', 'DIRECT_HUB_METER',
    'achieved_line_current_delta_a', '00000000-0000-7000-8000-0000000000f7', 'NEUTRAL_ON_SIGNAL_LOSS'
) ON CONFLICT (service_profile_id) DO NOTHING;

COMMIT;
