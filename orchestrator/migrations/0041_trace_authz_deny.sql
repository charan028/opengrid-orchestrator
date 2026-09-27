-- 0041: og.trace.decision_type accepts AUTHZ_DENY (opengrid.authz.enforce.audit_deny). Additive.
--
-- Live defect 2026-09-26: 0033's CHECK list lacks AUTHZ_DENY, so every audited deny raised a CheckViolation.
-- The trace backend journals any failed insert as a DB outage, and journal replay stops at the first row
-- that keeps failing -- one audited deny therefore blocked trace replay for the whole process.
--
-- The new list is a strict superset of 0033's (every existing row already satisfies it), so it is added
-- NOT VALID: no scan of og.trace, and the ACCESS EXCLUSIVE lock is held only for the catalog change.
-- It is deliberately NOT validated here: the migration runner applies each file in ONE transaction, and a
-- VALIDATE in the same transaction would keep that ACCESS EXCLUSIVE lock for the whole og.trace scan,
-- stalling every trace writer. New rows are checked either way; a VALIDATE, if wanted, belongs in its own
-- later file (it then takes only SHARE UPDATE EXCLUSIVE, which does not block writers).

SET LOCAL lock_timeout = '5s';

ALTER TABLE og.trace DROP CONSTRAINT IF EXISTS trace_decision_type_check;
ALTER TABLE og.trace ADD CONSTRAINT trace_decision_type_check CHECK (decision_type IN
    ('DA_PLAN','ID_PLAN','ADMISSION','COMMITMENT','RENOMINATION','RT_ALLOCATION',
     'SUBSTITUTION','GUARDIAN_VERDICT','SAFE_STOP','SHORTFALL','OPERATOR_ACTION',
     'FEED_CHANGE','ALERT','SETTLEMENT','ASSET_STATE_TRANSITION','CALIBRATION_ATTEMPT',
     'DATA_LIFECYCLE','AUTHZ_DENY')) NOT VALID;
