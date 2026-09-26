-- Fix the ERCOT_AS demo contract/product-rule's `product_code`: `0002_seed_demo.sql` seeded
-- `'NONSPIN'`, but ERCOT's real NP4-188-CD `ancillaryType` values (confirmed live: `og.feed_obs`
-- rows with `product = 'np4-188-cd'`) are `REGUP`, `REGDN`, `RRS`, `NSPIN`, `ECRS`
-- (`forecast/README.md`'s canonical series-key table lists `NSPIN`, `RRS`, `ECRS` as examples;
-- `feeds/normalize.py::ercot_as_price_to_feed_obs` writes the real `ancillaryType` string verbatim
-- as `feed_obs.series`). `opengrid.contracts.intake._intake_as` reads
-- `state.market.latest_as_mcpc_usd_per_mwh(rule.product_code)`, which looks up
-- `feed_obs.series = rule.product_code` -- with `product_code = 'NONSPIN'` that lookup never
-- matches a real row, so `mcpc` is always `None` and no `ERCOT_AS` opportunity is ever generated
-- despite live AS prices flowing into `feed_obs` (qa/merge-notes.md S15's "intake does not appear to
-- be generating any ERCOT_ENERGY/ERCOT_AS opportunities" finding, ERCOT_AS half). Corrects the
-- already-applied 0002 seed's contract `variant` and product_rule `product_code` in place (an UPDATE,
-- not a re-INSERT, since 0002 already ran on every environment).

SET search_path TO og;

UPDATE og.contract
SET variant = 'NSPIN'
WHERE contract_id = '00000000-0000-7000-8000-000000000d03'
  AND service_type = 'ERCOT_AS'
  AND variant = 'NONSPIN';

UPDATE og.product_rule
SET product_code = 'NSPIN'
WHERE contract_id = '00000000-0000-7000-8000-000000000d03'
  AND product_code = 'NONSPIN';
