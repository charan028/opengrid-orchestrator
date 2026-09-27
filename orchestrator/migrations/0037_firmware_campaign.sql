-- 0037: firmware update campaigns (R3.1, owner decision 2026-09-26: operators dispatch firmware updates from
-- the console, safely). Additive and idempotent (IF NOT EXISTS throughout).
--
--   og.firmware_catalogue   allowed images: version x hardware revision, sha256, release note. Merged with the
--                           [firmware.catalogue] config entries (opengrid.firmware.catalogue). No arbitrary
--                           version strings: a campaign can only target a catalogued (version, revision).
--   og.firmware_campaign    one rollout: target version, resolved hub ids, waves, caps, halt threshold,
--                           maintenance window, two-person approval. DRAFT -> PROPOSED -> APPROVED -> RUNNING
--                           -> PAUSED | HALTED | COMPLETED | ABORTED.
--   og.firmware_job         one row per (campaign, hub): PENDING/SENT/UPDATING/SUCCEEDED/FAILED/ROLLED_BACK/
--                           SKIPPED, attempts and the retry schedule (exponential backoff).
--   og.firmware_job_event   every job and campaign transition, timestamped (hub, from -> to, reason, attempt).
--                           The operator feed GET /og/api/firmware/campaigns/{id}/events reads it.
--   og.firmware_command     the engine -> guardian hand-off and the guardian's signed-command ledger (K3/K6):
--                           the engine REQUESTs one command per attempt; the guardian REFUSEs or SIGNs it with
--                           the hub's next (epoch, seq); the hub's status (firmware_status.schema.json) is
--                           recorded on the row by opengrid.firmware.ingest.
--
-- og.hub.firmware_version / hardware_revision are owned by 0036's follow-up (device-info ingest); they are
-- re-declared here with IF NOT EXISTS only so this migration applies on its own. A no-op when present.

ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS firmware_version text;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS hardware_revision text;

CREATE TABLE IF NOT EXISTS og.firmware_catalogue (
    version            text        NOT NULL CHECK (version ~ '^[0-9]+(\.[0-9]+){1,3}(-[0-9A-Za-z.]+)?$'),
    hardware_revision  text        NOT NULL,
    sha256             text        NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    release_note       text        NOT NULL,
    released_at        date,
    withdrawn_at       timestamptz,                    -- a withdrawn image can no longer be targeted
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (version, hardware_revision)
);

CREATE TABLE IF NOT EXISTS og.firmware_campaign (
    campaign_id            uuid        PRIMARY KEY,
    name                   text        NOT NULL,
    target_version         text        NOT NULL,
    state                  text        NOT NULL DEFAULT 'DRAFT' CHECK (state IN
                               ('DRAFT', 'PROPOSED', 'APPROVED', 'RUNNING', 'PAUSED', 'HALTED', 'COMPLETED',
                                'ABORTED')),
    selection              jsonb       NOT NULL,       -- the operator's filter or explicit ids, as submitted
    hub_ids                text[]      NOT NULL,       -- resolved once, at creation
    waves                  integer[]   NOT NULL,       -- hub count per wave (wave 0 = canary)
    bank_max_concurrent_pct double precision NOT NULL DEFAULT 10 CHECK (bank_max_concurrent_pct > 0),
    feeder_max_concurrent_pct double precision NOT NULL DEFAULT 10 CHECK (feeder_max_concurrent_pct > 0),
    max_failures           integer     NOT NULL DEFAULT 2 CHECK (max_failures >= 1),
    max_failure_pct        double precision NOT NULL DEFAULT 5 CHECK (max_failure_pct > 0),
    window_start           timestamptz,
    window_end             timestamptz,
    allow_downgrade        boolean     NOT NULL DEFAULT false,
    override_committed     boolean     NOT NULL DEFAULT false,
    requires_second_operator boolean   NOT NULL DEFAULT false,
    second_operator_reasons text[]     NOT NULL DEFAULT '{}',
    reason                 text        NOT NULL,
    proposed_by            text        NOT NULL,
    confirmed_by           text,
    approved_by            text,
    halt_reason            text,
    created_at             timestamptz NOT NULL DEFAULT now(),
    confirmed_at           timestamptz,
    approved_at            timestamptz,
    started_at             timestamptz,
    finished_at            timestamptz,
    updated_at             timestamptz NOT NULL DEFAULT now(),
    trace_id               uuid,
    CONSTRAINT firmware_campaign_window CHECK (window_end IS NULL OR window_start IS NULL OR window_end > window_start),
    CONSTRAINT firmware_campaign_two_person CHECK (
        approved_by IS NULL OR NOT requires_second_operator OR lower(approved_by) <> lower(proposed_by))
);

CREATE INDEX IF NOT EXISTS ix_firmware_campaign_active ON og.firmware_campaign (state)
    WHERE state IN ('APPROVED', 'RUNNING', 'PAUSED');

CREATE TABLE IF NOT EXISTS og.firmware_job (
    job_id           uuid        PRIMARY KEY,
    campaign_id      uuid        NOT NULL REFERENCES og.firmware_campaign(campaign_id),
    hub_id           text        NOT NULL,
    bank_id          text        NOT NULL,
    feeder_id        text,
    wave             integer     NOT NULL CHECK (wave >= 0),
    action           text        NOT NULL DEFAULT 'UPDATE' CHECK (action IN ('UPDATE', 'ROLLBACK')),
    state            text        NOT NULL DEFAULT 'PENDING' CHECK (state IN
                         ('PENDING', 'SENT', 'UPDATING', 'SUCCEEDED', 'FAILED', 'ROLLED_BACK', 'SKIPPED')),
    from_version     text,
    target_version   text        NOT NULL,
    attempts         integer     NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at  timestamptz,                      -- NULL = eligible now; set by a retry's backoff
    command_id       uuid,                             -- the current attempt's og.firmware_command
    reason           text,
    terminal_failure boolean     NOT NULL DEFAULT false, -- counts toward the campaign halt threshold
    sent_at          timestamptz,
    updating_at      timestamptz,
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT firmware_job_once UNIQUE (campaign_id, hub_id)
);

CREATE INDEX IF NOT EXISTS ix_firmware_job_campaign_state ON og.firmware_job (campaign_id, state);
CREATE INDEX IF NOT EXISTS ix_firmware_job_inflight ON og.firmware_job (hub_id) WHERE state IN ('SENT', 'UPDATING');

CREATE TABLE IF NOT EXISTS og.firmware_job_event (
    event_id     bigserial   PRIMARY KEY,
    campaign_id  uuid        NOT NULL REFERENCES og.firmware_campaign(campaign_id),
    job_id       uuid,                                 -- NULL for a campaign-level event
    hub_id       text,
    event        text        NOT NULL,                 -- SENT, ACKED, UPDATING, SUCCEEDED, FAILED, RETRY_SCHEDULED, ...
    from_state   text,
    to_state     text,
    reason       text,
    attempt      integer,
    detail       jsonb,
    ts           timestamptz NOT NULL DEFAULT now(),
    trace_id     uuid
);

CREATE INDEX IF NOT EXISTS ix_firmware_job_event_campaign ON og.firmware_job_event (campaign_id, event_id);

CREATE TABLE IF NOT EXISTS og.firmware_command (
    command_id       uuid        PRIMARY KEY,
    job_id           uuid        NOT NULL REFERENCES og.firmware_job(job_id),
    campaign_id      uuid        NOT NULL REFERENCES og.firmware_campaign(campaign_id),
    hub_id           text        NOT NULL,
    attempt          integer     NOT NULL CHECK (attempt >= 1),
    action           text        NOT NULL CHECK (action IN ('UPDATE', 'ROLLBACK')),
    target_version   text        NOT NULL,
    from_version     text,
    sha256           text        NOT NULL,
    hardware_revision text       NOT NULL,
    issued_at        timestamptz NOT NULL,
    expires_at       timestamptz NOT NULL,
    status           text        NOT NULL DEFAULT 'REQUESTED' CHECK (status IN
                         ('REQUESTED', 'RESERVED', 'SIGNED', 'REFUSED', 'PUBLISHED')),
    refuse_reason    text,
    epoch            bigint,
    seq              bigint,
    payload          jsonb,                            -- the signed wire command, exactly as published
    requested_at     timestamptz NOT NULL DEFAULT now(),
    signed_at        timestamptz,
    published_at     timestamptz,
    hub_state        text,                             -- last firmware_status.state from the hub
    hub_reason       text,
    hub_version      text,
    hub_ts           timestamptz,
    CONSTRAINT firmware_command_seq_once UNIQUE (hub_id, epoch, seq)
);

CREATE INDEX IF NOT EXISTS ix_firmware_command_requested ON og.firmware_command (requested_at)
    WHERE status = 'REQUESTED';
CREATE INDEX IF NOT EXISTS ix_firmware_command_hub ON og.firmware_command (hub_id, epoch, seq);

INSERT INTO og.retention_policy (event_class, retention_days, prune_after_checkpoint) VALUES
    ('FIRMWARE_CAMPAIGN', 1825, true),
    ('FIRMWARE_UPDATE', 1825, true)
ON CONFLICT (event_class) DO NOTHING;
