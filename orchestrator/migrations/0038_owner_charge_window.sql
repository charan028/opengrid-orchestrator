-- 0038: owner grid-charging windows, editable from the Fleet page (OWNER DECISION D-30). Additive, idempotent.
--
-- A window list may be set at ANY scope; the most specific one that has a row wins
-- (opengrid.core.charge_windows.resolve, the one resolver the selector and the API share):
--     FLEET (default, scope_ref '*') < PROVIDER (utility_id, or the TDSP of a competitive zone, e.g. ONCOR)
--     < ZONE < SUBSTATION < FEEDER < BANK < HUB
-- `windows` are "HH:MM-HH:MM" in America/Chicago local time; a window may wrap midnight ("22:00-06:00");
-- at most 4 per scope. An EMPTY list is a valid override: no grid charging at that scope.
-- Changed only through the two-step API (every change traced as OPERATOR_ACTION, old -> new).

CREATE OR REPLACE FUNCTION og.charge_windows_valid(p_windows text[]) RETURNS boolean
LANGUAGE sql IMMUTABLE AS $$
    SELECT cardinality(p_windows) <= 4
       AND NOT EXISTS (
           SELECT 1 FROM unnest(p_windows) AS w
           WHERE w !~ '^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$'
              OR split_part(w, '-', 1) = split_part(w, '-', 2)
       );
$$;

CREATE TABLE IF NOT EXISTS og.owner_charge_window (
    scope_kind  text NOT NULL
        CHECK (scope_kind IN ('FLEET', 'PROVIDER', 'ZONE', 'SUBSTATION', 'FEEDER', 'BANK', 'HUB')),
    scope_ref   text NOT NULL CHECK (length(scope_ref) > 0),
    windows     text[] NOT NULL CHECK (og.charge_windows_valid(windows)),
    updated_by  text NOT NULL,
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (scope_kind, scope_ref),
    CONSTRAINT owner_charge_window_fleet_ref CHECK (scope_kind <> 'FLEET' OR scope_ref = '*')
);

COMMENT ON TABLE og.owner_charge_window IS
    'D-30 owner grid-charging windows (America/Chicago "HH:MM-HH:MM"); most specific scope wins.';

INSERT INTO og.owner_charge_window (scope_kind, scope_ref, windows, updated_by)
VALUES ('FLEET', '*', ARRAY['22:00-06:00'], 'migration-0038')
ON CONFLICT (scope_kind, scope_ref) DO NOTHING;
