-- 0013: DATA_CENTER service type (PR #4 / WP-F, 06-service-profiles-and-power-quality.md S4.b).
-- Widens og.contract's service_type CHECK; every previously allowed value stays allowed (additive only).

ALTER TABLE og.contract DROP CONSTRAINT IF EXISTS contract_service_type_check;
ALTER TABLE og.contract ADD CONSTRAINT contract_service_type_check CHECK (service_type IN
    ('HOME', 'ERCOT_ENERGY', 'ERCOT_AS', 'DIST_DEFERRAL', 'PARTNER_CAPACITY', 'DATA_CENTER'));
