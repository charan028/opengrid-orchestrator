-- 0051: retention of og.delivery_record (D-38, 2026-09-27). Additive, idempotent.
--
-- The per-call summary (result, reasons, delivered vs committed kWh, meter check) is kept like the call it
-- verifies: og.as_deployment and og.dispatch_call have no deletion policy, so neither has this. The row is
-- registered with mode NONE (never deleted by opengrid.lifecycle; not in its DELETABLE_TABLES), not
-- `protected`, so an operator can still set a keep_days later.
--
-- The per-bucket `series` jsonb (the bulk of the row) is pruned sooner, by og-settle's delivery job after
-- [delivery].series_keep_days (default 60 d, the same horizon as og.grant / og.command_batch it is derived
-- from): `series` becomes '[]' and `series_pruned_at` records when. The summary columns are untouched.

SET search_path TO og;

ALTER TABLE og.delivery_record ADD COLUMN IF NOT EXISTS series_pruned_at timestamptz;

CREATE INDEX IF NOT EXISTS ix_delivery_record_series_prune ON og.delivery_record (window_end)
    WHERE final AND series_pruned_at IS NULL;

INSERT INTO og.data_retention (table_name, ts_column, mode, hot_days, keep_days, archive, protected, note)
VALUES (
    'delivery_record', 'window_start', 'NONE', NULL, NULL, false, false,
    'per-call delivery verification (D-38): summary kept like og.as_deployment; the per-bucket series is '
    'pruned by the delivery job after [delivery].series_keep_days'
)
ON CONFLICT (table_name) DO NOTHING;
