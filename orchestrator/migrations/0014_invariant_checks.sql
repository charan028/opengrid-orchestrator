-- 0014: measured invariant-check results (00-invariants.md K1/K2/K13, plus orphan reservations/
-- commitments and scheduled trace-chain verification). `opengrid.invariants` is the sole writer.
-- Additive only (BUILD.md S1: "never renumber existing migrations"; 0012/0013 are taken).
--
-- Two tables:
--   * og.invariant_check    -- one row per named check: when it last ran, how long it took, how many
--                               violations it found THIS run, a running total, and its watermark (so a
--                               restarted process resumes an incremental scan instead of rescanning
--                               from the beginning). The API's measured counters and "time of last
--                               check" are a plain read of this table (api/routers/health.py,
--                               api/routers/dispatch.py).
--   * og.invariant_violation -- an append-only log of each individual violation found, for the
--                                billing/audit trail and operator drill-down; never updated in place.

CREATE TABLE IF NOT EXISTS og.invariant_check (
    check_name       text PRIMARY KEY,
    last_run_at      timestamptz,
    last_run_ms      integer,
    last_violations  integer NOT NULL DEFAULT 0,
    -- double precision, not an integer count: K2's total is a running sum of "kWh sold twice"
    -- (`opengrid.invariants.checks.find_double_sold`'s `Violation.magnitude`), which is fractional.
    -- Every other check's magnitude is 1.0 per violation, so its total is numerically a plain count.
    total_violations double precision NOT NULL DEFAULT 0,
    watermark        jsonb,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS og.invariant_violation (
    id           bigserial PRIMARY KEY,
    check_name   text NOT NULL,
    detected_at  timestamptz NOT NULL DEFAULT now(),
    scope        jsonb NOT NULL,
    detail       jsonb
);
CREATE INDEX IF NOT EXISTS ix_invariant_violation_check_time
    ON og.invariant_violation (check_name, detected_at DESC);
