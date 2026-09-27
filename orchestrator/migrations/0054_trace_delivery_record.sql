-- 0054: og.trace.decision_type accepts DELIVERY_RECORD (D-38, opengrid.delivery). Additive.
--
-- Live defect 2026-09-27 (prod rc3, 02:36): og-settle's delivery job traces each final delivery record as
-- decision_type DELIVERY_RECORD / event_class DELIVERY_VERIFICATION on stream `delivery`. 0041's CHECK list
-- lacks DELIVERY_RECORD, so the insert raised a CheckViolation and the row was quarantined
-- (ALR-TRACE-QUARANTINED). Its hash already covers DELIVERY_RECORD, so the type is added rather than the row
-- rewritten; the quarantined row can be replayed unchanged once this is applied.
--
-- Same pattern as 0041: a strict superset of 0041's list, added NOT VALID (no og.trace scan; the ACCESS
-- EXCLUSIVE lock is held only for the catalog change). New rows are checked either way.

SET LOCAL lock_timeout = '5s';

ALTER TABLE og.trace DROP CONSTRAINT IF EXISTS trace_decision_type_check;
ALTER TABLE og.trace ADD CONSTRAINT trace_decision_type_check CHECK (decision_type IN
    ('DA_PLAN','ID_PLAN','ADMISSION','COMMITMENT','RENOMINATION','RT_ALLOCATION',
     'SUBSTITUTION','GUARDIAN_VERDICT','SAFE_STOP','SHORTFALL','OPERATOR_ACTION',
     'FEED_CHANGE','ALERT','SETTLEMENT','ASSET_STATE_TRANSITION','CALIBRATION_ATTEMPT',
     'DATA_LIFECYCLE','AUTHZ_DENY','DELIVERY_RECORD')) NOT VALID;
