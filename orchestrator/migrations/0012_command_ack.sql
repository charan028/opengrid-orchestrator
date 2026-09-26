-- 0012: hub command acknowledgements (A3: "every command signed by the guardian, verified by the hub,
-- acknowledged"). Hubs publish <root>/ack/<hub_id> (interfaces/mqtt/ack.schema.json); og-engine's
-- fleet twin buffers them and writes them in batch on its flush cadence. Additive only.

CREATE TABLE IF NOT EXISTS og.command_ack (
    batch_id       uuid        NOT NULL,
    hub_id         text        NOT NULL,
    accepted       boolean     NOT NULL,
    applied_p_kw   double precision,
    reject_reason  text CHECK (reject_reason IN ('BAD_SIGNATURE', 'STALE_EPOCH', 'STALE_SEQ', 'EXPIRED')),
    ts             timestamptz NOT NULL,
    received_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, hub_id)
);

CREATE INDEX IF NOT EXISTS ix_command_ack_ts ON og.command_ack (ts);
