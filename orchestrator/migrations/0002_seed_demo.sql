-- Demo seed data: 5 customers with one contract each, one per MVP-S service, so the demo serves several
-- customers concurrently from the start (BUILD.md S2). Product rules per contract follow the review's
-- divisibility decision (02a S1.3): ERCOT_AS is semi-continuous (min 0.1 MW, increment 0.1 MW),
-- ERCOT_ENERGY is continuous, PARTNER_CAPACITY is block (all-or-nothing).

SET search_path TO og;

-- Fixed UUIDs (uuidv7-shaped but hand-picked for reproducibility across environments/tests).
-- customer/contract ids: 00000000-0000-7000-8000-0000000000c<n> / ...d<n>

INSERT INTO og.contract (
    contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, end_at,
    renomination_allowed, penalty_alpha, penalty_beta, penalty_theta, degradation_cost, status
) VALUES
    ('00000000-0000-7000-8000-000000000d01', '00000000-0000-7000-8000-000000000c01',
     'HOME', 'ALR', 'L1', 'home-profile@1', now() - interval '30 days', NULL,
     false, NULL, NULL, NULL, 0.03, 'ACTIVE'),

    ('00000000-0000-7000-8000-000000000d02', '00000000-0000-7000-8000-000000000c02',
     'ERCOT_ENERGY', 'NCLR', 'T2', 'ercot-energy-profile@1', now() - interval '10 days', NULL,
     true, 0.01, 0.25, 0.10, 0.03, 'ACTIVE'),

    ('00000000-0000-7000-8000-000000000d03', '00000000-0000-7000-8000-000000000c03',
     'ERCOT_AS', 'NONSPIN', 'T2', 'ercot-as-profile@1', now() - interval '10 days', NULL,
     true, 0.02, 0.30, 0.10, 0.03, 'ACTIVE'),

    ('00000000-0000-7000-8000-000000000d04', '00000000-0000-7000-8000-000000000c04',
     'DIST_DEFERRAL', 'TDU_SB415', 'T1', 'dist-deferral-profile@1', now() - interval '5 days', NULL,
     false, 0.015, 0.35, 0.05, 0.03, 'ACTIVE'),

    ('00000000-0000-7000-8000-000000000d05', '00000000-0000-7000-8000-000000000c05',
     'PARTNER_CAPACITY', 'EVENT', 'T3', 'partner-capacity-profile@1', now() - interval '2 days', NULL,
     false, 0.01, 0.40, 0.10, 0.03, 'ACTIVE')
ON CONFLICT (contract_id) DO NOTHING;

INSERT INTO og.product_rule (
    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, duration_minutes, variable_kind
) VALUES
    -- HOME: no market product, reserve integrity only -- no product_rule row needed for MVP-S.

    -- ERCOT_ENERGY: continuous energy offer.
    ('00000000-0000-7000-8000-000000000e02', '00000000-0000-7000-8000-000000000d02',
     'ENERGY', 0, 0, false, 15, 'CONTINUOUS'),

    -- ERCOT_AS: semi-continuous, ERCOT min 0.1 MW = 100 kW, increment 0.1 MW = 100 kW.
    ('00000000-0000-7000-8000-000000000e03', '00000000-0000-7000-8000-000000000d03',
     'NONSPIN', 100, 100, false, 60, 'SEMI_CONTINUOUS'),

    -- DIST_DEFERRAL: capacity hold, semi-continuous at 1 kW granularity.
    ('00000000-0000-7000-8000-000000000e04', '00000000-0000-7000-8000-000000000d04',
     'CAPACITY_HOLD', 1, 1, false, 240, 'SEMI_CONTINUOUS'),

    -- PARTNER_CAPACITY: all-or-nothing event block.
    ('00000000-0000-7000-8000-000000000e05', '00000000-0000-7000-8000-000000000d05',
     'EVENT_BLOCK', 50, 0, true, 120, 'BINARY')
ON CONFLICT (contract_id, product_code) DO NOTHING;
