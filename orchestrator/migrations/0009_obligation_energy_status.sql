-- og.obligation_energy_status: latest per-obligation ENERGY-sufficiency snapshot (merge task,
-- combined-deploy pass). opengrid.allocator.energy_sufficiency.evaluate_with_substitution runs every
-- allocator cycle for every COMMITTED/DELIVERING obligation (engine/gateways.py's
-- EnergySufficiencyGateway), but previously only persisted anything (trace + alert) when the result was
-- AT_RISK -- there was nowhere for the API to read a *current* energy_margin_kwh/time_to_depletion_h for
-- an obligation that is currently fine. This is that missing "latest state" row: one per obligation,
-- overwritten every cycle (not an append-only history -- og.trace already holds the AT_RISK history).

SET search_path TO og;

CREATE TABLE og.obligation_energy_status (
    obligation_id       uuid PRIMARY KEY REFERENCES og.obligation(obligation_id),
    required_kwh        numeric(14,6) NOT NULL,
    available_kwh       numeric(14,6) NOT NULL,
    margin_kwh          numeric(14,6) NOT NULL,
    time_to_depletion_h numeric(10,4),
    at_risk             boolean NOT NULL DEFAULT false,
    used_substitution   boolean NOT NULL DEFAULT false,
    computed_at         timestamptz NOT NULL DEFAULT now()
);
