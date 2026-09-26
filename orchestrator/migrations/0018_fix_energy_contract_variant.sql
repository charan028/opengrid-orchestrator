-- 0018: relabel the seeded ERCOT_ENERGY demo contract's variant from NCLR to ALR (additive/data-only,
-- BUILD.md S1: "never renumber existing migrations" -- 0002_seed_demo.sql is an already-applied
-- migration and must never be edited in place; this is a new, idempotent data-correction migration,
-- the next free number after checking the local tree, which has 0001-0017).
--
-- Bug (Frank, demo-critical): 0002_seed_demo.sql seeded contract
-- '00000000-0000-7000-8000-000000000d02' (service_type ERCOT_ENERGY) with variant 'NCLR'. Per
-- 02a-mvp-s-spec-engine.md S7.1's baseline table, ERCOT_ENERGY's performance baseline is "ALR
-- set-point tracking vs. simulated UDSP" -- an ALR (Aggregated Load Resource) is ERCOT's continuously
-- dispatchable resource type; an NCLR (Non-Controllable Load Resource) is, by definition (FR-CTR-019/
-- FR-CTR-026, 01-product/02-functional-requirements.md), never given real-time dispatch instructions.
-- This contract IS continuously energy-dispatched (`opengrid.contracts.intake._intake_energy`,
-- `product_rule.product_code = 'ENERGY'`, `variable_kind = 'CONTINUOUS'`), so labelling it NCLR
-- contradicts its own product rule and the spec's baseline table. Verified before this migration was
-- written that no code branches on the string 'NCLR' for this contract (`orchestrator/src`,
-- `integration-sims/src` both grep clean), so this is a pure data relabel with no behavioural
-- dependency to also fix.

SET search_path TO og;

UPDATE og.contract
SET variant = 'ALR'
WHERE contract_id = '00000000-0000-7000-8000-000000000d02'
  AND service_type = 'ERCOT_ENERGY'
  AND variant = 'NCLR';
