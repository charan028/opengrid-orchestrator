-- 0033: data lifecycle -- daily partitions, retention policy, cold-tier manifests, 1-min/15-min rollups
-- (DATA-LIFECYCLE lane, 2026-09-26; docs/orchestrator/07-delivery/15-data-lifecycle.md). Plain PostgreSQL 17:
-- native declarative partitioning and plpgsql, no TimescaleDB.
--
-- Additive, idempotent (every statement re-runs cleanly) and catalog-cheap on a live database: this file
-- creates tables, functions and one view, seeds policy rows and sets per-table storage parameters. It
-- deliberately does NOT
--   * create telemetry day partitions -- with rows in og.telemetry_default, every CREATE ... PARTITION OF
--     scans the default partition under an ACCESS EXCLUSIVE lock on og.telemetry. `opengrid.lifecycle` first
--     swaps the populated default out (constraint validated without blocking ingest, then a metadata-only
--     detach/attach), then calls og.ensure_telemetry_partitions() against an empty default;
--   * build indexes on existing high-volume tables -- `opengrid.lifecycle` builds them with
--     CREATE INDEX CONCURRENTLY (not allowed inside this migration's transaction).
--
-- Ownership: og.data_retention and everything named lifecycle_* belong to `opengrid.lifecycle`. Trace
-- pruning is NOT here: og.trace keeps its own checkpoint-aware pruning (og.retention_policy,
-- opengrid.trace.pg_backend.run_retention_prune_job, K11).
--
-- Live-DB safety: a lock this file needs (og.trace, og.hub_state, ...) that is not granted within 10 s
-- fails the migration cleanly (the runner rolls the whole file back) instead of queueing writers behind it;
-- simply re-run it.

SET LOCAL lock_timeout = '10s';

-- =====================================================================================================
-- 1. Retention policy per table (06 S4.4 node profile, register R9).
--    mode PARTITION: daily range partitions, whole partitions detached and dropped after keep_days.
--    mode DELETE:    batched DELETEs (<= [lifecycle].batch_rows rows per statement, one short txn each).
--    mode NONE:      never deleted by the lifecycle job.
--    hot_days:       a closed day older than hot_days is exported to the cold tier ahead of its drop
--                    (NULL = export only when the day is dropped). keep_days NULL = kept forever.
--    archive:        export each day to /srv/ogbackup/cold/<table>/<yyyy>/<mm>/<dd>.csv.<codec> with a
--                    manifest, verified by checksum before any drop; a failed export blocks the drop.
--    legal_hold:     blocks every deletion for the table (export still runs).
--    protected:      settlement/billing/audit records; the CHECK makes a deletion policy impossible.
-- =====================================================================================================

CREATE TABLE IF NOT EXISTS og.data_retention (
    table_name   text PRIMARY KEY,
    ts_column    text NOT NULL,
    mode         text NOT NULL CHECK (mode IN ('PARTITION', 'DELETE', 'NONE')),
    hot_days     integer CHECK (hot_days IS NULL OR hot_days >= 0),
    keep_days    integer CHECK (keep_days IS NULL OR keep_days >= 1),
    archive      boolean NOT NULL DEFAULT true,
    legal_hold   boolean NOT NULL DEFAULT false,
    protected    boolean NOT NULL DEFAULT false,
    note         text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT data_retention_protected_never_deleted
        CHECK (NOT protected OR (keep_days IS NULL AND mode = 'NONE')),
    CONSTRAINT data_retention_hot_within_keep
        CHECK (hot_days IS NULL OR keep_days IS NULL OR hot_days <= keep_days)
);

INSERT INTO og.data_retention (table_name, ts_column, mode, hot_days, keep_days, archive, protected, note) VALUES
    ('telemetry',             'ts',           'PARTITION', 1,    7,    true,  false, 'raw hub telemetry (06 S4.4: 7 d on the node)'),
    ('telemetry_1m',          'bucket',       'PARTITION', 1,    60,   true,  false, '1-min per-hub rollup, M&V source after raw ages out (<= 60 d cap)'),
    ('telemetry_15m',         'bucket',       'DELETE',    1,    NULL, true,  false, '15-min per-hub rollup, kept'),
    ('command_batch',         'created_at',   'DELETE',    NULL, 60,   true,  false, 'signed command batches + their og.verdict rows (<= 60 d cap)'),
    ('command_ack',           'ts',           'DELETE',    NULL, 60,   true,  false, 'hub command acknowledgements (command events)'),
    ('grant',                 'created_at',   'DELETE',    NULL, 60,   true,  false, 'per-cycle allocator grants'),
    ('pq_waveform_summary',   'ts',           'DELETE',    NULL, 30,   true,  false, 'PQ waveform summaries'),
    ('pq_waveform_raw_index', 'ts',           'DELETE',    NULL, 14,   false, false, 'PQ raw capture index + blob files; retain_until > now() is kept'),
    ('feed_obs',              'ts',           'DELETE',    NULL, 30,   true,  false, 'market/SCADA observations'),
    ('plan_energy_value',     'created_at',   'DELETE',    NULL, 60,   false, false, 'selector water values (derivable analytics)'),
    ('lifecycle_run',         'started_at',   'DELETE',    NULL, 90,   false, false, 'lifecycle job run log'),
    ('invoice_line',          'created_at',   'NONE',      NULL, NULL, false, true,  'billing: never deleted; monthly write-once export'),
    ('pnl',                   'created_at',   'NONE',      NULL, NULL, false, true,  'billing: never deleted; monthly write-once export'),
    ('meter_interval',        'created_at',   'NONE',      NULL, NULL, false, true,  'settlement M&V: never deleted'),
    ('performance',           'created_at',   'NONE',      NULL, NULL, false, true,  'settlement performance: never deleted'),
    ('trace',                 'created_at',   'NONE',      NULL, NULL, false, true,  'audit: own checkpoint-aware pruning (og.retention_policy, K11)')
ON CONFLICT (table_name) DO NOTHING;

-- =====================================================================================================
-- 2. Lifecycle bookkeeping: rollup watermarks, cold-tier manifest registry, run log.
-- =====================================================================================================

CREATE TABLE IF NOT EXISTS og.lifecycle_watermark (
    name        text PRIMARY KEY,
    upto        timestamptz NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

-- One row per exported day (archive_name = table, or 'verdict' / 'billing_invoice_line' / 'billing_pnl';
-- day = the UTC day, or the first day of the month for the monthly billing export). Files are write-once:
-- a re-export (late rows) gets a new revision file, this row points at the latest one.
CREATE TABLE IF NOT EXISTS og.lifecycle_archive (
    archive_name  text NOT NULL,
    day           date NOT NULL,
    path          text NOT NULL,
    manifest_path text NOT NULL,
    codec         text NOT NULL CHECK (codec IN ('zstd', 'gzip')),
    row_count     bigint NOT NULL,
    min_ts        timestamptz,
    max_ts        timestamptz,
    sha256        text NOT NULL,
    bytes         bigint NOT NULL,
    revision      integer NOT NULL DEFAULT 1,
    created_at    timestamptz NOT NULL DEFAULT now(),
    verified_at   timestamptz,
    PRIMARY KEY (archive_name, day)
);

CREATE TABLE IF NOT EXISTS og.lifecycle_run (
    run_id       uuid PRIMARY KEY,
    cycle        text NOT NULL CHECK (cycle IN ('fast', 'hourly')),
    started_at   timestamptz NOT NULL DEFAULT now(),
    finished_at  timestamptz,
    ok           boolean,
    summary      jsonb
);
CREATE INDEX IF NOT EXISTS ix_lifecycle_run_started ON og.lifecycle_run (started_at DESC);

-- =====================================================================================================
-- 3. Rollups (M&V source once raw telemetry ages out). Built incrementally by `opengrid.lifecycle.rollup`
--    from og.telemetry with a watermark, idempotent via UPSERT. p_kw > 0 is charging (energy in), p_kw < 0
--    is discharging (energy out) -- settle's metering convention. energy_*_kwh = the minute's mean
--    charging/discharging kW x 1/60 h (the same per-hub per-minute averaging settle meters with).
-- =====================================================================================================

CREATE TABLE IF NOT EXISTS og.telemetry_1m (
    hub_id          text NOT NULL,
    bucket          timestamptz NOT NULL,
    p_kw_avg        double precision,
    p_kw_min        double precision,
    p_kw_max        double precision,
    soc_kwh_last    double precision,
    energy_in_kwh   double precision NOT NULL DEFAULT 0,
    energy_out_kwh  double precision NOT NULL DEFAULT 0,
    sample_count    integer NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (hub_id, bucket)
) PARTITION BY RANGE (bucket);

CREATE TABLE IF NOT EXISTS og.telemetry_1m_default PARTITION OF og.telemetry_1m DEFAULT;

CREATE TABLE IF NOT EXISTS og.telemetry_15m (
    hub_id          text NOT NULL,
    bucket          timestamptz NOT NULL,
    p_kw_avg        double precision,
    p_kw_min        double precision,
    p_kw_max        double precision,
    soc_kwh_last    double precision,
    energy_in_kwh   double precision NOT NULL DEFAULT 0,
    energy_out_kwh  double precision NOT NULL DEFAULT 0,
    sample_count    integer NOT NULL,
    computed_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (hub_id, bucket)
);
CREATE INDEX IF NOT EXISTS ix_telemetry_15m_bucket_brin ON og.telemetry_15m USING brin (bucket);

-- =====================================================================================================
-- 4. Partition catalog view and the daily-partition function.
-- =====================================================================================================

CREATE OR REPLACE FUNCTION og.lifecycle_partition_column(p_parent text) RETURNS text
LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE p_parent WHEN 'telemetry' THEN 'ts' WHEN 'telemetry_1m' THEN 'bucket' END
$$;

-- Every partition of an og.* partitioned table with its range bounds (NULL lower = MINVALUE).
CREATE OR REPLACE VIEW og.lifecycle_partitions AS
SELECT
    p.relname::text AS parent,
    c.relname::text AS partition,
    b.expr = 'DEFAULT' AS is_default,
    CASE WHEN m[1] IS NULL OR m[1] = 'MINVALUE' THEN NULL ELSE m[1]::timestamptz END AS lower_ts,
    CASE WHEN m[2] IS NULL OR m[2] = 'MAXVALUE' THEN NULL ELSE m[2]::timestamptz END AS upper_ts,
    pg_total_relation_size(c.oid) AS total_bytes,
    greatest(c.reltuples, 0)::bigint AS est_rows
FROM pg_inherits i
JOIN pg_class c ON c.oid = i.inhrelid
JOIN pg_class p ON p.oid = i.inhparent AND p.relkind = 'p'
JOIN pg_namespace n ON n.oid = p.relnamespace AND n.nspname = 'og'
CROSS JOIN LATERAL (SELECT pg_get_expr(c.relpartbound, c.oid) AS expr) b
CROSS JOIN LATERAL (
    SELECT regexp_match(b.expr, 'FROM \(''?([^'')]*)''?\) TO \(''?([^'')]*)''?\)') AS m
) r(m);

-- Creates the UTC-day partitions [today - p_days_back, today + p_days_ahead] of a lifecycle-partitioned
-- table, each with a BRIN index on the partition column (the parent's PRIMARY KEY is inherited) and
-- append-only autovacuum settings. Returns the number created, or -1 when the default partition holds
-- rows (creating a partition would scan it under an ACCESS EXCLUSIVE lock on the parent: run the default
-- swap in `opengrid.lifecycle.partitions` first). Days already covered by another partition (the legacy
-- range partition left by a swap) are skipped. Idempotent; lock_timeout 5 s per statement.
CREATE OR REPLACE FUNCTION og.lifecycle_ensure_daily_partitions(
    p_parent text, p_days_back integer, p_days_ahead integer
) RETURNS integer
LANGUAGE plpgsql AS $$
DECLARE
    v_col      text := og.lifecycle_partition_column(p_parent);
    v_default  text := p_parent || '_default';
    v_today    date := (now() AT TIME ZONE 'UTC')::date;
    v_day      date;
    v_name     text;
    v_has_rows boolean := false;
    v_created  integer := 0;
BEGIN
    IF v_col IS NULL THEN
        RAISE EXCEPTION 'lifecycle: og.% is not a lifecycle-partitioned table', p_parent;
    END IF;
    PERFORM set_config('lock_timeout', '5s', true);
    IF to_regclass(format('og.%I', v_default)) IS NOT NULL THEN
        EXECUTE format('SELECT EXISTS (SELECT 1 FROM og.%I)', v_default) INTO v_has_rows;
        IF v_has_rows THEN
            RAISE NOTICE 'lifecycle: og.% holds rows; swap the default partition first', v_default;
            RETURN -1;
        END IF;
    END IF;
    FOR v_day IN
        SELECT d::date FROM generate_series(v_today - p_days_back, v_today + p_days_ahead, interval '1 day') AS d
    LOOP
        v_name := p_parent || '_p' || to_char(v_day, 'YYYYMMDD');
        CONTINUE WHEN to_regclass(format('og.%I', v_name)) IS NOT NULL;
        BEGIN
            EXECUTE format(
                'CREATE TABLE og.%I PARTITION OF og.%I FOR VALUES FROM (%L) TO (%L) WITH ('
                'autovacuum_vacuum_scale_factor = 0.05, autovacuum_vacuum_insert_scale_factor = 0.05, '
                'autovacuum_analyze_scale_factor = 0.02)',
                v_name, p_parent,
                v_day::timestamp AT TIME ZONE 'UTC', (v_day + 1)::timestamp AT TIME ZONE 'UTC');
            EXECUTE format('CREATE INDEX %I ON og.%I USING brin (%I)', v_name || '_brin', v_name, v_col);
            v_created := v_created + 1;
        EXCEPTION WHEN invalid_object_definition THEN
            -- 42P17 "would overlap partition": this day is inside the legacy range partition.
            NULL;
        END;
    END LOOP;
    RETURN v_created;
END
$$;

CREATE OR REPLACE FUNCTION og.ensure_telemetry_partitions(days_ahead integer DEFAULT 7) RETURNS integer
LANGUAGE sql AS $$
    SELECT og.lifecycle_ensure_daily_partitions('telemetry', 1, days_ahead)
$$;

-- =====================================================================================================
-- 5. Per-table autovacuum for the hot tables (item 6). Storage parameters only: SHARE UPDATE EXCLUSIVE,
--    no rewrite. hub_state/heartbeat are tiny and rewritten every 2 s, so vacuum them by a fixed dead-row
--    count, not a fraction, and leave page room for HOT updates (fillfactor applies to new pages).
--    Append-mostly tables vacuum on inserts (visibility map for index-only scans) and analyze often so
--    time-range plans see today's rows.
-- =====================================================================================================

ALTER TABLE og.hub_state SET (
    fillfactor = 70, autovacuum_vacuum_scale_factor = 0.0, autovacuum_vacuum_threshold = 5000,
    autovacuum_analyze_scale_factor = 0.0, autovacuum_analyze_threshold = 5000, autovacuum_vacuum_cost_delay = 0
);
ALTER TABLE og.heartbeat SET (
    fillfactor = 70, autovacuum_vacuum_scale_factor = 0.0, autovacuum_vacuum_threshold = 200,
    autovacuum_vacuum_cost_delay = 0
);
ALTER TABLE og.telemetry_default SET (
    autovacuum_vacuum_scale_factor = 0.05, autovacuum_vacuum_insert_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
ALTER TABLE og.telemetry_1m_default SET (autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE og.telemetry_15m SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE og.feed_obs SET (
    autovacuum_vacuum_scale_factor = 0.05, autovacuum_vacuum_insert_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
ALTER TABLE og.command_ack SET (
    autovacuum_vacuum_scale_factor = 0.05, autovacuum_vacuum_insert_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
ALTER TABLE og.command_batch SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE og.verdict SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);
ALTER TABLE og."grant" SET (
    autovacuum_vacuum_scale_factor = 0.05, autovacuum_vacuum_insert_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
ALTER TABLE og.pq_waveform_summary SET (
    autovacuum_vacuum_scale_factor = 0.05, autovacuum_vacuum_insert_scale_factor = 0.05,
    autovacuum_analyze_scale_factor = 0.02
);
ALTER TABLE og.pq_waveform_raw_index SET (autovacuum_vacuum_scale_factor = 0.05, autovacuum_analyze_scale_factor = 0.02);

-- =====================================================================================================
-- 6. Trace: lifecycle runs are traced (event_class 'lifecycle', decision_type 'DATA_LIFECYCLE').
--    The decision_type CHECK is widened to a strict superset of 0011's list. NOT VALID: every existing row
--    already satisfies the narrower list, so no scan of og.trace is needed; new rows are checked. Kept last
--    so the ACCESS EXCLUSIVE lock on og.trace is held for the shortest time.
-- =====================================================================================================

INSERT INTO og.retention_policy (event_class, retention_days, prune_after_checkpoint) VALUES
    ('lifecycle', 1825, true)
ON CONFLICT (event_class) DO NOTHING;

ALTER TABLE og.trace DROP CONSTRAINT IF EXISTS trace_decision_type_check;
ALTER TABLE og.trace ADD CONSTRAINT trace_decision_type_check CHECK (decision_type IN
    ('DA_PLAN','ID_PLAN','ADMISSION','COMMITMENT','RENOMINATION','RT_ALLOCATION',
     'SUBSTITUTION','GUARDIAN_VERDICT','SAFE_STOP','SHORTFALL','OPERATOR_ACTION',
     'FEED_CHANGE','ALERT','SETTLEMENT','ASSET_STATE_TRANSITION','CALIBRATION_ATTEMPT',
     'DATA_LIFECYCLE')) NOT VALID;
