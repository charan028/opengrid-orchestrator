-- Adds the `forecast` table (02b-mvp-s-spec-platform.md S3), owned solely by opengrid.forecast.
--
-- OPEN ISSUE (flagged for the architect): BUILD.md S1 READ FIRST list points at
-- "migrations/0001_init.sql (the forecast table; do not edit it)", but 0001_init.sql as delivered has
-- no `forecast` table (it covers engine tables + the platform fleet/health/feeds tables only -- see its
-- own section 7 comment). Rather than block on that mismatch, this is a new forward-only migration
-- (BUILD.md S5a, 02b S9.6) adding exactly the DDL 02b S3 specifies, under the `og` schema for
-- consistency with every other MVP-S table. Please fold into 0001 or rename/renumber if you'd rather
-- keep a single init script -- nothing downstream depends on the filename, only on `og.forecast`
-- existing with this shape.

SET search_path TO og;

CREATE TABLE og.forecast (
  id                  BIGSERIAL PRIMARY KEY,
  series_key          TEXT NOT NULL,          -- price hub (e.g. 'HB_HUBAVG') or weather zone (e.g. 'LZ_SOUTH')
  kind                TEXT NOT NULL CHECK (kind IN ('price', 'load')),
  interval_start_utc  TIMESTAMPTZ NOT NULL,
  horizon_step        SMALLINT NOT NULL CHECK (horizon_step BETWEEN 0 AND 95),
  p10                 DOUBLE PRECISION NOT NULL,
  p50                 DOUBLE PRECISION NOT NULL,
  p90                 DOUBLE PRECISION NOT NULL,
  firm_fitness        TEXT NOT NULL DEFAULT 'FIRM_OK' CHECK (firm_fitness IN ('FIRM_OK', 'NOT_FOR_FIRM')),
  computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
  CONSTRAINT ck_forecast_quantile_order CHECK (p10 <= p50 AND p50 <= p90),
  UNIQUE (series_key, kind, interval_start_utc)
);
CREATE INDEX ix_forecast_series_kind_interval ON og.forecast (series_key, kind, interval_start_utc);
