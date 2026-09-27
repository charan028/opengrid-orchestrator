-- R3.1: record the short-history firm-fitness relaxation in og.forecast itself (auditability).
--
-- opengrid.forecast writes firm_fitness = 'FIRM_POOLED' for a slot that is firm only because weekday and
-- weekend samples for its time-of-day were pooled while same-day-type history is short
-- ([forecast].pool_day_types_when_short). FIRM_POOLED is firm: every reader that treats FIRM_OK as firm
-- also accepts it (the selector withholds only NOT_FOR_FIRM series).
--
-- The CHECK is widened to a strict superset of 0003's list. Added NOT VALID (no scan under the ACCESS
-- EXCLUSIVE lock, which is held only for the catalog change), then VALIDATEd, which takes only a SHARE
-- UPDATE EXCLUSIVE lock. The table is small (about 1.5k rows). lock_timeout bounds any wait behind the
-- live forecast upsert.

SET LOCAL lock_timeout = '5s';

ALTER TABLE og.forecast DROP CONSTRAINT IF EXISTS forecast_firm_fitness_check;
ALTER TABLE og.forecast ADD CONSTRAINT forecast_firm_fitness_check
    CHECK (firm_fitness IN ('FIRM_OK', 'FIRM_POOLED', 'NOT_FOR_FIRM')) NOT VALID;
ALTER TABLE og.forecast VALIDATE CONSTRAINT forecast_firm_fitness_check;
