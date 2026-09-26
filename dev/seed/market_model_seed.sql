-- dev/seed/market_model_seed.sql
--
-- Two-market model demo seed (08-market-model-two-markets.md S3/S3a/S3c, 09-optimizer-dispatcher-update.md
-- S11; migration 0025_market_model.sql). NOT run by any migration or deploy; the lead applies it by hand:
--
--   psql "$DSN" -v ON_ERROR_STOP=1 -f dev/seed/market_model_seed.sql
--
-- Preconditions:
--   1. Migrations 0025 (og.utility, og.asset, og.contract.market/utility_id) and 0029 (og.feeder_limit,
--      og.substation_limit) are applied.
--   2. The Austin zone block exists: dev/seed/add_austin_fleet.sql has run (bank-040..049 on LZ_AEN,
--      feeders feeder-LZ_AEN-00/01), and FLEET-SIM has enabled the substation asset sub-LZ_AEN-00
--      (integration-sims/config/fleet.yaml substation_assets, D-29 b).
--
-- Contents:
--   * og.utility AUSTIN_ENERGY and CPS_ENERGY -- the same planning values as opengrid.market.config
--     (DEFAULT_UTILITIES); sourced in integrations/regulated-utilities-austin-cps-2026-09.md.
--   * The demo utility TOLL (D-29 a/b): REGULATED_CAPACITY variant TOLLING with Austin Energy, 24 MW, a
--     90-min all-or-nothing block, $102/kW-yr (og.utility.capacity_price_usd_per_kw). opengrid.contracts.
--     tolling reserves it daily in [contracts.tolling]'s window (default 16:30-18:00 CT). 24 MW fits the
--     Austin home block (~6 MW behind 600 kVA banks) plus the 20 MW substation set (~26 MW).
--   * The 20 MW / 2 h substation battery set sub-LZ_AEN-00 (the sim's one-hub bank bank-sub-LZ_AEN-00):
--     og.bank (own feeder feeder-sub-LZ_AEN-00, 20408 kVA) + og.hub rows so the selector and allocator see it,
--     og.feeder_limit / og.substation_limit rows so the guardian passes a 20 MW call, and its og.asset row (RTE 0.88, 20%
--     floor, POI 20 MW both ways, planning capex $1,000/kW). The earlier placeholder asset sub-aen-01, if
--     present, is RETIRED.
--
-- Idempotent: ON CONFLICT ... DO UPDATE. The only contract it rewrites is its own demo contract ...ae0d (terms
-- for FUTURE admissions only: existing obligations keep their committed kW, K13). The substation hub_state row
-- is DO NOTHING (never clobber live telemetry). It touches no other contract, obligation, reservation or grant.

BEGIN;

SET search_path TO og;

INSERT INTO og.utility (
    utility_id, name, territory_zones, capacity_product, payment_basis, capacity_price_usd_per_kw,
    charging_tariff_kind, off_peak_rate_usd_per_kwh, mid_peak_rate_usd_per_kwh, on_peak_rate_usd_per_kwh,
    charging_adder_usd_per_kwh, solar_cost_usd_per_kwh, solar_share_floor, free_access_granted, tariff_ref,
    source_note
) VALUES
    ('AUSTIN_ENERGY', 'Austin Energy', ARRAY['LZ_AEN'], 'UTILITY_TOLLING', 'USD_PER_KW_YEAR', 102,
     'TOU_OFF_PEAK', 0.02677, 0.04118, 0.08442,
     0, 0.040, 0.30, false, 'AE-FY2026-RES-TOU-PILOT',
     'AE FY2026 tariff (eff. 2025-11-01) residential TOU pilot power supply: off-peak 2.677, mid 4.118, '
     || 'on 8.442 cents/kWh. Capacity $102/kW-yr fixed: utility tolling, RCA 26-1526 terms (D-29). Adders 0 per 08 S3c.'),
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

-- Demo utility toll: customer = Austin Energy (the utility is the customer, 08 S1).
-- Ids: customer ...ae0c, contract ...ae0d, product rule ...ae0e (outside the c<n>/d<n> demo ranges).
INSERT INTO og.contract (
    contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, end_at,
    renomination_allowed, penalty_alpha, penalty_beta, penalty_theta, degradation_cost, status,
    market, utility_id
) VALUES (
    '00000000-0000-7000-8000-00000000ae0d', '00000000-0000-7000-8000-00000000ae0c',
    'REGULATED_CAPACITY', 'TOLLING', 'T1', 'regulated-tolling-profile@1', now(), NULL,
    false, 0.015, 0.35, 0.05, 0.03, 'ACTIVE',
    'REGULATED', 'AUSTIN_ENERGY'
)
ON CONFLICT (contract_id) DO UPDATE SET
    service_type = EXCLUDED.service_type, variant = EXCLUDED.variant, tier = EXCLUDED.tier,
    profile_ref = EXCLUDED.profile_ref, market = EXCLUDED.market, utility_id = EXCLUDED.utility_id,
    updated_at = now();

-- 24 MW all-or-nothing, 90-min block (D-29 a). Must equal [contracts.tolling]'s window length.
INSERT INTO og.product_rule (
    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, duration_minutes, variable_kind
) VALUES (
    '00000000-0000-7000-8000-00000000ae0e', '00000000-0000-7000-8000-00000000ae0d',
    'REG_CAPACITY', 24000, 0, true, 90, 'BINARY'
)
ON CONFLICT (contract_id, product_code) DO UPDATE SET
    min_qty_kw = EXCLUDED.min_qty_kw, increment_kw = EXCLUDED.increment_kw, block = EXCLUDED.block,
    duration_minutes = EXCLUDED.duration_minutes, variable_kind = EXCLUDED.variable_kind;

-- The substation set as the sim models it: one hub in its own bank (ogsim.fleet.state._substation_segment).
-- 20 MW / 40 MWh, 20% floor (8 MWh), eta_c = eta_d = sqrt(0.88).
INSERT INTO og.bank (bank_id, zone, kva_rating, reserve_kva, feeder_id)
VALUES ('bank-sub-LZ_AEN-00', 'LZ_AEN', 20408, 0, 'feeder-sub-LZ_AEN-00')
ON CONFLICT (bank_id) DO UPDATE SET
    zone = EXCLUDED.zone, kva_rating = EXCLUDED.kva_rating, feeder_id = EXCLUDED.feeder_id;

INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw, eta_c, eta_d)
VALUES ('sub-LZ_AEN-00', 'bank-sub-LZ_AEN-00', 'LZ_AEN', 40000, 8000, 20000, 0.9381, 0.9381)
ON CONFLICT (hub_id) DO UPDATE SET
    bank_id = EXCLUDED.bank_id, zone = EXCLUDED.zone, e_kwh = EXCLUDED.e_kwh, r_kwh = EXCLUDED.r_kwh,
    p_kw = EXCLUDED.p_kw, eta_c = EXCLUDED.eta_c, eta_d = EXCLUDED.eta_d;

INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, health, lease_epoch, last_seen_at)
VALUES ('sub-LZ_AEN-00', 8000, 0, 'offline', 0, now())
ON CONFLICT (hub_id) DO NOTHING;

-- 20 MW / 2 h substation battery set in Austin Energy territory (09 D11 defaults; 4 h = e_kwh 80000).
INSERT INTO og.asset (
    asset_id, asset_class, bank_id, feeder_id, substation_id, zone, utility_id, p_kw, e_kwh, eta_rt,
    floor_frac, poi_import_kva, poi_export_kva, capex_usd, status
) VALUES (
    'sub-LZ_AEN-00', 'SUBSTATION', 'bank-sub-LZ_AEN-00', 'feeder-sub-LZ_AEN-00', 'sub-LZ_AEN-00', 'LZ_AEN',
    'AUSTIN_ENERGY', 20000, 40000, 0.88, 0.20, 20000, 20000, 20000000, 'ACTIVE'
)
ON CONFLICT (asset_id) DO UPDATE SET
    asset_class = EXCLUDED.asset_class, bank_id = EXCLUDED.bank_id, feeder_id = EXCLUDED.feeder_id,
    substation_id = EXCLUDED.substation_id, zone = EXCLUDED.zone, utility_id = EXCLUDED.utility_id,
    p_kw = EXCLUDED.p_kw, e_kwh = EXCLUDED.e_kwh, eta_rt = EXCLUDED.eta_rt, floor_frac = EXCLUDED.floor_frac,
    poi_import_kva = EXCLUDED.poi_import_kva, poi_export_kva = EXCLUDED.poi_export_kva,
    capex_usd = EXCLUDED.capex_usd, status = EXCLUDED.status, updated_at = now();

UPDATE og.asset SET status = 'RETIRED', updated_at = now() WHERE asset_id = 'sub-aen-01';

-- Guardian flow limits for the set (migration 0029). Without them the 20 MW toll call is vetoed by the
-- defaults: G-28 thermal 10 MW x 0.95, and G-29 (a substation with members and no limit row is unknown:
-- any increase vetoed). The set has its OWN feeder, feeder-sub-LZ_AEN-00 (it connects at the substation
-- bus, not on a home feeder), so these limits never loosen the Austin home banks' feeder-LZ_AEN-00.
--   thermal 24 MW (x 0.95 = 22.8 MW >= the 20 MW POI); reverse 20 MW = the POI export limit.
-- ASSUMPTION to confirm with Austin Energy: reverse flow up to the POI export at this substation. The
-- territory boundary (no net export out of Austin Energy, K15 c) stays G-30's check, not relaxed here.
-- The G-06 ramp ceiling for this feeder is config: [guardian.feeder_ramp_ceiling_kw_per_min_by_feeder]
-- in orchestrator.toml (20 MW/min).
INSERT INTO og.feeder_limit (feeder_id, thermal_kw, reverse_kw)
VALUES ('feeder-sub-LZ_AEN-00', 24000, 20000)
ON CONFLICT (feeder_id) DO UPDATE SET
    thermal_kw = EXCLUDED.thermal_kw, reverse_kw = EXCLUDED.reverse_kw, updated_at = now();

INSERT INTO og.substation_limit (substation_id, rating_kva, reverse_kw)
VALUES ('sub-LZ_AEN-00', 24000, 20000)
ON CONFLICT (substation_id) DO UPDATE SET
    rating_kva = EXCLUDED.rating_kva, reverse_kw = EXCLUDED.reverse_kw, updated_at = now();

COMMIT;

-- Verification (read-only):
--   SELECT utility_id, capacity_price_usd_per_kw FROM og.utility;                  -- AUSTIN_ENERGY 102
--   SELECT contract_id, service_type, variant, market, utility_id FROM og.contract WHERE market = 'REGULATED';
--   SELECT min_qty_kw, duration_minutes FROM og.product_rule
--    WHERE contract_id = '00000000-0000-7000-8000-00000000ae0d';                     -- 24000, 90
--   SELECT asset_id, asset_class, bank_id, status FROM og.asset;                      -- sub-LZ_AEN-00 ACTIVE
--   SELECT * FROM og.feeder_limit WHERE feeder_id = 'feeder-sub-LZ_AEN-00';          -- 24000 / 20000
--   SELECT * FROM og.substation_limit WHERE substation_id = 'sub-LZ_AEN-00';         -- 24000 / 20000