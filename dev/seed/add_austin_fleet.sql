-- dev/seed/add_austin_fleet.sql
--
-- Build phase, 2026-09-26 (docs/orchestrator/07-delivery/08-market-model-two-markets.md S3a): adds the
-- Austin Energy (LZ_AEN) and CPS Energy (LZ_CPS) fleet blocks -- 10 banks x 50 homes each, 500 hubs per
-- zone, 1,000 hubs / 20 banks total -- matching `opengrid.fleet.seed.ZoneBlockConfig` /
-- `ogsim.common.config.ZoneBlockConfig` at the settings shipped (disabled) in
-- `integration-sims/config/fleet.yaml`'s `zone_blocks:` list:
--   - zone: LZ_AEN,  banks: 10, homes_per_bank: 50
--   - zone: LZ_CPS,  banks: 10, homes_per_bank: 50
--
-- Ids continue from the base fleet (hub-00000..01999, bank-000..039), which this script never touches:
--   LZ_AEN -> bank-040..049, hub-02000..02499
--   LZ_CPS -> bank-050..059, hub-02500..02999
-- Feeders group 5 banks per zone (banks_per_feeder=5, orchestrator.toml [fleet].banks_per_feeder):
--   feeder-LZ_AEN-00 (bank-040..044), feeder-LZ_AEN-01 (bank-045..049)
--   feeder-LZ_CPS-00 (bank-050..054), feeder-LZ_CPS-01 (bank-055..059)
-- Ratings: 39.2 kWh / 11 kW single-unit, 78.4 kWh / 20 kW dual-unit, 20% reserve (Base hardware,
-- confirmed 2026-09-25) -- 10 of each bank's 50 homes are dual-unit, same per-bank-offset rule as the
-- base fleet (k = hub's rank within its own bank; dual iff floor((k+1)*0.2) > floor(k*0.2), k=0..49),
-- so this SQL reproduces `_is_dual_unit`/`_dual_unit_mask` exactly without a Python runtime (same
-- approach as dev/seed/rebalance_dual_units.sql).
--
-- NOT run by this build. Apply by hand:
--   psql "$DSN" -v ON_ERROR_STOP=1 -f dev/seed/add_austin_fleet.sql
--
-- Idempotent: every INSERT is `ON CONFLICT (id) DO UPDATE SET ...` on the same columns
-- `opengrid.fleet.seed.seed_topology`'s upserts use, so re-running this script (e.g. after widening
-- `homes_per_bank` or re-running post a config change) converges to the same state rather than erroring
-- or duplicating rows. New hub_state rows use `DO NOTHING` (never overwrite a hub's live SoC/lease once
-- telemetry has started updating it).
--
-- ---------------------------------------------------------------------------------------------------
-- Live risk assessment (read before running)
-- ---------------------------------------------------------------------------------------------------
--
-- This script only ever INSERTs rows whose bank_id/hub_id do not exist in the base fleet (bank-040+,
-- hub-02000+) -- it never UPDATEs or references bank-000..039 / hub-00000..01999, and `og.reservation`/
-- `og.grant`/`og.obligation` only ever reference bank ids that a selector/allocator cycle has already
-- reserved capacity on, which can only be a bank that already existed before this script ran. So:
--
--   1. No existing reservation, grant, obligation, commitment, or lease is read, written, or
--      invalidated by this script -- it adds capacity nobody has ever been offered yet, on a load zone
--      (LZ_AEN/LZ_CPS) no existing contract or product_rule references (the selector/allocator/guardian
--      wiring for regulated-market zones is separate follow-up work, owned by the live-path agent; see
--      the build report's wiring notes). K2 (one buyer) and K13 (commitment lock) are therefore
--      unaffected: there is nothing on these new banks/hubs to double-book or reassign.
--   2. `og.hub`/`og.bank` have no FK *into* `og.obligation`/`og.reservation`/`og.grant` (checked against
--      0001_init.sql: those tables' FKs all point the other way, obligation/reservation -> bank_id/
--      hub_id as a plain TEXT reference, not the reverse), so adding new bank/hub rows cannot violate
--      any existing constraint or trigger a cascade.
--   3. The new hub_state rows are fresh inserts (`last_seen_at = now()`, `health = 'offline'` until the
--      ogsim simulator publishes real telemetry for them) -- they never touch an existing hub's state.
--
-- CONCLUSION: safe to run at any time with deliveries active on the EXISTING fleet (bank-000..039):
-- this script is additive-only and touches nothing the engine/guardian/allocator currently read for
-- those banks. The one hazard is operational, not transactional: the ogsim fleet simulator must be
-- restarted together with this script (same requirement as dev/seed/rebalance_dual_units.sql) --
-- `opengrid.fleet`'s twin loads `og.hub`/`og.bank` at og-engine startup (or on its own refresh cycle;
-- confirm which against the engine owner before relying on a live refresh), so until BOTH this script
-- has run AND ogsim/og-engine have restarted with `zone_blocks[].enabled: true`, telemetry for
-- hub-02000+ will not exist and these rows will simply show `offline`/stale -- harmless, but pointless
-- to run without the matching restart. Run this script, then restart `og-sim-fleet`/`og-engine`
-- together, in the same maintenance window.
--
-- Precondition check (informational only -- this script's own safety does not depend on it, per the
-- assessment above, but run it first to confirm the EXISTING fleet's delivery state before touching
-- anything in the same maintenance window):
--
--     SELECT o.obligation_id, o.state, o.window_start, o.window_end, r.bank_id
--     FROM og.obligation o
--     JOIN og.reservation r ON r.obligation_id = o.obligation_id
--     WHERE o.state IN ('DELIVERING', 'COMMITTED') AND r.released_at IS NULL;
--
-- ---------------------------------------------------------------------------------------------------

BEGIN;

WITH params AS (
    SELECT
        10    AS banks_per_zone,
        50    AS homes_per_bank,
        0.2   AS dual_unit_share,
        0.20  AS reserve_frac,
        78.4  AS e_kwh_dual,
        20.0  AS p_kw_dual,
        39.2  AS e_kwh_single,
        11.0  AS p_kw_single,
        600.0 AS bank_kva_rating,
        5     AS banks_per_feeder,
        40    AS base_bank_count,   -- existing bank-000..039; new banks start at bank-040
        2000  AS base_hub_count     -- existing hub-00000..01999; new hubs start at hub-02000
),
zone_blocks AS (
    -- One row per zone block, in the same order as fleet.yaml's `zone_blocks:` list -- LZ_AEN's ids
    -- start right after the base fleet, LZ_CPS's right after LZ_AEN's (never renumbering either).
    SELECT 'LZ_AEN' AS zone, 0 AS block_rank
    UNION ALL
    SELECT 'LZ_CPS' AS zone, 1 AS block_rank
),
zone_offsets AS (
    SELECT
        zb.zone,
        p.base_bank_count + zb.block_rank * p.banks_per_zone AS bank_offset,
        p.base_hub_count + zb.block_rank * p.banks_per_zone * p.homes_per_bank AS hub_offset
    FROM zone_blocks zb, params p
),
new_banks AS (
    SELECT
        zo.zone,
        zo.bank_offset + b AS bank_idx,
        b AS rank_in_zone
    FROM zone_offsets zo, params p, generate_series(0, p.banks_per_zone - 1) AS b
),
bank_rows AS (
    SELECT
        'bank-' || lpad(nb.bank_idx::text, 3, '0') AS bank_id,
        nb.zone,
        p.bank_kva_rating AS kva_rating,
        0.0 AS reserve_kva,
        'feeder-' || nb.zone || '-'
            || lpad((nb.rank_in_zone / p.banks_per_feeder)::text, 2, '0') AS feeder_id
    FROM new_banks nb, params p
)
INSERT INTO og.bank (bank_id, zone, kva_rating, reserve_kva, feeder_id)
SELECT bank_id, zone, kva_rating, reserve_kva, feeder_id FROM bank_rows
ON CONFLICT (bank_id) DO UPDATE SET
    zone = EXCLUDED.zone, kva_rating = EXCLUDED.kva_rating, reserve_kva = EXCLUDED.reserve_kva,
    feeder_id = EXCLUDED.feeder_id;

WITH params AS (
    SELECT
        10 AS banks_per_zone, 50 AS homes_per_bank, 0.2 AS dual_unit_share, 0.20 AS reserve_frac,
        78.4 AS e_kwh_dual, 20.0 AS p_kw_dual, 39.2 AS e_kwh_single, 11.0 AS p_kw_single,
        40 AS base_bank_count, 2000 AS base_hub_count
),
zone_blocks AS (
    SELECT 'LZ_AEN' AS zone, 0 AS block_rank UNION ALL SELECT 'LZ_CPS' AS zone, 1 AS block_rank
),
zone_offsets AS (
    SELECT
        zb.zone,
        p.base_bank_count + zb.block_rank * p.banks_per_zone AS bank_offset,
        p.base_hub_count + zb.block_rank * p.banks_per_zone * p.homes_per_bank AS hub_offset
    FROM zone_blocks zb, params p
),
new_hubs AS (
    SELECT
        zo.zone,
        zo.hub_offset + j AS hub_idx,
        zo.bank_offset + (j % p.banks_per_zone) AS bank_idx,
        j / p.banks_per_zone AS k
    FROM zone_offsets zo, params p, generate_series(0, p.homes_per_bank * p.banks_per_zone - 1) AS j
),
hub_rows AS (
    SELECT
        'hub-' || lpad(nh.hub_idx::text, 5, '0') AS hub_id,
        'bank-' || lpad(nh.bank_idx::text, 3, '0') AS bank_id,
        nh.zone,
        (floor((nh.k + 1) * p.dual_unit_share) > floor(nh.k * p.dual_unit_share)) AS is_dual,
        p.e_kwh_dual, p.p_kw_dual, p.e_kwh_single, p.p_kw_single, p.reserve_frac
    FROM new_hubs nh, params p
)
INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw, eta_c, eta_d)
SELECT
    hr.hub_id, hr.bank_id, hr.zone,
    CASE WHEN hr.is_dual THEN hr.e_kwh_dual ELSE hr.e_kwh_single END AS e_kwh,
    CASE WHEN hr.is_dual THEN hr.e_kwh_dual ELSE hr.e_kwh_single END * hr.reserve_frac AS r_kwh,
    CASE WHEN hr.is_dual THEN hr.p_kw_dual  ELSE hr.p_kw_single  END AS p_kw,
    0.9487 AS eta_c, 0.9487 AS eta_d
FROM hub_rows hr
ON CONFLICT (hub_id) DO UPDATE SET
    bank_id = EXCLUDED.bank_id, zone = EXCLUDED.zone, e_kwh = EXCLUDED.e_kwh, r_kwh = EXCLUDED.r_kwh,
    p_kw = EXCLUDED.p_kw, eta_c = EXCLUDED.eta_c, eta_d = EXCLUDED.eta_d;

-- hub_state: one row per new hub, `offline` until the (separately restarted) simulator publishes real
-- telemetry -- DO NOTHING so a re-run of this script never clobbers a hub's live state once telemetry
-- has started (unlike og.hub/og.bank's ratings, which are safe to re-affirm on every run).
INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, health, lease_epoch, last_seen_at)
SELECT h.hub_id, h.r_kwh, 0.0, 'offline', 0, now()
FROM og.hub h
WHERE h.hub_id >= 'hub-02000' AND h.hub_id NOT IN (SELECT hub_id FROM og.hub_state)
ON CONFLICT (hub_id) DO NOTHING;

COMMIT;

-- ---------------------------------------------------------------------------------------------------
-- Post-run verification (read-only)
-- ---------------------------------------------------------------------------------------------------
--
-- 1,000 new hubs, 20 new banks, base fleet untouched:
--
--     SELECT count(*) FROM og.hub WHERE hub_id >= 'hub-02000';     -- expect 1000
--     SELECT count(*) FROM og.bank WHERE bank_id >= 'bank-040';    -- expect 20
--     SELECT count(*) FROM og.hub WHERE hub_id < 'hub-02000';      -- expect 2000, unchanged
--     SELECT count(*) FROM og.bank WHERE bank_id < 'bank-040';     -- expect 40, unchanged
--
-- 10 dual-unit homes per new bank, every new bank single-zone:
--
--     SELECT bank_id, zone, count(*) FILTER (WHERE p_kw = 20.0) AS dual_unit_homes, count(*) AS total
--     FROM og.hub WHERE hub_id >= 'hub-02000' GROUP BY bank_id, zone ORDER BY bank_id;
--     -- expect dual_unit_homes = 10, total = 50 on every row; zone constant per bank_id
--
-- Every new bank has a feeder (K4/G-06 needs one to evaluate):
--
--     SELECT bank_id, feeder_id FROM og.bank WHERE bank_id >= 'bank-040' AND feeder_id IS NULL;
--     -- expect zero rows
