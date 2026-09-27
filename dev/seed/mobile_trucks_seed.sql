-- dev/seed/mobile_trucks_seed.sql -- the owner's truck fleet (MOBILE_STORAGE, D-31), 2026-09-26.
--
-- Eight truck-mounted batteries: 2 Austin, 2 San Antonio, 4 Dallas. Each truck is its own single-hub bank
-- (hub_id = truck id, bank_id = 'bank-<truck id>', its own depot feeder 'feeder-<truck id>'), rated
-- 1 MWh / 500 kW with a 20% floor (200 kWh) and eta_c = eta_d = 0.9487 (the home fleet's one-way figure).
-- ASSUMPTION: planning ratings for a realistic truck-mounted BESS, to be confirmed with Base.
--
-- Source of truth for the depots and the unit -> depot binding is
-- orchestrator/config/service_profiles/mobile_storage_home_stations.toml ([[home_station]] / [[assignment]]);
-- the sim side is integration-sims/config/fleet.yaml + scada.yaml `mobile_units:`. This file must match both
-- 1:1 (ids, zone, lat/lon) -- enforced by orchestrator/tests/unit/profiles/test_mobile_trucks_consistency.py.
--
-- Location: og.hub.lat/lon starts at the truck's HOME STATION (it is parked there, charging allowed). The
-- device-info intake (opengrid.fleet.device_info) moves it when the truck reports a new position, and the
-- guardian's G-35 compares it to the home station (D-31: a truck charges ONLY at its home station, at that
-- station's tariff; never from the fleet).
--
-- og.bank.zone is the home station's zone for display; the selector prices a mobile unit at its home
-- station's zone from the registry, never og.bank.zone. kva_rating 600 = the depot service/transformer
-- rating (above the 500 kW PCS / ~526 kVA at 0.95 pf). No og.asset row: og.asset's asset_class is
-- HOME_BANK | SUBSTATION only, and MOBILE is classified from the registry (api/routers/fleet_search.py).
--
-- units: migration 0032's insert trigger sets units = 2 for e_kwh >= 70 (a dual-unit HOME rule); a truck is
-- one PCS/battery unit, so the insert is followed by an explicit UPDATE ... SET units = 1.
--
-- Idempotent: ON CONFLICT ... DO UPDATE for og.bank/og.hub; og.hub_state is DO NOTHING (never clobber live
-- telemetry). Apply with: psql "$OG_DB_DSN" -v ON_ERROR_STOP=1 -f dev/seed/mobile_trucks_seed.sql

BEGIN;

SET search_path TO og;

CREATE TEMP TABLE _og_trucks (
    hub_id text PRIMARY KEY,
    zone   text NOT NULL,
    lat    double precision NOT NULL,
    lon    double precision NOT NULL
) ON COMMIT DROP;

INSERT INTO _og_trucks (hub_id, zone, lat, lon) VALUES
    ('truck-aus-01', 'LZ_SOUTH', 30.5195, -97.6480),  -- hs-aus-roundrock-01    Round Rock (Oncor)
    ('truck-aus-02', 'LZ_AEN',   30.2090, -97.6125),  -- hs-aus-sandhill-01     Sand Hill, Del Valle (Austin Energy)
    ('truck-sat-01', 'LZ_CPS',   29.5840, -98.3170),  -- hs-sat-selma-01        Selma / Schertz (CPS Energy)
    ('truck-sat-02', 'LZ_CPS',   29.3570, -98.5680),  -- hs-sat-leoncreek-01    Leon Creek, SW San Antonio (CPS Energy)
    ('truck-dfw-01', 'LZ_NORTH', 32.8385, -96.9730),  -- hs-dfw-irving-01       Irving (Oncor)
    ('truck-dfw-02', 'LZ_NORTH', 32.8920, -96.6560),  -- hs-dfw-garland-01      Garland (Oncor, assumed)
    ('truck-dfw-03', 'LZ_NORTH', 32.7905, -96.6150),  -- hs-dfw-mesquite-01     Mesquite (Oncor)
    ('truck-dfw-04', 'LZ_NORTH', 32.7630, -97.0560);  -- hs-dfw-grandprairie-01 Grand Prairie (Oncor)

INSERT INTO og.bank (bank_id, zone, kva_rating, reserve_kva, feeder_id)
SELECT 'bank-' || hub_id, zone, 600, 0, 'feeder-' || hub_id FROM _og_trucks
ON CONFLICT (bank_id) DO UPDATE SET
    zone = EXCLUDED.zone, kva_rating = EXCLUDED.kva_rating, feeder_id = EXCLUDED.feeder_id;

INSERT INTO og.hub (
    hub_id, bank_id, zone, e_kwh, r_kwh, p_kw, eta_c, eta_d, lat, lon, units,
    manufacturer, model, inverter_model
)
SELECT
    hub_id, 'bank-' || hub_id, zone, 1000, 200, 500, 0.9487, 0.9487, lat, lon, 1,
    'Base Power', 'Base Power Mobile BESS (Truck)', 'Base Power Mobile PCS'
FROM _og_trucks
ON CONFLICT (hub_id) DO UPDATE SET
    bank_id = EXCLUDED.bank_id, zone = EXCLUDED.zone, e_kwh = EXCLUDED.e_kwh, r_kwh = EXCLUDED.r_kwh,
    p_kw = EXCLUDED.p_kw, eta_c = EXCLUDED.eta_c, eta_d = EXCLUDED.eta_d, lat = EXCLUDED.lat,
    lon = EXCLUDED.lon, manufacturer = EXCLUDED.manufacturer, model = EXCLUDED.model,
    inverter_model = EXCLUDED.inverter_model;

-- 0032's trigger made these dual-unit (e_kwh >= 70); a truck is one unit.
UPDATE og.hub SET units = 1 WHERE hub_id IN (SELECT hub_id FROM _og_trucks) AND units <> 1;

INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, health, lease_epoch, last_seen_at)
SELECT hub_id, 200, 0, 'offline', 0, now() FROM _og_trucks
ON CONFLICT (hub_id) DO NOTHING;

COMMIT;

-- Verification (read-only):
--   SELECT hub_id, bank_id, zone, e_kwh, p_kw, units, lat, lon FROM og.hub WHERE hub_id LIKE 'truck-%' ORDER BY 1;
--   -- 8 rows, units 1, e_kwh 1000, p_kw 500
--   SELECT bank_id, zone, kva_rating, feeder_id FROM og.bank WHERE bank_id LIKE 'bank-truck-%' ORDER BY 1;  -- 8 rows
