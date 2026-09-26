-- 0019: per-scope dispatch posture from guardian veto statistics (ES06-S04, K7: TIMEOUT != VETO != STOP).
-- Additive only. og-guardian (opengrid.guardian.escalation) is the sole writer; og-engine reads it and
-- honours CONSERVATIVE with reduced or zero new dispatch in that scope. A safe stop is only ever REQUESTED
-- of a person (alert + operator-action proposal), never engaged from here.

CREATE TABLE IF NOT EXISTS og.scope_posture (
    scope_kind       text NOT NULL CHECK (scope_kind IN ('BANK', 'ZONE')),
    scope_ref        text NOT NULL,
    posture          text NOT NULL CHECK (posture IN ('NORMAL', 'CONSERVATIVE')),
    veto_ratio       double precision NOT NULL,
    consecutive      integer NOT NULL,
    stop_requested   boolean NOT NULL DEFAULT false,
    since            timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_kind, scope_ref)
);

CREATE INDEX IF NOT EXISTS ix_scope_posture_conservative ON og.scope_posture (posture) WHERE posture = 'CONSERVATIVE';
