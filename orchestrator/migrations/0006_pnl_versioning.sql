-- Settle idempotency fix (BUILD.md follow-up finding): og.pnl was created without the
-- version/superseded_by columns that 02a S1's insert-only-table rule requires for every table in its
-- list ("trace, verdict, stop_event, meter_interval, invoice_line, pnl, operator_action, ...").
-- meter_interval and invoice_line already carry version/supersedes columns; pnl did not, so
-- settle.pg_backend's app-level "existing_pnl is None or ... changed" check was the ONLY guard against
-- duplicate rows -- a check-then-insert race (two concurrent settle() calls for the same
-- obligation-interval, e.g. a manual re-run overlapping og-settle's own cycle) could still insert two
-- active pnl rows with nothing at the database level to prevent it.
--
-- This mirrors meter_interval's pattern exactly: a version counter, a forward-pointing
-- superseded_by column, and a partial unique index enforcing at most one ACTIVE
-- (superseded_by IS NULL) row per (obligation_id, interval_start) -- the database itself now refuses a
-- second concurrent "active" insert instead of relying solely on the read-then-write check in Python.

SET search_path TO og;

ALTER TABLE og.pnl ADD COLUMN version integer NOT NULL DEFAULT 1;
ALTER TABLE og.pnl ADD COLUMN superseded_by uuid REFERENCES og.pnl(pnl_id);

CREATE UNIQUE INDEX ux_pnl_active ON og.pnl(obligation_id, interval_start)
    WHERE superseded_by IS NULL;
