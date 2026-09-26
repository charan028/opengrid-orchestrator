-- 0027: discharge-flow-limit telemetry fields (09-optimizer-dispatcher-update.md S1.9/G11 -- "Hub
-- telemetry has no meter net power, PV, cell temperature, BMS limits or peak budget"). Additive only:
-- every column is nullable with no default that changes existing row semantics, so a hub still on the
-- old wire schema (interfaces/mqtt/telemetry.schema.json's new fields are all optional) simply leaves
-- these NULL, and every existing reader that doesn't know about them is unaffected.
--
-- og.telemetry: one column per new wire field, for every historical sample (it is the time-series
-- table, PARTITION BY RANGE (ts), 0001_init.sql).
-- og.hub_state: the same set, as the latest-known snapshot (0001_init.sql) -- ingest overwrites these
-- on every telemetry message per hub, same as soc_kwh/p_kw today (see the ingest wiring note in the
-- FLEET-SIM build report for the exact UPDATE statement the release manager adds to
-- opengrid.fleet.__init__).
--
-- Owned by FLEET-SIM (lead's 2026-09-26 reassignment); ingest wiring into
-- orchestrator/src/opengrid/fleet/__init__.py belongs to the release manager (see the build report).
--
-- Also backfills og.hub.lat/lon (owner UI request, 2026-09-26, #19): those columns already exist
-- (0001_init.sql), just always NULL, because `opengrid.fleet.seed.build_topology` never set them
-- until this build. Every hub now gets a deterministic point (`opengrid.fleet.seed.hub_lat_lon`,
-- mirrored independently in `ogsim.fleet.state.hub_lat_lon`, BUILD.md S1) inside its load zone's real
-- geography; re-running `opengrid.fleet.seed`'s normal UPSERT after this migration will also set them
-- for any hub seeded before this build (its UPSERT now always writes lat/lon), so this backfill is
-- belt-and-suspenders for a hub that is never re-seeded.

ALTER TABLE og.telemetry
  ADD COLUMN IF NOT EXISTS home_load_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS pv_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS meter_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS cell_temp_c DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS p_dis_max_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS p_ch_max_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS peak_power_budget_kws DOUBLE PRECISION;

ALTER TABLE og.hub_state
  ADD COLUMN IF NOT EXISTS home_load_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS pv_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS meter_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS cell_temp_c DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS p_dis_max_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS p_ch_max_kw DOUBLE PRECISION,
  ADD COLUMN IF NOT EXISTS peak_power_budget_kws DOUBLE PRECISION;

-- Backfill lat/lon for every hub-NNNNN hub currently missing one, using the exact formula in
-- opengrid.fleet.seed.hub_lat_lon / ogsim.fleet.state.hub_lat_lon: a zone-center lookup plus a
-- two-constant linear-congruential jitter of the hub's own numeric index, +-0.175 degrees (half of
-- the 0.35-degree cluster spread) around that zone's center.
WITH hub_index AS (
    SELECT hub_id, zone, (substring(hub_id FROM 'hub-(\d+)'))::bigint AS idx
    FROM og.hub
    WHERE lat IS NULL OR lon IS NULL
),
centers AS (
    SELECT * FROM (VALUES
        ('LZ_NORTH',  32.7767,  -96.7970),
        ('LZ_HOUSTON', 29.7604, -95.3698),
        ('LZ_SOUTH',  29.4241,  -98.4936),
        ('LZ_WEST',   31.9973, -102.0779),
        ('LZ_AEN',    30.2672,  -97.7431),
        ('LZ_CPS',    29.4241,  -98.4936),
        ('LZ_LCRA',   30.5000,  -98.3000),
        ('LZ_RAYBN',  32.8700,  -95.7500)
    ) AS t(zone, center_lat, center_lon)
),
computed AS (
    SELECT
        hi.hub_id,
        coalesce(c.center_lat, 31.0) AS center_lat,
        coalesce(c.center_lon, -100.0) AS center_lon,
        ((hi.idx * 9301 + 49297) % 233280) / 233280.0 AS frac_lat,
        ((hi.idx * 134775813 + 40503) % 1000003) / 1000003.0 AS frac_lon
    FROM hub_index hi
    LEFT JOIN centers c ON c.zone = hi.zone
    WHERE hi.idx IS NOT NULL
)
UPDATE og.hub h
SET
    lat = computed.center_lat + (computed.frac_lat - 0.5) * 0.35,
    lon = computed.center_lon + (computed.frac_lon - 0.5) * 0.35
FROM computed
WHERE h.hub_id = computed.hub_id;
