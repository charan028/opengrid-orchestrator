-- 0025: two-market model (08-market-model-two-markets.md S3, 09-optimizer-dispatcher-update.md D1/D2/D11,
-- decision log D-20..D-22). Additive only: new tables and new nullable/defaulted columns; every existing
-- row stays valid (every existing contract becomes market = 'FREE', utility_id = NULL).
--
--   * og.utility                 -- a regulated, vertically integrated utility as a customer: territory
--                                   zones, capacity product and payment basis, and Base's charging terms
--                                   inside its territory (TOU off-peak / night rate, solar share floor).
--   * og.contract.market         -- REGULATED | FREE (default FREE).
--   * og.contract.utility_id     -- the utility of a REGULATED contract; NULL iff FREE.
--   * og.contract.service_type   -- owns the full CHECK list: gains PIPELINE_AC, REGULATED_CAPACITY (a
--                                   REGULATED contract by definition), PJM_CAPACITY, MOBILE_STORAGE,
--                                   LARGE_LOAD.
--   * og.asset                   -- Base-owned assets: HOME_BANK (mirrors og.bank) or SUBSTATION (one
--                                   battery set at a distribution substation; planning default 20 MW / 2 h,
--                                   4 h as an option), with kW, kWh, round-trip efficiency and the
--                                   interconnection (POI) limit in both directions.
--
-- Seed rows (Austin Energy, CPS Energy, a demo REGULATED_CAPACITY contract, a 20 MW substation asset) are
-- NOT in this migration: dev/seed/market_model_seed.sql, applied by hand by the lead.

SET search_path TO og;

CREATE TABLE IF NOT EXISTS og.utility (
    utility_id                 text PRIMARY KEY CHECK (utility_id IN ('AUSTIN_ENERGY', 'CPS_ENERGY')),
    name                       text NOT NULL,
    territory_zones            text[] NOT NULL,             -- ERCOT settlement-point codes, e.g. {LZ_AEN}
    capacity_product           text NOT NULL,               -- e.g. RESIDENTIAL_BATTERY_DR, DIST_CAPACITY
    payment_basis              text NOT NULL CHECK (payment_basis IN ('USD_PER_KW_MONTH', 'USD_PER_KW_YEAR')),
    capacity_price_usd_per_kw  numeric(18,6),               -- planning default; the contract overrides
    charging_tariff_kind       text NOT NULL CHECK (charging_tariff_kind IN ('TOU_OFF_PEAK', 'NIGHT_RATE')),
    off_peak_rate_usd_per_kwh  numeric(18,6) NOT NULL CHECK (off_peak_rate_usd_per_kwh >= 0),
    mid_peak_rate_usd_per_kwh  numeric(18,6) CHECK (mid_peak_rate_usd_per_kwh >= 0),
    on_peak_rate_usd_per_kwh   numeric(18,6) CHECK (on_peak_rate_usd_per_kwh >= 0),
    charging_adder_usd_per_kwh numeric(18,6) NOT NULL DEFAULT 0,   -- CBC/regulatory adders (0 per 08 S3c)
    solar_cost_usd_per_kwh     numeric(18,6) NOT NULL DEFAULT 0.040 CHECK (solar_cost_usd_per_kwh >= 0),
    solar_share_floor          numeric(6,4) NOT NULL DEFAULT 0.30
                               CHECK (solar_share_floor >= 0 AND solar_share_floor <= 1),
    free_access_granted        boolean NOT NULL DEFAULT false,  -- K15(b): ERCOT access for territory assets
    tariff_ref                 text NOT NULL,
    source_note                text,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE og.contract ADD COLUMN IF NOT EXISTS market text NOT NULL DEFAULT 'FREE';
ALTER TABLE og.contract ADD COLUMN IF NOT EXISTS utility_id text REFERENCES og.utility(utility_id);

ALTER TABLE og.contract DROP CONSTRAINT IF EXISTS contract_market_check;
ALTER TABLE og.contract ADD CONSTRAINT contract_market_check CHECK (market IN ('REGULATED', 'FREE'));

-- A REGULATED contract names its utility; a FREE contract never does.
ALTER TABLE og.contract DROP CONSTRAINT IF EXISTS contract_market_utility_check;
ALTER TABLE og.contract ADD CONSTRAINT contract_market_utility_check
    CHECK ((market = 'REGULATED') = (utility_id IS NOT NULL));

-- Widen service_type. This migration OWNS the full list (lead decision 2026-09-26; 0026 no longer
-- touches it). Every earlier value stays allowed. Added here: PIPELINE_AC (06 S4.a), REGULATED_CAPACITY
-- (08 S3), and PJM_CAPACITY, MOBILE_STORAGE, LARGE_LOAD (owner decision 2026-09-26). Mirrors
-- opengrid.core.models.engine.ServiceType.
ALTER TABLE og.contract DROP CONSTRAINT IF EXISTS contract_service_type_check;
ALTER TABLE og.contract ADD CONSTRAINT contract_service_type_check CHECK (service_type IN
    ('HOME', 'ERCOT_ENERGY', 'ERCOT_AS', 'DIST_DEFERRAL', 'PARTNER_CAPACITY', 'DATA_CENTER', 'PIPELINE_AC',
     'REGULATED_CAPACITY', 'PJM_CAPACITY', 'MOBILE_STORAGE', 'LARGE_LOAD'));

-- A regulated capacity contract is, by definition, in the regulated market.
ALTER TABLE og.contract DROP CONSTRAINT IF EXISTS contract_regulated_capacity_market_check;
ALTER TABLE og.contract ADD CONSTRAINT contract_regulated_capacity_market_check
    CHECK (service_type <> 'REGULATED_CAPACITY' OR market = 'REGULATED');

CREATE INDEX IF NOT EXISTS ix_contract_market ON og.contract (market, utility_id) WHERE status = 'ACTIVE';

CREATE TABLE IF NOT EXISTS og.asset (
    asset_id        text PRIMARY KEY,
    asset_class     text NOT NULL CHECK (asset_class IN ('HOME_BANK', 'SUBSTATION')),
    bank_id         text REFERENCES og.bank(bank_id),
    feeder_id       text,
    substation_id   text,
    zone            text NOT NULL,
    utility_id      text REFERENCES og.utility(utility_id),   -- territory; NULL = ERCOT competitive area
    p_kw            numeric(14,3) NOT NULL CHECK (p_kw > 0),
    e_kwh           numeric(14,3) NOT NULL CHECK (e_kwh > 0),
    eta_rt          numeric(6,4) NOT NULL CHECK (eta_rt > 0 AND eta_rt <= 1),
    floor_frac      numeric(6,4) NOT NULL DEFAULT 0.20 CHECK (floor_frac >= 0 AND floor_frac < 1),
    poi_import_kva  numeric(14,3) CHECK (poi_import_kva >= 0),
    poi_export_kva  numeric(14,3) CHECK (poi_export_kva >= 0),
    capex_usd       numeric(18,2) CHECK (capex_usd >= 0),
    status          text NOT NULL DEFAULT 'PLANNED' CHECK (status IN ('PLANNED', 'ACTIVE', 'RETIRED')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- A home-bank asset is its bank. A substation asset links to a bank or a feeder, and has an
    -- interconnection limit in BOTH directions (a missing POI row means zero dispatch, 09 G-29).
    CONSTRAINT asset_home_bank_link CHECK (asset_class <> 'HOME_BANK' OR bank_id IS NOT NULL),
    CONSTRAINT asset_substation_link CHECK (
        asset_class <> 'SUBSTATION'
        OR ((bank_id IS NOT NULL OR feeder_id IS NOT NULL)
            AND poi_import_kva IS NOT NULL AND poi_export_kva IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS ix_asset_territory ON og.asset (utility_id, asset_class);
CREATE INDEX IF NOT EXISTS ix_asset_zone ON og.asset (zone);
