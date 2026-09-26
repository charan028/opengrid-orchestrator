-- 0036: og.hub asset dates and device identity for the hub detail drawer (UI-FLEET): when the home's battery
-- was installed and last serviced, and what the device reports about itself. Additive and idempotent.
--
--   installed_at      date         -- install date of the home's battery unit(s); NULL = unknown
--   last_serviced_at  timestamptz  -- last completed service: a successful calibration, a closed
--                                     maintenance work order, or an inverter replacement/recommissioning
--
-- Backfill (sim fleet, deterministic, only where still NULL so a re-run never overwrites real data):
--   installed_at: spread over the 24 months (730 days) before 2026-09-01 by the hub id's number
--                 (hub-00042 -> 42 * 37 mod 730 days back; ids without digits hash instead). A fixed
--                 anchor date, not now(), so every database gets the same dates.
--   last_serviced_at: the latest completed service event already on record, else NULL.
--
-- Kept current by triggers on the three service tables rather than in one writer: calibration outcomes
-- are written by opengrid.assets and the guardian path, work orders by opengrid.assets and operators, so
-- the database is the one place every completion passes through. The update only ever moves the date
-- forward (GREATEST), so an out-of-order write cannot move it back.

ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS installed_at date;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS last_serviced_at timestamptz;

-- Device identity, reported by the battery itself on connect (interfaces/mqtt/device_info.schema.json,
-- OWNER DECISION 2026-09-26), written by opengrid.fleet.device_info.upsert_device_info. All NULL until the
-- device first reports; device_info_at is when it last did.
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS serial_number text;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS manufacturer text;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS model text;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS firmware_version text;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS hardware_revision text;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS commissioned_at date;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS inverter_model text;
ALTER TABLE og.hub ADD COLUMN IF NOT EXISTS device_info_at timestamptz;
COMMENT ON COLUMN og.hub.installed_at IS 'Install date of the home battery unit(s); NULL = unknown.';
COMMENT ON COLUMN og.hub.last_serviced_at IS
    'Last completed service (successful calibration, closed work order, inverter replaced/recommissioned).';

UPDATE og.hub
SET installed_at = DATE '2026-09-01' - (
    (COALESCE(NULLIF(regexp_replace(hub_id, '\D', '', 'g'), '')::bigint, abs(hashtext(hub_id))::bigint) * 37)
    % 730
)::int
WHERE installed_at IS NULL;

UPDATE og.hub h
SET last_serviced_at = s.serviced_at
FROM (
    SELECT hub_id, max(serviced_at) AS serviced_at
    FROM (
        SELECT hub_id, verified_at AS serviced_at FROM og.calibration_attempt
        WHERE outcome IN ('IMPROVED', 'CORRECTED', 'NO_CHANGE') AND verified_at IS NOT NULL
        UNION ALL
        SELECT hub_id, closed_at FROM og.maintenance_work_order
        WHERE status = 'CLOSED' AND closed_at IS NOT NULL
        UNION ALL
        SELECT hub_id, occurred_at FROM og.asset_event
        WHERE event_type IN ('INVERTER_REPLACED', 'RECOMMISSIONED')
    ) events
    GROUP BY hub_id
) s
WHERE h.hub_id = s.hub_id AND h.last_serviced_at IS NULL;

CREATE OR REPLACE FUNCTION og.hub_mark_serviced(p_hub_id text, p_at timestamptz) RETURNS void
LANGUAGE sql AS $$
    UPDATE og.hub SET last_serviced_at = GREATEST(COALESCE(last_serviced_at, p_at), p_at)
    WHERE hub_id = p_hub_id;
$$;

CREATE OR REPLACE FUNCTION og.hub_serviced_by_calibration() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.outcome IN ('IMPROVED', 'CORRECTED', 'NO_CHANGE') AND NEW.verified_at IS NOT NULL THEN
        PERFORM og.hub_mark_serviced(NEW.hub_id, NEW.verified_at);
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION og.hub_serviced_by_work_order() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.status = 'CLOSED' AND NEW.closed_at IS NOT NULL THEN
        PERFORM og.hub_mark_serviced(NEW.hub_id, NEW.closed_at);
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION og.hub_serviced_by_asset_event() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.event_type IN ('INVERTER_REPLACED', 'RECOMMISSIONED') THEN
        PERFORM og.hub_mark_serviced(NEW.hub_id, NEW.occurred_at);
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_hub_serviced_calibration ON og.calibration_attempt;
CREATE TRIGGER trg_hub_serviced_calibration
    AFTER INSERT OR UPDATE OF outcome, verified_at ON og.calibration_attempt
    FOR EACH ROW EXECUTE FUNCTION og.hub_serviced_by_calibration();

DROP TRIGGER IF EXISTS trg_hub_serviced_work_order ON og.maintenance_work_order;
CREATE TRIGGER trg_hub_serviced_work_order
    AFTER INSERT OR UPDATE OF status, closed_at ON og.maintenance_work_order
    FOR EACH ROW EXECUTE FUNCTION og.hub_serviced_by_work_order();

DROP TRIGGER IF EXISTS trg_hub_serviced_asset_event ON og.asset_event;
CREATE TRIGGER trg_hub_serviced_asset_event
    AFTER INSERT ON og.asset_event
    FOR EACH ROW EXECUTE FUNCTION og.hub_serviced_by_asset_event();
