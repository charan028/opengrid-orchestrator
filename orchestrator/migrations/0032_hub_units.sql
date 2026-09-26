-- 0032: og.hub.units -- battery/inverter units installed in the home (review fix, G-02 per-unit cap).
--
-- The guardian's G-02 (opengrid.core.limits.continuous_power_kw) caps a hub at min(p_kw, unit rating),
-- where the unit rating is the Base battery spec: 11 kW for one unit, 20 kW for a dual-unit home (NOT
-- 2 x 11). Without a unit count it fails closed to one unit (11 kW), so a mis-seeded p_kw can no longer
-- raise the cap on its own.
--
-- Additive and idempotent: the column, its CHECK and the one-time backfill run only when the column does
-- not exist yet, so a re-run never overwrites a unit count corrected by hand. Backfill rule (Base battery
-- specs, 2026-09-25): 39.2 kWh per unit, 78.4 kWh dual-unit homes -- e_kwh >= 70 is a dual-unit home.
--
-- Insert-time default: writers that predate this column (opengrid.fleet.seed's UPSERT, dev/seed SQL,
-- test fixtures) do not set units, so a plain DEFAULT 1 would record every newly seeded dual-unit home as
-- one unit and cap it at 11 kW. og.hub_units_default applies the same backfill rule on INSERT when the
-- writer left units at its default of 1 and the energy says two units. A writer that knows the count
-- should set it explicitly; UPDATEs are never touched.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'og' AND table_name = 'hub' AND column_name = 'units'
    ) THEN
        ALTER TABLE og.hub
            ADD COLUMN units smallint NOT NULL DEFAULT 1
            CONSTRAINT hub_units_check CHECK (units IN (1, 2));
        UPDATE og.hub SET units = 2 WHERE e_kwh >= 70;
    END IF;
END
$$;

COMMENT ON COLUMN og.hub.units IS
    'Battery/inverter units in the home (1 = 11 kW, 2 = 20 kW dual-unit home). G-02 caps |P| at min(p_kw, unit rating).';

CREATE OR REPLACE FUNCTION og.hub_units_default() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.units = 1 AND NEW.e_kwh >= 70 THEN
        NEW.units := 2;
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS trg_hub_units_default ON og.hub;
CREATE TRIGGER trg_hub_units_default
    BEFORE INSERT ON og.hub
    FOR EACH ROW EXECUTE FUNCTION og.hub_units_default();
