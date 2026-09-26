-- dev/seed/rebalance_dual_units.sql
--
-- Backfill for the dual-unit clustering fix, v2 (opengrid.fleet.seed._is_dual_unit /
-- ogsim.fleet.state._dual_unit_mask). Banks are feeder segments and must stay single-zone, so this
-- version does NOT move any hub between banks (bank assignment stays `bank-{i % bank_count:03d}`,
-- unchanged, exactly as live today). It only reclassifies which hubs are dual-unit: the corrected
-- rule offsets the dual-unit selection *per bank* (`k = hub_index // bank_count`, dual-unit iff
-- `floor((k+1)*dual_unit_share) > floor(k*dual_unit_share)`) instead of by the hub's fleet-wide
-- index, so every bank ends up with 10 of its 50 hubs dual-unit instead of the old rule clustering
-- all 400 dual-unit homes into 8 of the 40 banks.
--
-- This script only updates each hub's RATED capacity (e_kwh, p_kw, r_kwh) to match its corrected
-- dual/single classification, and clamps any now-inconsistent live SoC in og.hub_state. It never
-- touches og.hub.bank_id, og.hub.zone, or og.bank (those are all correct already and out of scope for
-- this version of the fix).
--
-- DO NOT RUN THIS AUTOMATICALLY. Read "Live risk assessment" below and get sign-off on timing first.
-- This ships AFTER noon -- excluded from the pre-freeze release. The ogsim simulator and this DB
-- backfill MUST switch together: ogsim.fleet.state's `_dual_unit_mask` and this script both encode
-- the identical corrected rule, and telemetry from a hub whose sim-side rating disagrees with its
-- DB-side rating (e_kwh/p_kw) will misreport capacity until both sides run the same rule.
--
-- Parameters below (bank_count, dual_unit_share, and the four rating constants) match the MVP-S demo
-- defaults (opengrid/fleet/seed.py's SimFleetTopologyConfig / integration-sims/config/fleet.yaml). If
-- that config has been overridden, update the constants in the `params` CTE before running -- this
-- script has no Python runtime, so it must be kept numerically in sync with fleet.yaml by hand.
--
-- Idempotent: every hub's dual/single classification is derived purely from its hub_id and the
-- constants below, and each UPDATE only touches a row whose current value disagrees with the
-- recomputed one, so re-running this script after it has already been applied is a no-op.
--
-- ---------------------------------------------------------------------------------------------------
-- Live risk assessment (read before running)
-- ---------------------------------------------------------------------------------------------------
--
-- Checked schema (0001_init.sql onward): og.hub has no FK to anything this script writes, and
-- og.hub_inverter_pq (0010_service_profile.sql) has no per-unit-count column and is keyed by hub_id
-- only (PK), not by rating -- it needs no change. There is no "unit count" column anywhere in the
-- orchestrator schema; ogsim's per-leg inverter modeling (ogsim.fleet.pq) is sim-side only, never
-- persisted to this DB, so there is nothing else keyed on a hub's dual/single status to update here.
--
-- The real risk is SoC becoming physically inconsistent, not referential integrity:
--   1. A hub DEMOTED from dual-unit (78.4 kWh) to single-unit (39.2 kWh) may have a live
--      `og.hub_state.soc_kwh` recorded above its new, smaller capacity -- an impossible state (more
--      energy stored than the battery can hold) that would corrupt every downstream SoC-based
--      calculation (K1 reserve floor, guardian energy projection G-01-ENERGY, allocator headroom).
--      This script clamps `soc_kwh` down to the new `e_kwh` for exactly these hubs.
--   2. A hub PROMOTED from single-unit (39.2 kWh) to dual-unit (78.4 kWh) only ever gains headroom
--      (its old soc_kwh was always <= 39.2 <= the new 78.4 cap), so no clamp is needed on promotion --
--      its SoC-as-a-fraction-of-capacity silently drops, which is honest (the physical battery really
--      is bigger now) rather than a data-integrity problem.
--   3. Reservations/grants are sized in kW against `og.hub`/`og.bank`'s rated capacity at the time
--      they were made. Reclassifying a hub's p_kw mid-obligation changes the fleet's real available
--      capacity out from under an ACTIVE reservation/grant, exactly the same class of risk as v1's
--      bank-reassignment script (reservations are per bank, not per hub, so a bank whose dual/single
--      mix changes mid-commitment is now delivering a different aggregate kW than what was reserved).
--
-- RECOMMENDATION: do NOT apply this before noon (per the lead: this ships after the pre-freeze
-- release regardless). Apply only in a window with zero DELIVERING/COMMITTED obligations, and only
-- once the ogsim simulator is ALSO restarted with the corrected `_dual_unit_mask` -- switching only
-- one side leaves telemetry and DB ratings disagreeing about which hubs are dual-unit. Confirm with
-- the exact check query below immediately before running -- if it returns any rows, DO NOT run this
-- script yet:
--
--     SELECT o.obligation_id, o.state, o.window_start, o.window_end, r.bank_id
--     FROM og.obligation o
--     JOIN og.reservation r ON r.obligation_id = o.obligation_id
--     WHERE o.state IN ('DELIVERING', 'COMMITTED')
--       AND r.released_at IS NULL;
--
-- ---------------------------------------------------------------------------------------------------

BEGIN;

WITH params AS (
    SELECT
        40    AS bank_count,
        0.2   AS dual_unit_share,
        0.20  AS reserve_frac,       -- confirmed reserve fraction, all hubs
        78.4  AS e_kwh_dual,
        20.0  AS p_kw_dual,
        39.2  AS e_kwh_single,
        11.0  AS p_kw_single
),
hub_index AS (
    SELECT
        h.hub_id,
        (substring(h.hub_id FROM 'hub-(\d+)'))::int AS idx
    FROM og.hub h
),
classified AS (
    SELECT
        hi.hub_id,
        -- k = the hub's rank within its own bank (hub i belongs to bank i % bank_count, and there are
        -- hub_count // bank_count such ranks 0..49 per bank); dual-unit iff floor((k+1)*share) >
        -- floor(k*share) -- identical to opengrid.fleet.seed._is_dual_unit / ogsim.fleet.state.
        -- _dual_unit_mask.
        (
            floor((hi.idx / p.bank_count + 1) * p.dual_unit_share)
            > floor((hi.idx / p.bank_count) * p.dual_unit_share)
        ) AS is_dual,
        p.e_kwh_dual, p.p_kw_dual, p.e_kwh_single, p.p_kw_single, p.reserve_frac
    FROM hub_index hi, params p
)
UPDATE og.hub h
SET
    e_kwh = CASE WHEN c.is_dual THEN c.e_kwh_dual ELSE c.e_kwh_single END,
    p_kw  = CASE WHEN c.is_dual THEN c.p_kw_dual  ELSE c.p_kw_single  END,
    r_kwh = CASE WHEN c.is_dual THEN c.e_kwh_dual ELSE c.e_kwh_single END * c.reserve_frac
FROM classified c
WHERE h.hub_id = c.hub_id
  AND (
        h.e_kwh IS DISTINCT FROM (CASE WHEN c.is_dual THEN c.e_kwh_dual ELSE c.e_kwh_single END)
     OR h.p_kw  IS DISTINCT FROM (CASE WHEN c.is_dual THEN c.p_kw_dual  ELSE c.p_kw_single  END)
  );

-- Clamp any hub_state.soc_kwh that now exceeds its (possibly newly demoted) hub's capacity -- an
-- impossible physical state otherwise (see risk item 1 above). The WHERE guard means this only ever
-- pulls a too-high value down to the new ceiling; it never raises a SoC that is already <= capacity.
UPDATE og.hub_state hs
SET soc_kwh = h.e_kwh
FROM og.hub h
WHERE hs.hub_id = h.hub_id
  AND hs.soc_kwh > h.e_kwh;

COMMIT;

-- ---------------------------------------------------------------------------------------------------
-- Post-run verification (read-only; run these after COMMIT to confirm the fix landed)
-- ---------------------------------------------------------------------------------------------------
--
-- 1. Every bank now has exactly 10 dual-unit homes (p_kw = the dual-unit rating, 20.0 kW), and every
--    bank is untouched in its hub membership (still 50 hubs, same bank_id as before this script ran):
--
--     SELECT bank_id, count(*) FILTER (WHERE p_kw = 20.0) AS dual_unit_homes, count(*) AS total_homes
--     FROM og.hub
--     GROUP BY bank_id
--     ORDER BY bank_id;   -- expect dual_unit_homes = 10 and total_homes = 50 on every row
--
-- 2. No hub_state row exceeds its hub's rated capacity:
--
--     SELECT hs.hub_id, hs.soc_kwh, h.e_kwh
--     FROM og.hub_state hs
--     JOIN og.hub h ON h.hub_id = hs.hub_id
--     WHERE hs.soc_kwh > h.e_kwh;   -- expect zero rows
--
-- 3. No bank still concentrates all its dual-unit capacity (the original symptom: 1,000 kW on a
--    600 kVA bank):
--
--     SELECT bank_id, sum(p_kw) AS dual_unit_kw
--     FROM og.hub
--     WHERE p_kw = 20.0
--     GROUP BY bank_id
--     HAVING sum(p_kw) > 600.0;   -- expect zero rows
