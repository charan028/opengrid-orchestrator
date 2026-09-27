-- dev/seed/noie_switch_seed.sql -- D-37 (r3.4.1): LZ_LCRA / LZ_RAYBN are regulated (NOIE) territory with NO
-- real contract. Idempotent, insert/update-only, and it touches ONLY LCRA/RAYBURN rows:
--
--   1. og.utility LCRA and RAYBURN (insert only; market_model_seed.sql upserts the same values).
--   2. One SAMPLE toll per utility, cloned from the Austin toll terms (D-29): REGULATED_CAPACITY / TOLLING,
--      90-min all-or-nothing block, $102/kW-yr (og.utility placeholder), sized to the zone's 10 x 600 kVA banks
--      (6,000 kW). Named "Sample Contract: ...", is_sample = true and SUSPENDED: never callable (tolling only
--      reserves ACTIVE contracts), never reserved, never billed (0046 CHECK: a sample is never ACTIVE).
--   3. og.bank.availability = UNAVAILABLE / REGULATED_NO_CONTRACT for every bank in LCRA/RAYBURN territory while
--      that utility has no ACTIVE non-sample contract (availability_since = now(): the K13 grandfather cut-off).
--   4. og.asset HOME_BANK rows of those zones that still say ERCOT competitive (utility_id NULL) -> the utility.
--
-- Never touches an obligation, reservation, commitment or grant, nor any AEN/CPS/NORTH/SOUTH/HOUSTON/WEST row.
-- Applied by deploy/scripts/bootstrap_from_scratch.sh phase e (fresh install, after market_model_seed.sql) and
-- by deploy/scripts/noie_switch_apply.py (existing database: one transaction, checksum guard, dry-run default).
-- No BEGIN/COMMIT here: the caller owns the transaction.

SET search_path TO og;

INSERT INTO og.utility (
    utility_id, name, territory_zones, capacity_product, payment_basis, capacity_price_usd_per_kw,
    charging_tariff_kind, off_peak_rate_usd_per_kwh, mid_peak_rate_usd_per_kwh, on_peak_rate_usd_per_kwh,
    charging_adder_usd_per_kwh, solar_cost_usd_per_kwh, solar_share_floor, free_access_granted, tariff_ref,
    source_note
) VALUES
    ('LCRA', 'LCRA (Lower Colorado River Authority)', ARRAY['LZ_LCRA'], 'UTILITY_TOLLING', 'USD_PER_KW_YEAR', 102,
     'TOU_OFF_PEAK', 0.02677, 0.04118, 0.08442, 0, 0.040, 0.30, false, 'LCRA-PLACEHOLDER-D37',
     'PLACEHOLDER (D-37): no contract exists. Terms cloned from Austin Energy''s toll (D-29: 90 min, $102/kW-yr, '
     || 'discharge calls only) and AE''s TOU off-peak charging rates until the real tariff and contract are known. '
     || 'The real counterparty may be a member city or distribution co-op.'),
    ('RAYBURN', 'Rayburn Country Electric Cooperative', ARRAY['LZ_RAYBN'], 'UTILITY_TOLLING', 'USD_PER_KW_YEAR', 102,
     'TOU_OFF_PEAK', 0.02677, 0.04118, 0.08442, 0, 0.040, 0.30, false, 'RAYBURN-PLACEHOLDER-D37',
     'PLACEHOLDER (D-37): no contract exists. Terms cloned from Austin Energy''s toll (D-29: 90 min, $102/kW-yr, '
     || 'discharge calls only) and AE''s TOU off-peak charging rates until the real tariff and contract are known. '
     || 'The real counterparty may be a member city or distribution co-op.')
ON CONFLICT (utility_id) DO NOTHING;

-- Sample tolls. Ids: LCRA customer ...ac1c / contract ...ac1d / rule ...ac1e; RAYBURN ...ac2c / ...ac2d / ...ac2e.
INSERT INTO og.contract (
    contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, end_at,
    renomination_allowed, penalty_alpha, penalty_beta, penalty_theta, degradation_cost, status,
    market, utility_id, name, is_sample
) VALUES
    ('00000000-0000-7000-8000-00000000ac1d', '00000000-0000-7000-8000-00000000ac1c',
     'REGULATED_CAPACITY', 'TOLLING', 'T1', 'regulated-tolling-profile@1', now(), NULL,
     false, 0.015, 0.35, 0.05, 0.03, 'SUSPENDED', 'REGULATED', 'LCRA',
     'Sample Contract: LCRA Tolling (placeholder terms)', true),
    ('00000000-0000-7000-8000-00000000ac2d', '00000000-0000-7000-8000-00000000ac2c',
     'REGULATED_CAPACITY', 'TOLLING', 'T1', 'regulated-tolling-profile@1', now(), NULL,
     false, 0.015, 0.35, 0.05, 0.03, 'SUSPENDED', 'REGULATED', 'RAYBURN',
     'Sample Contract: Rayburn Tolling (placeholder terms)', true)
ON CONFLICT (contract_id) DO UPDATE SET
    name = EXCLUDED.name, is_sample = true, status = 'SUSPENDED', updated_at = now()
WHERE og.contract.is_sample OR og.contract.name IS NULL;

-- 6,000 kW (10 banks x 600 kVA) all-or-nothing, 90-min block: the same rule shape as Austin's ...ae0e.
INSERT INTO og.product_rule (
    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, duration_minutes, variable_kind
) VALUES
    ('00000000-0000-7000-8000-00000000ac1e', '00000000-0000-7000-8000-00000000ac1d',
     'REG_CAPACITY', 6000, 0, true, 90, 'BINARY'),
    ('00000000-0000-7000-8000-00000000ac2e', '00000000-0000-7000-8000-00000000ac2d',
     'REG_CAPACITY', 6000, 0, true, 90, 'BINARY')
ON CONFLICT (contract_id, product_code) DO NOTHING;

-- Unavailable until a real (non-sample) contract for the utility is ACTIVE.
UPDATE og.bank b
SET availability = 'UNAVAILABLE', availability_reason = 'REGULATED_NO_CONTRACT', availability_since = now()
FROM og.utility u
WHERE u.utility_id IN ('LCRA', 'RAYBURN')
  AND b.zone = ANY (u.territory_zones)
  AND b.availability = 'AVAILABLE'
  AND NOT EXISTS (
      SELECT 1 FROM og.contract c
      WHERE c.utility_id = u.utility_id AND c.status = 'ACTIVE' AND NOT c.is_sample
  );

-- Regulated HOME_BANK asset rows (topology_seed.py writes new ones with the utility already).
UPDATE og.asset a
SET utility_id = u.utility_id, updated_at = now()
FROM og.utility u
WHERE u.utility_id IN ('LCRA', 'RAYBURN')
  AND a.asset_class = 'HOME_BANK'
  AND a.zone = ANY (u.territory_zones)
  AND a.utility_id IS NULL;
