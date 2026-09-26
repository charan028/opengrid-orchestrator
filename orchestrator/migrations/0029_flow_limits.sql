-- 0029: static premise and distribution limits the guardian's discharge-flow checks read
-- (09-optimizer-dispatcher-update.md S1.9, S2.6: G-26 home meter, G-27 service transformer, G-28 feeder,
-- G-29 substation, G-31 peak). Additive only: nullable columns and new tables. A NULL premise column means
-- "not on file": the guardian falls back to its configured static default ([guardian.flow]), and an
-- explicitly unknown default fails closed.

SET search_path TO og;

-- Per premise (the interconnection agreement / install record).
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS export_limit_kw double precision CHECK (export_limit_kw >= 0);  -- X_exp
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS service_kw      double precision CHECK (service_kw > 0);       -- S_svc
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS pv_rated_kw     double precision CHECK (pv_rated_kw >= 0);
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS peak_kw         double precision CHECK (peak_kw > 0);          -- P_pk
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS tau_peak_s      double precision CHECK (tau_peak_s > 0);
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS transformer_id  text;  -- NULL: unmapped (group of one)

-- Service transformers (utility GIS/AMI: transformer to meter). Members are the og.hub rows naming it.
CREATE TABLE IF NOT EXISTS og.service_transformer (
    transformer_id text PRIMARY KEY,
    bank_id        text REFERENCES og.bank(bank_id),
    rating_kva     double precision NOT NULL CHECK (rating_kva > 0),
    created_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_hub_transformer ON og.hub (transformer_id) WHERE transformer_id IS NOT NULL;

-- Feeder thermal limit and allowed feeder-head reverse flow (G-28). No row: the configured default.
CREATE TABLE IF NOT EXISTS og.feeder_limit (
    feeder_id  text PRIMARY KEY,
    thermal_kw double precision CHECK (thermal_kw > 0),
    reverse_kw double precision CHECK (reverse_kw >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Substation transformer limits (G-29). Membership: og.asset.substation_id (migration 0025). A substation
-- with members and no row here has unknown limits: any increase is vetoed.
CREATE TABLE IF NOT EXISTS og.substation_limit (
    substation_id text PRIMARY KEY,
    rating_kva    double precision CHECK (rating_kva > 0),
    reverse_kw    double precision CHECK (reverse_kw >= 0),
    updated_at    timestamptz NOT NULL DEFAULT now()
);
