-- dev/seed/market_model_seed.sql
--
-- Two-market model demo seed (08-market-model-two-markets.md S3/S3a/S3c, 09-optimizer-dispatcher-update.md
-- S11; migration 0025_market_model.sql). NOT run by any migration or deploy; the lead applies it by hand:
--
--   psql "$DSN" -v ON_ERROR_STOP=1 -f dev/seed/market_model_seed.sql
--
-- Preconditions:
--   1. Migration 0025 is applied (og.utility, og.asset, og.contract.market/utility_id).
--   2. The Austin zone block exists: dev/seed/add_austin_fleet.sql has run (bank-040..049 on LZ_AEN,
--      feeders feeder-LZ_AEN-00/01). The substation asset below links to feeder-LZ_AEN-00 only (no FK
--      to og.bank), so this script does not fail without it -- but nothing can serve the demo contract
--      until the Austin banks exist and the lead enables the block.
--
-- Contents:
--   * og.utility AUSTIN_ENERGY and CPS_ENERGY -- the same planning values as opengrid.market.config
--     (DEFAULT_UTILITIES); sourced in integrations/regulated-utilities-austin-cps-2026-09.md.
--   * One demo REGULATED_CAPACITY contract with Austin Energy: 2 MW, Power Partner-like $75/kW-yr
--     (the price is og.utility.capacity_price_usd_per_kw), a 3 h weekday event block (15:00-18:00, 09 OQ-3).
--   * One SUBSTATION asset on LZ_AEN: 20 MW / 2 h (40 MWh), RTE 0.88, 20% floor, POI 20 MW both ways,
--     planning capex $1,000/kW.
--
-- Additive and idempotent: ON CONFLICT ... DO UPDATE for reference rows, DO NOTHING for the contract (never
-- rewrite a live contract). It touches no existing contract, obligation, reservation, grant or bank.

BEGIN;

SET search_path TO og;

INSERT INTO og.utility (
    utility_id, name, territory_zones, capacity_product, payment_basis, capacity_price_usd_per_kw,
    charging_tariff_kind, off_peak_rate_usd_per_kwh, mid_peak_rate_usd_per_kwh, on_peak_rate_usd_per_kwh,
    charging_adder_usd_per_kwh, solar_cost_usd_per_kwh, solar_share_floor, free_access_granted, tariff_ref,
    source_note
) VALUES
    ('AUSTIN_ENERGY', 'Austin Energy', ARRAY['LZ_AEN'], 'RESIDENTIAL_BATTERY_DR', 'USD_PER_KW_YEAR', 75,
     'TOU_OFF_PEAK', 0.02677, 0.04118, 0.08442,
     0, 0.040, 0.30, false, 'AE-FY2026-RES-TOU-PILOT',
     'AE FY2026 tariff (eff. 2025-11-01) residential TOU pilot power supply: off-peak 2.677, mid 4.118, '
     || 'on 8.442 cents/kWh; capacity $75/kW-yr is Power Partner-like (secondary source). Adders 0 per 08 S3c.'),
    ('CPS_ENERGY', 'CPS Energy', ARRAY['LZ_CPS'], 'DEMAND_RESPONSE', 'USD_PER_KW_YEAR', 45,
     'NIGHT_RATE', 0.05026, NULL, NULL,
     0, 0.040, 0.30, false, 'CPS-2024-PL-PLANNING',
     'PLACEHOLDER pending the contract (09 OQ-6): CPS has no published TOU night rate; uses Schedule PL '
     || 'additional-kWh energy 3.610 + fuel base 1.416 cents/kWh. Capacity $45/kW per season (C&I DR).')
ON CONFLICT (utility_id) DO UPDATE SET
    name = EXCLUDED.name, territory_zones = EXCLUDED.territory_zones,
    capacity_product = EXCLUDED.capacity_product, payment_basis = EXCLUDED.payment_basis,
    capacity_price_usd_per_kw = EXCLUDED.capacity_price_usd_per_kw,
    charging_tariff_kind = EXCLUDED.charging_tariff_kind,
    off_peak_rate_usd_per_kwh = EXCLUDED.off_peak_rate_usd_per_kwh,
    mid_peak_rate_usd_per_kwh = EXCLUDED.mid_peak_rate_usd_per_kwh,
    on_peak_rate_usd_per_kwh = EXCLUDED.on_peak_rate_usd_per_kwh,
    charging_adder_usd_per_kwh = EXCLUDED.charging_adder_usd_per_kwh,
    solar_cost_usd_per_kwh = EXCLUDED.solar_cost_usd_per_kwh, solar_share_floor = EXCLUDED.solar_share_floor,
    free_access_granted = EXCLUDED.free_access_granted, tariff_ref = EXCLUDED.tariff_ref,
    source_note = EXCLUDED.source_note, updated_at = now();

-- Demo REGULATED_CAPACITY contract: customer = Austin Energy (the utility is the customer, 08 S1).
-- Ids: customer ...ae0c, contract ...ae0d, product rule ...ae0e (outside the c<n>/d<n> demo ranges).
INSERT INTO og.contract (
    contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, end_at,
    renomination_allowed, penalty_alpha, penalty_beta, penalty_theta, degradation_cost, status,
    market, utility_id
) VALUES (
    '00000000-0000-7000-8000-00000000ae0d', '00000000-0000-7000-8000-00000000ae0c',
    'REGULATED_CAPACITY', 'POWER_PARTNER', 'T1', 'regulated-capacity-profile@1', now(), NULL,
    false, 0.015, 0.35, 0.05, 0.03, 'ACTIVE',
    'REGULATED', 'AUSTIN_ENERGY'
)
ON CONFLICT (contract_id) DO NOTHING;

-- 2 MW all-or-nothing, 3 h event block (15:00-18:00 weekdays, 09 OQ-3 default).
INSERT INTO og.product_rule (
    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, duration_minutes, variable_kind
) VALUES (
    '00000000-0000-7000-8000-00000000ae0e', '00000000-0000-7000-8000-00000000ae0d',
    'REG_CAPACITY', 2000, 0, true, 180, 'BINARY'
)
ON CONFLICT (contract_id, product_code) DO NOTHING;

-- 20 MW / 2 h substation battery set in Austin Energy territory (09 D11 defaults; 4 h = e_kwh 80000).
INSERT INTO og.asset (
    asset_id, asset_class, bank_id, feeder_id, substation_id, zone, utility_id, p_kw, e_kwh, eta_rt,
    floor_frac, poi_import_kva, poi_export_kva, capex_usd, status
) VALUES (
    'sub-aen-01', 'SUBSTATION', NULL, 'feeder-LZ_AEN-00', 'sub-aen-01', 'LZ_AEN', 'AUSTIN_ENERGY',
    20000, 40000, 0.88, 0.20, 20000, 20000, 20000000, 'PLANNED'
)
ON CONFLICT (asset_id) DO UPDATE SET
    asset_class = EXCLUDED.asset_class, feeder_id = EXCLUDED.feeder_id, substation_id = EXCLUDED.substation_id,
    zone = EXCLUDED.zone, utility_id = EXCLUDED.utility_id, p_kw = EXCLUDED.p_kw, e_kwh = EXCLUDED.e_kwh,
    eta_rt = EXCLUDED.eta_rt, floor_frac = EXCLUDED.floor_frac, poi_import_kva = EXCLUDED.poi_import_kva,
    poi_export_kva = EXCLUDED.poi_export_kva, capex_usd = EXCLUDED.capex_usd, updated_at = now();

COMMIT;

-- Verification (read-only):
--   SELECT utility_id, capacity_price_usd_per_kw, off_peak_rate_usd_per_kwh FROM og.utility;   -- 2 rows
--   SELECT contract_id, service_type, market, utility_id FROM og.contract WHERE market = 'REGULATED';
--   SELECT asset_id, asset_class, zone, utility_id, p_kw, e_kwh FROM og.asset;
