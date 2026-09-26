-- Settle idempotency fix, part 2 (BUILD.md follow-up finding): a correction (re-settling an
-- obligation-interval whose telemetry changed) needs to atomically "retire the old active row, add
-- the new one" for `og.meter_interval` (and now `og.pnl`, migration 0006). Both tables have a partial
-- unique index enforcing at most one row with `superseded_by IS NULL` per (obligation_id,
-- interval_start) -- correct, that IS the idempotency guard. But `settle.pg_backend` previously ran
-- the INSERT of the new row before the UPDATE that retires the old one (in two separate
-- transactions, no less), so at INSERT time the old row was still active and the new row's insert
-- immediately violated the partial unique index (`ux_meter_active`/`ux_pnl_active`).
--
-- The fix is to do it the other way around -- UPDATE the old row's `superseded_by` to the new row's
-- (already-generated, client-side) id FIRST, then INSERT the new row, in ONE transaction -- but that
-- means the UPDATE momentarily points `superseded_by` at a row that does not exist yet. Making the
-- self-referencing FK DEFERRABLE INITIALLY DEFERRED defers its existence check to COMMIT, by which
-- time the new row has been inserted, so both operations succeed together or not at all.

SET search_path TO og;

ALTER TABLE og.meter_interval DROP CONSTRAINT meter_interval_superseded_by_fkey;
ALTER TABLE og.meter_interval
    ADD CONSTRAINT meter_interval_superseded_by_fkey
    FOREIGN KEY (superseded_by) REFERENCES og.meter_interval(meter_interval_id)
    DEFERRABLE INITIALLY DEFERRED;

ALTER TABLE og.pnl DROP CONSTRAINT pnl_superseded_by_fkey;
ALTER TABLE og.pnl
    ADD CONSTRAINT pnl_superseded_by_fkey
    FOREIGN KEY (superseded_by) REFERENCES og.pnl(pnl_id)
    DEFERRABLE INITIALLY DEFERRED;
