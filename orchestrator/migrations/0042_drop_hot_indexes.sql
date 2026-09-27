-- 0042: drop 0039's two og.hub_state indexes. og.hub_state is rewritten for every hub on every
-- telemetry flush (fillfactor 70 so updates stay HOT); an index on p_kw or last_seen_at -- both change on
-- every update -- forces a non-HOT update and index maintenance on the hottest table. The Fleet table
-- sorts by P and telemetry age with a scan and a top-N sort of the filtered set instead (~2,500 rows).
-- Additive-safe: IF EXISTS, and lock_timeout so a busy table never blocks the migration run for long.

SET lock_timeout = '5s';

DROP INDEX IF EXISTS og.hub_state_last_seen_hub_idx;
DROP INDEX IF EXISTS og.hub_state_p_kw_hub_idx;
