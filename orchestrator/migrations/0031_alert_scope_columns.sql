-- 0031: structured scope_kind/scope_ref columns on og.alert (additive). Renumbered from 0024, which
-- collided with Frank's 0024_invoice_line_immutable.sql (PR #15). This migration is idempotent
-- (IF NOT EXISTS throughout), so it re-applying under the new filename on a workspace where it already
-- ran as 0024 is harmless.
--
-- Every ALR-* alert already carries a scope in its JSONB `detail` under inconsistent key names (guardian's
-- ALR-SCOPE-CONSERVATIVE/ALR-SAFE-STOP-REQUESTED already use "scope_kind"/"scope_ref"; health's own rules
-- use "process"/"zone"/"bank_id"/"source"+"product" -- see opengrid.health.queries.condition_key_for's
-- fallback chain). The UI has to parse the alert's `summary` TEXT to recover which bank/zone/process an
-- alert is about. These columns are the real, queryable, structured form; `detail` is unchanged and still
-- carries the rule-specific fields.
--
-- opengrid.health.queries.raise_alert (the single og.alert writer, used directly by health's own rules
-- and by guardian's PgAlertPort -- opengrid.guardian.repo.PgAlertPort.raise_alert calls it exactly the
-- same way) populates these from AlertFinding.detail's "scope_kind"/"scope_ref" keys when present.

ALTER TABLE og.alert
    ADD COLUMN IF NOT EXISTS scope_kind TEXT,
    ADD COLUMN IF NOT EXISTS scope_ref TEXT;

CREATE INDEX IF NOT EXISTS ix_alert_scope ON og.alert (scope_kind, scope_ref) WHERE cleared_at IS NULL;
