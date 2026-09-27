-- 0052: retention policy for the dispatch-call audit trail (D-33, r3.4.3). Additive only.
--
-- og.dispatch_call (the call ledger, migration 0047) and og.as_deployment (the deployments it references,
-- migration 0020) had no og.data_retention row. They are the record of every toll/AS call and its refusals
-- that settlement and disputes rely on, so they are kept like the other audit/settlement tables: mode NONE,
-- protected (the CHECK data_retention_protected_never_deleted then forbids any keep window). Neither table
-- is in opengrid.lifecycle.policy.DELETABLE_TABLES, so the lifecycle job never deletes from them anyway;
-- these rows make the policy explicit and visible (Maintenance screen, lifecycle status).

SET search_path TO og;

INSERT INTO og.data_retention (table_name, ts_column, mode, hot_days, keep_days, archive, protected, note) VALUES
    ('dispatch_call', 'created_at', 'NONE', NULL, NULL, false, true,
     'call ledger (D-33): every call and refusal, all origins; audit + toll settlement, never deleted'),
    ('as_deployment', 'created_at', 'NONE', NULL, NULL, false, true,
     'deployments (AS awards, utility tolls); referenced by dispatch_call; audit + settlement, never deleted')
ON CONFLICT (table_name) DO NOTHING;
