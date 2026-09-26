-- 0030: selector plan analytics (OPTIMIZER; number assigned by the lead 2026-09-26). Additive only.
--
-- og.plan_energy_value  09 D7 stored-energy (water) value per bank over a plan's horizon: the real-time
--                       dispatcher discharges free headroom only when the live zone price covers
--                       `discharge_threshold_usd_per_mwh` at the current interval (replaces the fixed
--                       $30/MWh, finding G9). One row per (plan, bank); arrays indexed by interval
--                       (interval i starts at horizon_start + i * interval_minutes). NULL element = no
--                       value for that interval (dual unavailable). Kept 7 days (pruned by the selector).
-- og.plan_value         ES05-S07 / KPI-22: the LP plan's and the rule baseline's (F2 shadow run) expected
--                       net value on the SAME gate inputs, their difference (value added by the LP), and
--                       the plan-level forgone upside of the commitment lock.
-- og.plan_shadow_obligation  Per obligation-interval: kW the LP and the rule each gave it, and the best
--                       competing candidate value for a committed obligation's locked capacity. Read by
--                       settle (`fetch_rule_baseline_delivered_kwh`, `fetch_best_competing_value_per_kwh`)
--                       via og.commitment.plan_id.

CREATE TABLE IF NOT EXISTS og.plan_energy_value (
    plan_id                          uuid NOT NULL REFERENCES og.plan(plan_id),
    bank_id                          text NOT NULL,
    horizon_start                    timestamptz NOT NULL,
    horizon_end                      timestamptz NOT NULL,
    interval_minutes                 integer NOT NULL CHECK (interval_minutes > 0),
    water_value_usd_per_mwh          double precision[] NOT NULL,
    discharge_threshold_usd_per_mwh  double precision[] NOT NULL,
    planned_floor_kwh                double precision[] NOT NULL,
    hold_floor_kwh                   double precision[],
    created_at                       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plan_id, bank_id)
);
-- hold_floor_kwh: 09 S1.8 e^hold, the HARD SoC floor (reserve + held awards' full-deployment energy).
-- Added here idempotently too, for a database that applied an earlier draft of this file.
ALTER TABLE og.plan_energy_value ADD COLUMN IF NOT EXISTS hold_floor_kwh double precision[];
-- Owner decision D-28: the measured solar share of charging the plan used per interval, and its source
-- (TELEMETRY, ERCOT_SOLAR or ASSUMPTION).
ALTER TABLE og.plan_energy_value ADD COLUMN IF NOT EXISTS solar_share double precision[];
ALTER TABLE og.plan_energy_value ADD COLUMN IF NOT EXISTS solar_share_source text[];
CREATE INDEX IF NOT EXISTS ix_plan_energy_value_bank_time
    ON og.plan_energy_value (bank_id, horizon_start DESC, created_at DESC);
-- Retention: the selector prunes rows older than 7 days after each gate (selector.db.prune_plan_energy_value).
CREATE INDEX IF NOT EXISTS ix_plan_energy_value_created ON og.plan_energy_value (created_at);

CREATE TABLE IF NOT EXISTS og.plan_value (
    plan_id              uuid PRIMARY KEY REFERENCES og.plan(plan_id),
    lp_net_value         numeric(18,4) NOT NULL,
    rule_net_value       numeric(18,4) NOT NULL,
    value_added          numeric(18,4) NOT NULL,
    forgone_upside       numeric(18,4) NOT NULL DEFAULT 0,
    stage_r_objective    numeric(18,4),
    breakdown            jsonb NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS og.plan_shadow_obligation (
    plan_id                        uuid NOT NULL REFERENCES og.plan(plan_id),
    obligation_id                  uuid NOT NULL,
    interval_start                 timestamptz NOT NULL,
    interval_end                   timestamptz NOT NULL,
    lp_kw                          numeric(12,3) NOT NULL,
    rule_kw                        numeric(12,3) NOT NULL,
    best_competing_value_per_kwh   numeric(18,6),
    PRIMARY KEY (plan_id, obligation_id, interval_start)
);
CREATE INDEX IF NOT EXISTS ix_plan_shadow_obligation_obligation
    ON og.plan_shadow_obligation (obligation_id, interval_start);
