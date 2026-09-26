-- 0024: og.invoice_line is insert-only at the database layer (traceability gap ES08, issue #14 item 2).
-- Additive and idempotent (CREATE OR REPLACE FUNCTION, DROP TRIGGER IF EXISTS).
--
-- 02a S1.1 makes invoice_line an insert-only table: a correction is a NEW row whose `supersedes` column
-- points back at the line it corrects, never an UPDATE, and a billed line is never deleted. Until now
-- that rule lived only in Python (`opengrid.settle.billing` / `settle.pg_backend`, which only ever
-- SELECT and INSERT this table), so a stray UPDATE/DELETE from any other code path, a manual psql
-- session or a future bug would silently rewrite billing history. This migration makes Postgres
-- itself refuse it.
--
-- 1. og.invoice_line_reject_mutation(): raises SQLSTATE 42501 (insufficient_privilege) naming the
--    rejected operation, unless the escape hatch below is set.
-- 2. Trigger invoice_line_immutable: BEFORE UPDATE OR DELETE OR TRUNCATE, FOR EACH STATEMENT. Statement
--    level so it also fires when the WHERE clause matches no rows (a mutation attempt is refused as
--    such, not only when it happens to hit data) and so TRUNCATE is covered by the same trigger.
--
-- Escape hatch (explicit migrations only): a migration that must correct invoice_line data in place
-- runs, inside its own transaction (migrate_sync applies each file in one transaction):
--     SET LOCAL og.allow_invoice_line_mutation = 'on';
-- SET LOCAL ends with that transaction, so the setting never leaks into the app's pooled sessions.
-- Application code must never set it; a grep for `allow_invoice_line_mutation` outside
-- orchestrator/migrations/ is a review finding.
--
-- Why a trigger and not REVOKE: every environment (dev/docker-compose.yml, deploy/README.md) uses one
-- role, `opengrid`, which OWNS the og schema, runs the migrations and runs the app. An owner can GRANT
-- privileges back to itself, and a REVOKE would also block the migrations that are meant to be the only
-- exception. The trigger works regardless of role and is visible in the schema (`\d og.invoice_line`).
-- It is a guard against mistakes, not a security boundary against a hostile `opengrid` session (which
-- could set the hatch or drop the trigger); separating an owner role from the app role is future work.

SET search_path TO og;

CREATE OR REPLACE FUNCTION og.invoice_line_reject_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF current_setting('og.allow_invoice_line_mutation', true) = 'on' THEN
        RETURN NULL;
    END IF;
    RAISE EXCEPTION 'og.invoice_line is insert-only (ES08): % rejected', TG_OP
        USING ERRCODE = 'insufficient_privilege',
              HINT = 'Insert a correcting row whose supersedes column points at the original line.';
END;
$$;

COMMENT ON FUNCTION og.invoice_line_reject_mutation() IS
    'ES08: rejects UPDATE/DELETE/TRUNCATE on og.invoice_line unless a migration has run '
    'SET LOCAL og.allow_invoice_line_mutation = ''on'' (see migration 0024).';

DROP TRIGGER IF EXISTS invoice_line_immutable ON og.invoice_line;
CREATE TRIGGER invoice_line_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON og.invoice_line
    FOR EACH STATEMENT EXECUTE FUNCTION og.invoice_line_reject_mutation();
