-- 0017: adversarial-review fixes to og.invariant_violation / trace-verify watermarking (additive only,
-- BUILD.md S1: "never renumber existing migrations"; 0015 (customer_services) and 0016
-- (calibration_command) are taken by other agents' in-flight work; 0017 is the next free number as of
-- this file's creation.
--
-- (a) og.invariant_violation gains a `dedupe_key` so `opengrid.invariants.queries.insert_violations` can
--     upsert on (check_name, dedupe_key) instead of blindly appending every run -- a still-true
--     condition (e.g. K2's oversold bank/interval, re-scanned every 60s) is then the SAME row, not
--     counted and stored again (verified live: one over-sale was recounted ~120x/hour under the old
--     unkeyed insert). Existing rows are backfilled with their own `id` as a unique placeholder key so
--     the NOT NULL + unique constraint can be added without deleting any audit history.
--
-- (b) A new table, og.invariant_trace_watermark, holds K11 trace verification's PER-STREAM resume
--     point as one row per stream, replacing a single jsonb blob keyed by stream_id on
--     og.invariant_check -- streams are also then discoverable incrementally (the TRACE_VERIFY check's
--     own `og.invariant_check.watermark` keeps just a scalar `discovery_since` cursor instead of a full
--     `SELECT DISTINCT stream_id` scan every run).

ALTER TABLE og.invariant_violation ADD COLUMN IF NOT EXISTS dedupe_key text;
UPDATE og.invariant_violation SET dedupe_key = id::text WHERE dedupe_key IS NULL;
ALTER TABLE og.invariant_violation ALTER COLUMN dedupe_key SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_invariant_violation_dedupe
    ON og.invariant_violation (check_name, dedupe_key);

CREATE TABLE IF NOT EXISTS og.invariant_trace_watermark (
    stream_id      text PRIMARY KEY,
    next_from_seq  bigint NOT NULL DEFAULT 0,
    updated_at     timestamptz NOT NULL DEFAULT now()
);
