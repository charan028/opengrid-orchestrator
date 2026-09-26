-- 0019: set the seeded ERCOT_AS (Non-Spin) product rule's duration to ERCOT's 4-hour stored-energy
-- requirement (additive/data-only, BUILD.md S1: 0002_seed_demo.sql is already applied and is never
-- edited in place; same pattern as 0008 and 0018).
--
-- Bug (Frank, bug list #6): 0002 seeded the Non-Spin product rule with duration_minutes = 60. Since the
-- Dec 5, 2025 RTC+B go-live, NPRR1282 requires an ESR to hold 4 hours of stored energy per MW of
-- Non-Spin awarded (1 hour for ECRS). `selector.gate` now reads `duration_minutes` for AS candidates as
-- `sustained_hours`, and `selector.model` holds that energy above each bank's reserve while the award
-- is held, so the seeded value must be the real requirement.

SET search_path TO og;

UPDATE og.product_rule
SET duration_minutes = 240
WHERE contract_id = '00000000-0000-7000-8000-000000000d03'
  AND product_code = 'NSPIN'
  AND duration_minutes = 60;
