-- 0039: indexes for the Fleet table at scale (owner review R3, api `routers.fleet_search`). Additive
-- only. Keyset pages order by (sort key, hub_id), so each sortable column gets a (column, hub_id)
-- index; the typeahead and the hub-id filter use `lower(id) LIKE 'prefix%'`, which is an index range
-- scan on a `lower(id) text_pattern_ops` index regardless of the database collation. The tables are
-- small today (~2,500 hubs), so a plain (non-concurrent) build is fine; lock_timeout keeps a busy
-- table from blocking the migration run for long.

SET lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS hub_bank_hub_idx ON og.hub (bank_id, hub_id);
CREATE INDEX IF NOT EXISTS hub_zone_hub_idx ON og.hub (zone, hub_id);
CREATE INDEX IF NOT EXISTS hub_lower_hub_id_pattern_idx ON og.hub (lower(hub_id) text_pattern_ops);
CREATE INDEX IF NOT EXISTS hub_lower_bank_id_pattern_idx ON og.hub (lower(bank_id) text_pattern_ops);
CREATE INDEX IF NOT EXISTS bank_lower_bank_id_pattern_idx ON og.bank (lower(bank_id) text_pattern_ops);
CREATE INDEX IF NOT EXISTS hub_state_last_seen_hub_idx ON og.hub_state (last_seen_at, hub_id);
CREATE INDEX IF NOT EXISTS hub_state_p_kw_hub_idx ON og.hub_state (p_kw, hub_id);
