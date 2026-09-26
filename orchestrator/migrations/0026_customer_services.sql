-- 0026: tailored customer services (06-service-profiles-and-power-quality.md S4.a/S4.b/S5.4).
-- Additive only: four new tables and two retention rows. Number assigned by the lead. og.contract's
-- service_type CHECK (incl. PIPELINE_AC) is owned by 0025_market_model.sql and not touched here.
--
--   * og.customer_site_meter_reading  -- DATA_CENTER site meter at the point of common coupling
--                                        (interfaces/mqtt/customer_site_meter.schema.json). Written by
--                                        opengrid.site_ingest in batches with asynchronous commit.
--   * og.corridor_current_reading     -- PIPELINE_AC corridor current
--                                        (interfaces/mqtt/pipeline_corridor_current.schema.json), same writer.
--   * og.invoice_dispute              -- a customer's dispute of one invoice line (opengrid.customer_api).
--   * og.customer_obligation_request  -- a customer's cancel/renominate request on one obligation that the
--                                        commitment lock (K13) does not let the customer apply directly;
--                                        reviewed by an operator or consumed at a re-nomination point.

SET search_path TO og;

CREATE TABLE IF NOT EXISTS og.customer_site_meter_reading (
    customer_id  text        NOT NULL,
    site_id      text        NOT NULL,
    ts           timestamptz NOT NULL,
    p_kw         double precision NOT NULL,   -- + import from the grid, - export
    q_kvar       double precision NOT NULL,
    v_rms_a_v    double precision NOT NULL,
    v_rms_b_v    double precision NOT NULL,
    v_rms_c_v    double precision NOT NULL,
    i_rms_a_a    double precision NOT NULL,
    i_rms_b_a    double precision NOT NULL,
    i_rms_c_a    double precision NOT NULL,
    freq_hz      double precision NOT NULL,
    pf           double precision NOT NULL,
    thd_v_pct    double precision NOT NULL,
    thd_i_pct    double precision NOT NULL,
    quality      text        NOT NULL CHECK (quality IN ('GOOD', 'SUSPECT', 'BAD')),
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, site_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_site_meter_reading_ts ON og.customer_site_meter_reading (ts);

CREATE TABLE IF NOT EXISTS og.corridor_current_reading (
    customer_id  text        NOT NULL,
    corridor_id  text        NOT NULL,
    line_id      text        NOT NULL,
    ts           timestamptz NOT NULL,
    i_ac_a       double precision NOT NULL,
    limit_a      double precision NOT NULL,
    quality      text        NOT NULL CHECK (quality IN ('GOOD', 'SUSPECT', 'BAD')),
    received_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (customer_id, corridor_id, ts)
);
CREATE INDEX IF NOT EXISTS ix_corridor_current_reading_ts ON og.corridor_current_reading (ts);

CREATE TABLE IF NOT EXISTS og.invoice_dispute (
    dispute_id       uuid PRIMARY KEY,
    invoice_line_id  uuid NOT NULL REFERENCES og.invoice_line(invoice_line_id),
    contract_id      uuid NOT NULL REFERENCES og.contract(contract_id),
    customer_id      uuid NOT NULL,
    reason           text,                   -- free text and/or a machine reason code; at least one
    reason_code      text,
    status           text NOT NULL DEFAULT 'OPEN'
                     CHECK (status IN ('OPEN', 'UNDER_REVIEW', 'RESOLVED', 'REJECTED')),
    raised_by        text NOT NULL,
    reviewed_by      text,
    review_note      text,
    trace_id         uuid NOT NULL,          -- og.trace row of the customer action (no FK: trace pruning)
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_invoice_dispute_reason CHECK (reason IS NOT NULL OR reason_code IS NOT NULL)
);
-- At most one live dispute per invoice line; a closed one may be followed by a new one.
CREATE UNIQUE INDEX IF NOT EXISTS ux_invoice_dispute_open
    ON og.invoice_dispute (invoice_line_id) WHERE status IN ('OPEN', 'UNDER_REVIEW');
CREATE INDEX IF NOT EXISTS ix_invoice_dispute_customer ON og.invoice_dispute (customer_id, created_at DESC);

CREATE TABLE IF NOT EXISTS og.customer_obligation_request (
    request_id             uuid PRIMARY KEY,
    obligation_id          uuid NOT NULL REFERENCES og.obligation(obligation_id),
    contract_id            uuid NOT NULL REFERENCES og.contract(contract_id),
    customer_id            uuid NOT NULL,
    kind                   text NOT NULL CHECK (kind IN ('CANCEL', 'RENOMINATE')),
    obligation_state       text NOT NULL,          -- state when the request was made
    requested_kw           numeric(10,3),          -- RENOMINATE only: a new quantity, and/or
    requested_window_start timestamptz,            --   a new window
    requested_window_end   timestamptz,
    renomination_point_id  uuid REFERENCES og.renomination_point(renomination_point_id),
    penalty_terms          jsonb,                  -- the contract's alpha/beta/theta at request time
    rule_code              text NOT NULL,          -- which customer-request rule applied (customer_api/rules.py)
    status                 text NOT NULL
                           CHECK (status IN ('PENDING_OPERATOR_REVIEW', 'QUEUED_FOR_RENOMINATION',
                                             'ACCEPTED', 'REJECTED', 'APPLIED')),
    raised_by              text NOT NULL,
    reviewed_by            text,
    review_note            text,
    trace_id               uuid NOT NULL,          -- og.trace row of the request (no FK: trace pruning)
    created_at             timestamptz NOT NULL DEFAULT now(),
    updated_at             timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_customer_request_open
    ON og.customer_obligation_request (obligation_id, kind)
    WHERE status IN ('PENDING_OPERATOR_REVIEW', 'QUEUED_FOR_RENOMINATION');
CREATE INDEX IF NOT EXISTS ix_customer_request_customer
    ON og.customer_obligation_request (customer_id, created_at DESC);

-- Trace retention for the customer-action event classes: the commercial dispute window (02a S8.1).
INSERT INTO og.retention_policy (event_class, retention_days, prune_after_checkpoint) VALUES
    ('CUSTOMER_DISPUTE', 1825, true),
    ('CUSTOMER_REQUEST', 1825, true)
ON CONFLICT (event_class) DO NOTHING;
