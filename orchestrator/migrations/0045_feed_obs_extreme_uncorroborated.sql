-- FR-ING-117 / V-P1 (04-external-data-integration.md): a real-time price outside the normal band
-- (-$250..$5,000/MWh) but inside the hard bounds is stored and flagged EXTREME_UNCORROBORATED until
-- corroborated, never dropped or clipped. og.feed_obs.quality gains that value (the spec's quality set,
-- S3.2, of which GOOD/ESTIMATED/STALE were the subset implemented so far).
--
-- The CHECK is widened to a strict superset of 0001's list. Added NOT VALID (no table scan under the
-- ACCESS EXCLUSIVE lock, which is held only for the catalog change), then VALIDATEd under a SHARE UPDATE
-- EXCLUSIVE lock that does not block the live ingest. lock_timeout bounds any wait behind it.

SET LOCAL lock_timeout = '5s';

ALTER TABLE og.feed_obs DROP CONSTRAINT IF EXISTS feed_obs_quality_check;
ALTER TABLE og.feed_obs ADD CONSTRAINT feed_obs_quality_check
    CHECK (quality IN ('GOOD', 'ESTIMATED', 'STALE', 'EXTREME_UNCORROBORATED')) NOT VALID;
ALTER TABLE og.feed_obs VALIDATE CONSTRAINT feed_obs_quality_check;
