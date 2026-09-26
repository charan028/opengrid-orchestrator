-- 0034: the hub's own split of its charging power between PV and grid (owner decision D-28, source 1 of
-- `opengrid.core.solar_share`). Additive and idempotent: nullable columns, no default, so a hub that does not
-- report the split leaves them NULL and every reader falls back to PV minus home load (migration 0027), then
-- ERCOT's solar share, then the 30% assumption.
--
-- og.telemetry is partitioned by range on ts (0001_init.sql, turned over by 0033's data lifecycle): ADD COLUMN
-- on the partitioned parent propagates to every partition. og.hub_state carries the same pair as the
-- latest-known snapshot. Ingest wiring (core/models, opengrid.fleet, fleet/pg_backend) belongs to live-path.
-- Read by opengrid.settle.pg_backend (M1 grid-charged kWh) and the selector's solar history.

ALTER TABLE og.telemetry ADD COLUMN IF NOT EXISTS charge_pv_kw DOUBLE PRECISION NULL;
ALTER TABLE og.telemetry ADD COLUMN IF NOT EXISTS charge_grid_kw DOUBLE PRECISION NULL;
ALTER TABLE og.hub_state ADD COLUMN IF NOT EXISTS charge_pv_kw DOUBLE PRECISION NULL;
ALTER TABLE og.hub_state ADD COLUMN IF NOT EXISTS charge_grid_kw DOUBLE PRECISION NULL;
