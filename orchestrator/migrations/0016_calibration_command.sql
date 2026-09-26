-- 0016: guardian-issued calibration commands (07-delivery/06 S6.7, G-25; interfaces/crypto.md S2.5).
-- Additive only. og-guardian is the sole writer of the claim/sign columns; the ack handler
-- (opengrid.assets.calibration_ack) is the sole writer of ack_consumed_at.
--
-- One row per og.calibration_attempt the guardian has decided:
--   * the INSERT is the atomic claim of the attempt (primary key): two evaluators can never both sign it;
--   * (hub_id, epoch, seq) is the durable, strictly increasing per-hub command sequence (replacing the
--     old in-memory counter with a wall-clock epoch); UNIQUE forbids reuse;
--   * status SIGNED rows feed G-25's per-hub rate limit and the fleet-wide budget/concurrency caps;
--   * ack_consumed_at makes each hub ack count exactly once, and binds it to the hub and (epoch, seq).

CREATE TABLE IF NOT EXISTS og.calibration_command (
    calibration_id   uuid PRIMARY KEY REFERENCES og.calibration_attempt(calibration_id),
    hub_id           text NOT NULL REFERENCES og.hub(hub_id),
    status           text NOT NULL CHECK (status IN ('RESERVED', 'SIGNED', 'REFUSED')),
    epoch            bigint,
    seq              bigint,
    reason           text,
    signed_at        timestamptz,
    ack_consumed_at  timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT calibration_command_seq_iff_not_refused CHECK ((status = 'REFUSED') = (seq IS NULL)),
    CONSTRAINT calibration_command_hub_seq_unique UNIQUE (hub_id, epoch, seq)
);

CREATE INDEX IF NOT EXISTS ix_calibration_command_signed ON og.calibration_command (status, signed_at);
