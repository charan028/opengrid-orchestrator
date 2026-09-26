-- 0022: switch the seeded ERCOT_AS demo contract from Non-Spin (NSPIN, 4 h deployment) to ECRS (1 h)
-- (lead decision 2026-09-26). Data-only, idempotent. Runs after 0020, which set NSPIN's duration to its
-- correct 240 minutes; any other Non-Spin contract keeps that.
--
-- Why: a 500 kW Non-Spin award must hold 500 x 4 h / 0.9487 ~ 2.1 MWh above reserve, far beyond what the
-- demo banks store (~500 kWh above reserve on bank-000), so every hold would be flagged AT_RISK. An ECRS
-- award of the same size needs ~527 kWh for 1 h; the seeded split (352 kW on bank-000) needs ~371 kWh.
-- ECRS is a real NP4-188-CD ancillaryType, so intake's MCPC lookup (feed_obs.series = product_code) keeps
-- working. Existing obligations keep their contract; only the product basis of new awards changes.

SET search_path TO og;

UPDATE og.contract
SET variant = 'ECRS'
WHERE contract_id = '00000000-0000-7000-8000-000000000d03'
  AND service_type = 'ERCOT_AS'
  AND variant = 'NSPIN';

UPDATE og.product_rule
SET product_code = 'ECRS', duration_minutes = 60
WHERE contract_id = '00000000-0000-7000-8000-000000000d03'
  AND product_code = 'NSPIN'
  AND NOT EXISTS (
      SELECT 1 FROM og.product_rule p2
      WHERE p2.contract_id = '00000000-0000-7000-8000-000000000d03' AND p2.product_code = 'ECRS'
  );
