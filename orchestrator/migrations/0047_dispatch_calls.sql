-- 0047: dispatch calls from every origin through one core path (D-33, 2026-09-26). Additive only.
--
-- 1. og.as_deployment gains the utility customer API, the grid link and ERCOT (the ercot_mms deployment
--    poller, origin ERCOT_POLL) as sources, a signed requested kW
--    (sign convention +charge/-discharge; a call is discharge-only, so it is always < 0; NULL = the
--    obligation's full committed kW, the operator path's default) and the call ledger id it came from.
-- 2. og.dispatch_call: the call ledger. Every call attempt from any origin (OPERATOR, UTILITY, GRID_LINK,
--    MARKET_SIM, SCENARIO) is recorded here, ACCEPTED (with its deployment) or REFUSED (with the reason
--    code), so a utility can read back the status and history of its own calls, refusals included.
--    Idempotency: one row per (principal, idempotency_key). Never deleted (audit trail).

SET search_path TO og;

ALTER TABLE og.as_deployment DROP CONSTRAINT IF EXISTS as_deployment_source_check;
ALTER TABLE og.as_deployment ADD CONSTRAINT as_deployment_source_check
    CHECK (source IN ('OPERATOR', 'MARKET_SIM', 'SCENARIO', 'UTILITY', 'GRID_LINK', 'ERCOT'));

ALTER TABLE og.as_deployment ADD COLUMN IF NOT EXISTS requested_kw numeric(12,3);
ALTER TABLE og.as_deployment DROP CONSTRAINT IF EXISTS as_deployment_discharge_only;
ALTER TABLE og.as_deployment ADD CONSTRAINT as_deployment_discharge_only
    CHECK (requested_kw IS NULL OR requested_kw < 0);
ALTER TABLE og.as_deployment ADD COLUMN IF NOT EXISTS call_id uuid;

CREATE TABLE IF NOT EXISTS og.dispatch_call (
    call_id           uuid PRIMARY KEY,
    origin            text NOT NULL
        CHECK (origin IN ('OPERATOR', 'UTILITY', 'GRID_LINK', 'MARKET_SIM', 'SCENARIO', 'ERCOT_POLL')),
    principal         text NOT NULL,
    idempotency_key   text,
    request_hash      text NOT NULL,
    outcome           text NOT NULL CHECK (outcome IN ('ACCEPTED', 'REFUSED')),
    reason_code       text,
    detail            text,
    obligation_id     uuid REFERENCES og.obligation(obligation_id),
    utility_id        text,
    kind              text CHECK (kind IS NULL OR kind IN ('AS', 'UTILITY_CALL')),
    requested_kw      numeric(12,3),
    committed_kw      numeric(12,3),
    start_at          timestamptz NOT NULL,
    end_at            timestamptz NOT NULL,
    duration_minutes  integer NOT NULL,
    reason            text NOT NULL,
    deployment_id     uuid REFERENCES og.as_deployment(deployment_id),
    trace_id          uuid,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dispatch_call_accepted_has_deployment
        CHECK ((outcome = 'ACCEPTED') = (deployment_id IS NOT NULL)),
    CONSTRAINT dispatch_call_refused_has_reason CHECK (outcome = 'ACCEPTED' OR reason_code IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_dispatch_call_idempotency
    ON og.dispatch_call (principal, idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_dispatch_call_principal_time ON og.dispatch_call (principal, created_at);
CREATE INDEX IF NOT EXISTS ix_dispatch_call_utility_time ON og.dispatch_call (utility_id, created_at);
CREATE INDEX IF NOT EXISTS ix_as_deployment_obligation ON og.as_deployment (obligation_id)
    WHERE cancelled_at IS NULL;
