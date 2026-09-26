-- 0035: og-safestop's durable publish outbox (K8). Every accepted stop ENGAGE (signed by the stop-only key)
-- and every relayed guardian-signed RELEASE is written here in the same order it was accepted, and
-- (re)published -- QoS 1, retained -- until the broker acknowledges it, including right after a broker
-- reconnect. Before this, a stop engaged while the broker was down longer than the publish wait failed its
-- publish and nothing ever re-published it.
--
-- Idempotent by (stop_id, action): the same event is queued once; hubs (the sim's StopRegistry) ignore a
-- re-published duplicate. Additive and idempotent (IF NOT EXISTS throughout). Written and read only by
-- opengrid.safestop.pg_backend.

CREATE TABLE IF NOT EXISTS og.stop_outbox (
    seq           bigserial PRIMARY KEY,               -- publish order = acceptance order
    stop_id       uuid        NOT NULL,
    action        text        NOT NULL CHECK (action IN ('ENGAGE', 'RELEASE')),
    topic_suffix  text        NOT NULL,                -- <scope>/<id>/<stop_id> under <root>/stop/
    payload       jsonb       NOT NULL,                -- the signed StopEvent, exactly as published
    created_at    timestamptz NOT NULL DEFAULT now(),
    published_at  timestamptz,                         -- set once the broker acknowledged (PUBACK)
    attempts      integer     NOT NULL DEFAULT 0,
    last_error    text,
    CONSTRAINT stop_outbox_once UNIQUE (stop_id, action)
);

CREATE INDEX IF NOT EXISTS ix_stop_outbox_pending ON og.stop_outbox (seq) WHERE published_at IS NULL;
