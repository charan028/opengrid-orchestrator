-- 0044: og.asset accepts asset_class MOBILE_STORAGE (D-31 trucks). Additive.
--
-- The guardian rates a hub at its nameplate og.hub.p_kw (never the 11 kW home per-unit cap, G-02) when it
-- belongs to an og.asset SUBSTATION or MOBILE_STORAGE row (guardian.repo._ALL_HUB_PARAMS_SQL). 0025's CHECK
-- allowed only HOME_BANK/SUBSTATION, so a truck's asset row was refused and every truck stayed at 11 kW.
-- dev/seed/mobile_trucks_seed.sql writes one MOBILE_STORAGE row per truck (asset_id = hub_id, bank_id =
-- its single-hub bank 'bank-<truck id>').
--
-- The new list is a strict superset of 0025's, so it is added NOT VALID (no scan, the ACCESS EXCLUSIVE
-- lock held only for the catalog change), like 0041. New and updated rows are checked either way.
-- A MOBILE_STORAGE asset must name its bank (like HOME_BANK). It carries no POI and no substation_id: a
-- truck is not sited at a substation; 0025's asset_substation_link applies to SUBSTATION rows only.

SET LOCAL lock_timeout = '5s';

ALTER TABLE og.asset DROP CONSTRAINT IF EXISTS asset_asset_class_check;
ALTER TABLE og.asset ADD CONSTRAINT asset_asset_class_check
    CHECK (asset_class IN ('HOME_BANK', 'SUBSTATION', 'MOBILE_STORAGE')) NOT VALID;

ALTER TABLE og.asset DROP CONSTRAINT IF EXISTS asset_mobile_storage_link;
ALTER TABLE og.asset ADD CONSTRAINT asset_mobile_storage_link
    CHECK (asset_class <> 'MOBILE_STORAGE' OR bank_id IS NOT NULL) NOT VALID;