-- OpenGrid Orchestrator MVP-S -- initial schema.
-- Source: 02a-mvp-s-spec-engine.md S1 (engine tables) + 02b-mvp-s-spec-platform.md S4.2, S6.4 (platform
-- tables: hub, bank, hub_state, telemetry, heartbeat, alert, feed_obs, feed_status).
-- Forward-only within MVP-S (BUILD.md S5a, 02b S9.6). Applied by opengrid.platform.db (`make migrate`).

CREATE SCHEMA IF NOT EXISTS og;
SET search_path TO og;

-- =====================================================================================================
-- 1. contract, product_rule (02a S1.2-1.3)
-- =====================================================================================================

CREATE TABLE og.contract (
    contract_id      uuid PRIMARY KEY,
    customer_id      uuid NOT NULL,
    service_type     text NOT NULL CHECK (service_type IN
                       ('HOME','ERCOT_ENERGY','ERCOT_AS','DIST_DEFERRAL','PARTNER_CAPACITY')),
    variant          text,
    tier             text NOT NULL CHECK (tier IN ('L0','L1','L2','T1','T2','T3','T4')),
    profile_ref      text NOT NULL,
    territory_id     uuid,
    start_at         timestamptz NOT NULL,
    end_at           timestamptz,
    renomination_allowed boolean NOT NULL DEFAULT false,
    penalty_alpha    numeric(18,6),
    penalty_beta     numeric(18,6),
    penalty_theta    numeric(6,4),
    degradation_cost numeric(18,6) NOT NULL DEFAULT 0.03,
    fallback_allowed boolean NOT NULL DEFAULT false,
    status           text NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE','SUSPENDED','ENDED')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_contract_service ON og.contract(service_type, status);
CREATE INDEX ix_contract_customer ON og.contract(customer_id);

CREATE TABLE og.product_rule (
    product_rule_id  uuid PRIMARY KEY,
    contract_id      uuid NOT NULL REFERENCES og.contract(contract_id),
    product_code     text NOT NULL,
    min_qty_kw       numeric(10,3) NOT NULL DEFAULT 0,
    increment_kw     numeric(10,3) NOT NULL DEFAULT 0.1,
    block            boolean NOT NULL DEFAULT false,
    duration_minutes integer NOT NULL,
    variable_kind    text NOT NULL CHECK (variable_kind IN ('CONTINUOUS','SEMI_CONTINUOUS','BINARY')),
    UNIQUE (contract_id, product_code)
);

-- =====================================================================================================
-- 2. opportunity, plan (plan created before obligation/commitment which reference it)
-- =====================================================================================================

CREATE TABLE og.plan (
    plan_id          uuid PRIMARY KEY,
    plan_mode        text NOT NULL CHECK (plan_mode IN ('L-DA','L-ID','RULE_FALLBACK')),
    gate_kind        text NOT NULL CHECK (gate_kind IN ('SCHEDULED_15MIN','ADMISSION','RENOMINATION')),
    horizon_start    timestamptz NOT NULL,
    horizon_end      timestamptz NOT NULL,
    scenario_set     jsonb NOT NULL,
    solver_status    text NOT NULL,
    solver_gap       numeric(8,5),
    solver_time_ms   integer,
    objective_value  numeric(18,4),
    superseded_by    uuid REFERENCES og.plan(plan_id),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_plan_horizon ON og.plan(horizon_start, horizon_end);

CREATE TABLE og.opportunity (
    opportunity_id   uuid PRIMARY KEY,
    contract_id      uuid NOT NULL REFERENCES og.contract(contract_id),
    product_rule_id  uuid REFERENCES og.product_rule(product_rule_id),
    window_start     timestamptz NOT NULL,
    window_end       timestamptz NOT NULL,
    requested_kw     numeric(10,3) NOT NULL,
    value_per_mwh    numeric(18,6),
    scenario_basis   text NOT NULL DEFAULT 'P50' CHECK (scenario_basis IN ('P10','P50','P90')),
    state            text NOT NULL DEFAULT 'OFFERED' CHECK (state IN
                       ('OFFERED','SELECTED','REJECTED','EXPIRED')),
    reason_code      text,
    admitted_at      timestamptz NOT NULL DEFAULT now(),
    decided_at       timestamptz,
    gate_id          uuid REFERENCES og.plan(plan_id)
);
CREATE INDEX ix_opportunity_state ON og.opportunity(state, window_start);
CREATE INDEX ix_opportunity_contract ON og.opportunity(contract_id);

-- =====================================================================================================
-- 3. obligation, commitment, renomination_point (02a S1.5-1.7) -- the commitment-lock core
-- =====================================================================================================

CREATE TABLE og.obligation (
    obligation_id    uuid PRIMARY KEY,
    opportunity_id   uuid NOT NULL REFERENCES og.opportunity(opportunity_id),
    contract_id      uuid NOT NULL REFERENCES og.contract(contract_id),
    service_type     text NOT NULL,
    tier             text NOT NULL,
    window_start     timestamptz NOT NULL,
    window_end       timestamptz NOT NULL,
    committed_qty_kw numeric(10,3) NOT NULL,
    state            text NOT NULL DEFAULT 'OFFERED' CHECK (state IN
                       ('OFFERED','SELECTED','COMMITTED','DELIVERING','FULFILLED',
                        'SHORTFALL','SETTLED','REJECTED','EXPIRED')),
    at_risk          boolean NOT NULL DEFAULT false,
    last_reason_code text,
    version          integer NOT NULL DEFAULT 1,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_obligation_state ON og.obligation(state);
CREATE INDEX ix_obligation_window ON og.obligation(window_start, window_end);

CREATE TABLE og.commitment (
    commitment_id    uuid PRIMARY KEY,
    obligation_id    uuid NOT NULL REFERENCES og.obligation(obligation_id),
    plan_id          uuid NOT NULL REFERENCES og.plan(plan_id),
    interval_start   timestamptz NOT NULL,
    interval_end     timestamptz NOT NULL,
    committed_kw     numeric(10,3) NOT NULL,
    variable_kind    text NOT NULL,
    supersedes       uuid REFERENCES og.commitment(commitment_id),
    reason_code      text NOT NULL DEFAULT 'R-GATE-SELECT',
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_commitment_obligation ON og.commitment(obligation_id, interval_start);
CREATE UNIQUE INDEX ux_commitment_active ON og.commitment(obligation_id, interval_start)
    WHERE supersedes IS NULL;

CREATE TABLE og.renomination_point (
    renomination_point_id uuid PRIMARY KEY,
    contract_id      uuid NOT NULL REFERENCES og.contract(contract_id),
    obligation_id    uuid REFERENCES og.obligation(obligation_id),
    scheduled_at     timestamptz NOT NULL,
    exercised_at     timestamptz,
    outcome          text CHECK (outcome IN ('RESELECTED','CONFIRMED','SKIPPED')),
    plan_id          uuid REFERENCES og.plan(plan_id)
);
CREATE INDEX ix_renom_contract ON og.renomination_point(contract_id, scheduled_at);

-- =====================================================================================================
-- 4. reservation, grant, command_batch, verdict, stop_event (02a S1.9-1.12)
-- =====================================================================================================

CREATE TABLE og.reservation (
    reservation_id   uuid PRIMARY KEY,
    obligation_id    uuid NOT NULL REFERENCES og.obligation(obligation_id),
    bank_id          uuid NOT NULL,
    kind             text NOT NULL CHECK (kind IN ('POWER_KW','ENERGY_KWH')),
    amount           numeric(12,3) NOT NULL,
    interval_start   timestamptz NOT NULL,
    interval_end     timestamptz NOT NULL,
    ledger_version   bigint NOT NULL,
    released_at      timestamptz,
    release_reason   text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_reservation_bank_interval ON og.reservation(bank_id, interval_start, interval_end)
    WHERE released_at IS NULL;
CREATE INDEX ix_reservation_obligation ON og.reservation(obligation_id);

CREATE TABLE og.grant (
    grant_id         uuid PRIMARY KEY,
    cycle_id         text NOT NULL,
    obligation_id    uuid REFERENCES og.obligation(obligation_id),
    bank_id          uuid NOT NULL,
    granted_kw       numeric(10,3) NOT NULL,
    is_headroom      boolean NOT NULL DEFAULT false,
    ledger_version   bigint NOT NULL,
    command_batch_id uuid,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_grant_cycle ON og.grant(cycle_id);
CREATE INDEX ix_grant_obligation ON og.grant(obligation_id);

CREATE TABLE og.command_batch (
    command_batch_id uuid PRIMARY KEY,
    cycle_id         text NOT NULL,
    ledger_version   bigint NOT NULL,
    submission_id    text NOT NULL UNIQUE,
    command_count    integer NOT NULL,
    merkle_root      text NOT NULL,
    trace_pre_image_id uuid,
    created_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE og.verdict (
    verdict_id       uuid PRIMARY KEY,
    command_batch_id uuid NOT NULL REFERENCES og.command_batch(command_batch_id),
    outcome          text NOT NULL CHECK (outcome IN ('PASS','PARTLY_VETOED','VETOED','TIMEOUT')),
    vetoed_rule_ids  text[],
    latency_ms       integer NOT NULL,
    inputs_hash      text NOT NULL,
    signature        text,
    signed_at        timestamptz
);
CREATE INDEX ix_verdict_batch ON og.verdict(command_batch_id);

CREATE TABLE og.stop_event (
    stop_event_id    uuid PRIMARY KEY,
    scope_kind       text NOT NULL CHECK (scope_kind IN ('BANK','ZONE','FLEET')),
    scope_ref        text NOT NULL,
    action           text NOT NULL CHECK (action IN ('ENGAGE','RELEASE')),
    initiator_kind   text NOT NULL CHECK (initiator_kind IN ('OPERATOR','GUARDIAN','SAFESTOP_AUTHORITY','UTILITY')),
    initiator_ref    text NOT NULL,
    reason           text NOT NULL,
    approver_ref     text,
    signature        text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_stop_scope ON og.stop_event(scope_kind, scope_ref, created_at DESC);

-- =====================================================================================================
-- 5. meter_interval, performance, invoice_line, pnl, operator_action (02a S1.13, S1.15)
-- =====================================================================================================

CREATE TABLE og.meter_interval (
    meter_interval_id uuid PRIMARY KEY,
    obligation_id    uuid NOT NULL REFERENCES og.obligation(obligation_id),
    interval_start   timestamptz NOT NULL,
    interval_end     timestamptz NOT NULL,
    delivered_kwh    numeric(14,6) NOT NULL,
    baseline_kwh     numeric(14,6),
    source           text NOT NULL CHECK (source IN ('DIRECT_HUB_METER','AMI_INTERVAL','SCADA_OUTCOME','ESTIMATED')),
    quality_flag     text NOT NULL DEFAULT 'GOOD' CHECK (quality_flag IN ('GOOD','ESTIMATED','DISPUTED')),
    version          integer NOT NULL DEFAULT 1,
    superseded_by    uuid REFERENCES og.meter_interval(meter_interval_id),
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_meter_active ON og.meter_interval(obligation_id, interval_start)
    WHERE superseded_by IS NULL;

CREATE TABLE og.performance (
    performance_id   uuid PRIMARY KEY,
    obligation_id    uuid NOT NULL REFERENCES og.obligation(obligation_id),
    interval_start   timestamptz NOT NULL,
    interval_end     timestamptz NOT NULL,
    compliance_pct   numeric(6,4) NOT NULL,
    season_pct       numeric(6,4),
    availability_pct numeric(6,4),
    response_time_s  integer,
    passed_threshold boolean NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_performance_obligation ON og.performance(obligation_id, interval_start);

CREATE TABLE og.invoice_line (
    invoice_line_id  uuid PRIMARY KEY,
    contract_id      uuid NOT NULL REFERENCES og.contract(contract_id),
    obligation_id    uuid NOT NULL REFERENCES og.obligation(obligation_id),
    period_start     date NOT NULL,
    period_end       date NOT NULL,
    line_type        text NOT NULL CHECK (line_type IN
                       ('CAPACITY_PAYMENT','ENERGY','AVAILABILITY_PAYMENT','LD_PENALTY','DERATE','BUYBACK','FIXED_FEE')),
    quantity         numeric(14,6),
    unit             text,
    rate             numeric(18,6),
    amount           numeric(18,6) NOT NULL,
    status           text NOT NULL DEFAULT 'PROVISIONAL' CHECK (status IN ('PROVISIONAL','FINAL','CORRECTED')),
    supersedes       uuid REFERENCES og.invoice_line(invoice_line_id),
    trace_roll_up    text,
    version          integer NOT NULL DEFAULT 1,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_invoice_contract_period ON og.invoice_line(contract_id, period_start);

CREATE TABLE og.pnl (
    pnl_id           uuid PRIMARY KEY,
    obligation_id    uuid NOT NULL REFERENCES og.obligation(obligation_id),
    interval_start   timestamptz NOT NULL,
    interval_end     timestamptz NOT NULL,
    revenue          numeric(18,6) NOT NULL DEFAULT 0,
    energy_cost      numeric(18,6) NOT NULL DEFAULT 0,
    degradation_cost numeric(18,6) NOT NULL DEFAULT 0,
    penalty          numeric(18,6) NOT NULL DEFAULT 0,
    net_value        numeric(18,6) NOT NULL,
    rule_baseline_value numeric(18,6),
    forgone_upside   numeric(18,6) DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_pnl_obligation ON og.pnl(obligation_id, interval_start);

CREATE TABLE og.operator_action (
    operator_action_id uuid PRIMARY KEY,
    operator_ref     text NOT NULL,
    action_kind      text NOT NULL CHECK (action_kind IN
                       ('MANUAL_COMMAND','SAFE_STOP_ENGAGE','SAFE_STOP_RELEASE','APPROVAL','CONFIG_CHANGE')),
    target_ref       text,
    tier             text CHECK (tier IN ('PRE_AUTHORIZED','ENGAGE','TIER1','TIER2')),
    reason           text,
    confirmed_at     timestamptz,
    approver_ref     text,
    trace_id         uuid,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_operator_action_time ON og.operator_action(created_at);

-- =====================================================================================================
-- 6. trace, trace_checkpoint, retention_policy (02a S1.14, S8) -- FK to trace added after the table
-- =====================================================================================================

CREATE TABLE og.trace (
    trace_id         uuid PRIMARY KEY,
    parent_trace_id  uuid REFERENCES og.trace(trace_id),
    decision_type    text NOT NULL CHECK (decision_type IN
                       ('DA_PLAN','ID_PLAN','ADMISSION','COMMITMENT','RENOMINATION','RT_ALLOCATION',
                        'SUBSTITUTION','GUARDIAN_VERDICT','SAFE_STOP','SHORTFALL','OPERATOR_ACTION',
                        'FEED_CHANGE','ALERT','SETTLEMENT')),
    event_class      text NOT NULL,
    stream_id        text NOT NULL,
    seq              bigint NOT NULL,
    scope            jsonb,
    payload          jsonb NOT NULL,
    reason_codes     text[],
    prev_hash        text,
    hash             text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX ux_trace_stream_seq ON og.trace(stream_id, seq);
CREATE UNIQUE INDEX ux_trace_stream_prev ON og.trace(stream_id, prev_hash);
CREATE INDEX ix_trace_class_time ON og.trace(event_class, created_at);
CREATE INDEX ix_trace_reason ON og.trace USING gin(reason_codes);

ALTER TABLE og.operator_action
    ADD CONSTRAINT fk_operator_action_trace FOREIGN KEY (trace_id) REFERENCES og.trace(trace_id);

CREATE TABLE og.trace_checkpoint (
    checkpoint_id    uuid PRIMARY KEY,
    checkpoint_at    timestamptz NOT NULL,
    stream_heads     jsonb NOT NULL,
    checkpoint_hash  text NOT NULL,
    anchor_ref       text,
    created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_checkpoint_time ON og.trace_checkpoint(checkpoint_at);

CREATE TABLE og.retention_policy (
    event_class      text PRIMARY KEY,
    retention_days   integer NOT NULL,
    prune_after_checkpoint boolean NOT NULL DEFAULT true,
    updated_at       timestamptz NOT NULL DEFAULT now()
);

-- =====================================================================================================
-- 7. Platform tables: fleet twin + health (02b S4.2, S6.4)
-- =====================================================================================================

CREATE TABLE og.hub (
  hub_id      TEXT PRIMARY KEY,
  bank_id     TEXT NOT NULL,
  zone        TEXT NOT NULL,
  e_kwh       DOUBLE PRECISION NOT NULL,
  r_kwh       DOUBLE PRECISION NOT NULL,
  p_kw        DOUBLE PRECISION NOT NULL,
  eta_c       DOUBLE PRECISION NOT NULL DEFAULT 0.9487,
  eta_d       DOUBLE PRECISION NOT NULL DEFAULT 0.9487,
  lat DOUBLE PRECISION, lon DOUBLE PRECISION,
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE og.bank (
  bank_id     TEXT PRIMARY KEY,
  zone        TEXT NOT NULL,
  kva_rating  DOUBLE PRECISION NOT NULL,
  reserve_kva DOUBLE PRECISION NOT NULL DEFAULT 0,
  feeder_id   TEXT
);

CREATE TABLE og.hub_state (
  hub_id      TEXT PRIMARY KEY REFERENCES og.hub(hub_id),
  soc_kwh     DOUBLE PRECISION NOT NULL,
  p_kw        DOUBLE PRECISION NOT NULL,
  health      TEXT NOT NULL DEFAULT 'online',
  lease_epoch BIGINT NOT NULL DEFAULT 0,
  lease_expires_at TIMESTAMPTZ,
  last_command_id UUID,
  last_seen_at TIMESTAMPTZ NOT NULL,
  fault_code  TEXT
);

CREATE TABLE og.telemetry (
  hub_id TEXT NOT NULL, ts TIMESTAMPTZ NOT NULL, soc_kwh DOUBLE PRECISION, p_kw DOUBLE PRECISION,
  seq BIGINT NOT NULL, epoch BIGINT NOT NULL, health TEXT, PRIMARY KEY (hub_id, ts)
) PARTITION BY RANGE (ts);

-- One default partition so inserts work out of the box; settle/deploy add day partitions ahead of time.
CREATE TABLE og.telemetry_default PARTITION OF og.telemetry DEFAULT;

CREATE TABLE og.heartbeat (
  process TEXT NOT NULL, pid INT NOT NULL, ts TIMESTAMPTZ NOT NULL, status TEXT NOT NULL DEFAULT 'ok',
  PRIMARY KEY (process)
);

CREATE TABLE og.alert (
  id BIGSERIAL PRIMARY KEY, rule TEXT NOT NULL, severity TEXT NOT NULL,
  summary TEXT NOT NULL, detail JSONB, opened_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  cleared_at TIMESTAMPTZ, acked_by TEXT
);

CREATE TABLE og.feed_obs (
  source TEXT NOT NULL, product TEXT NOT NULL, series TEXT NOT NULL,
  ts TIMESTAMPTZ NOT NULL, value DOUBLE PRECISION NOT NULL, unit TEXT NOT NULL,
  quality TEXT NOT NULL CHECK (quality IN ('GOOD','ESTIMATED','STALE')),
  recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (source, product, series, ts)
);
CREATE INDEX ix_feed_obs_series_ts ON og.feed_obs(series, ts DESC);

CREATE TABLE og.feed_status (
  source TEXT NOT NULL, product TEXT NOT NULL,
  last_value_at TIMESTAMPTZ, last_success_at TIMESTAMPTZ,
  consecutive_failures INT NOT NULL DEFAULT 0,
  breaker_open BOOLEAN NOT NULL DEFAULT false,
  active_key TEXT,
  PRIMARY KEY (source, product)
);

-- =====================================================================================================
-- 8. Seed: retention_policy defaults (02a S8.1). Operator-configurable afterward with no code change.
-- =====================================================================================================

INSERT INTO og.retention_policy (event_class, retention_days, prune_after_checkpoint) VALUES
    ('SAFE_STOP',        3650, true),
    ('COMMITMENT',       1825, true),
    ('SHORTFALL',        1825, true),
    ('GUARDIAN_VERDICT', 1825, true),
    ('RT_ALLOCATION',      90, true),
    ('SUBSTITUTION',       90, true),
    ('DA_PLAN',           365, true),
    ('ID_PLAN',           365, true),
    ('RENOMINATION',      365, true),
    ('ADMISSION',        1825, true),
    ('OPERATOR_ACTION',  1825, true),
    ('FEED_CHANGE',        90, true),
    ('ALERT',              90, true),
    ('SETTLEMENT',       2555, true)
ON CONFLICT (event_class) DO NOTHING;
