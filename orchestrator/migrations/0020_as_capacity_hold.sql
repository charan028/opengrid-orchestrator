-- 0020: ERCOT_AS is a CAPACITY HOLD, not a continuous discharge (lead decision 2026-09-26, NPRR1282).
-- Additive only.
--
-- 1. og.as_deployment: when ERCOT deploys an AS award. An award discharges (up to its committed kW) only
--    while a deployment covers now; otherwise the allocator grants it 0 kW and keeps its reservation
--    locked (K13). `obligation_id` NULL deploys every ERCOT_AS award. Written by the operator API
--    (`POST /og/api/dispatch/as-deployments`, the demo trigger) or a market/scenario source; ending one
--    early sets `cancelled_at` (never a delete: the deployment log is part of the audit trail).
-- 2. Seed fix: the demo Non-Spin (NSPIN) product rule was seeded with duration_minutes = 60. Non-Spin
--    must be sustainable for 4 hours (240 min; ECRS is 1 h). The engine's energy hold reads this
--    duration: an award keeps committed_kw x duration / eta_d above the reserve floor.

SET search_path TO og;

CREATE TABLE IF NOT EXISTS og.as_deployment (
    deployment_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    obligation_id  uuid REFERENCES og.obligation(obligation_id),
    start_at       timestamptz NOT NULL,
    end_at         timestamptz NOT NULL,
    source         text NOT NULL CHECK (source IN ('OPERATOR', 'MARKET_SIM', 'SCENARIO')),
    requested_by   text,
    reason         text,
    cancelled_at   timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT as_deployment_window CHECK (end_at > start_at)
);

CREATE INDEX IF NOT EXISTS ix_as_deployment_active ON og.as_deployment (end_at) WHERE cancelled_at IS NULL;

UPDATE og.product_rule
SET duration_minutes = 240
WHERE contract_id = '00000000-0000-7000-8000-000000000d03'
  AND product_code = 'NSPIN'
  AND duration_minutes = 60;
