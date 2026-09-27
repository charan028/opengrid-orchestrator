-- 0043: what a battery REPORTS about its ratings and location, kept apart from the seed (H4 safety fix,
-- 2026-09-26). Additive, idempotent. Number confirmed by the release manager (0042 is UI-FLEET's index migration).
--
-- opengrid.fleet.device_info writes these columns only. The seed columns the fleet plans and signs with
-- (units, p_kw, e_kwh, r_kwh -- the K1 reserve floor -- and lat, lon) are never changed by a device report;
-- a report that differs from them raises ALR-DEVICE-RATING-MISMATCH instead.

ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS device_units smallint;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS device_rated_kw double precision;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS device_rated_kwh double precision;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS device_reserve_floor_pct double precision;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS device_lat double precision;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS device_lon double precision;

COMMENT ON COLUMN og.hub.device_reserve_floor_pct IS
    'Reserve floor percent the device reports (informational; the K1 floor is og.hub.r_kwh, never set from this).';
