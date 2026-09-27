-- 0046: D-37 NOIE switch (r3.4.1). Additive only: every existing row stays valid, and nothing is backfilled
-- here (dev/seed/market_model_seed.sql on a fresh install, deploy/scripts/noie_switch_apply.py on an
-- existing database write the rows).
--
--   * og.utility.utility_id  -- also LCRA and RAYBURN: LZ_LCRA / LZ_RAYBN are regulated (NOIE) territory
--                               like Austin Energy (D-37 supersedes D-32). Mirrors core UtilityId.
--   * og.contract.name       -- a display name (nullable).
--   * og.contract.is_sample  -- a sample contract ("Sample Contract: ..."): never ACTIVE, so never callable,
--                               never reserved, never billed (CHECK contract_sample_inactive_check), and its
--                               name must say so (CHECK contract_sample_name_check).
--   * og.bank.availability   -- AVAILABLE | UNAVAILABLE, the ONE availability representation the selector,
--     availability_reason       allocator, guardian, UI and APIs share (a hub inherits its bank's). Reason
--     availability_since        REGULATED_NO_CONTRACT: regulated territory (no ERCOT, K15) and no utility
--                               capacity contract. availability_since is when the state last changed; K13
--                               grandfathering compares an obligation's created_at against it.
--
-- The widened CHECK lists are strict supersets, so they are added NOT VALID (no scan; the ACCESS EXCLUSIVE
-- lock is held only for the catalog change), like 0041/0044. New and updated rows are checked either way.

SET LOCAL lock_timeout = '5s';

ALTER TABLE og.utility DROP CONSTRAINT IF EXISTS utility_utility_id_check;
ALTER TABLE og.utility ADD CONSTRAINT utility_utility_id_check
    CHECK (utility_id IN ('AUSTIN_ENERGY', 'CPS_ENERGY', 'LCRA', 'RAYBURN')) NOT VALID;

ALTER TABLE og.contract ADD COLUMN IF NOT EXISTS name text;
ALTER TABLE og.contract ADD COLUMN IF NOT EXISTS is_sample boolean NOT NULL DEFAULT false;

ALTER TABLE og.contract DROP CONSTRAINT IF EXISTS contract_sample_inactive_check;
ALTER TABLE og.contract ADD CONSTRAINT contract_sample_inactive_check
    CHECK (NOT is_sample OR status <> 'ACTIVE');

ALTER TABLE og.contract DROP CONSTRAINT IF EXISTS contract_sample_name_check;
ALTER TABLE og.contract ADD CONSTRAINT contract_sample_name_check
    CHECK (NOT is_sample OR (name IS NOT NULL AND name LIKE 'Sample Contract%'));

ALTER TABLE og.bank ADD COLUMN IF NOT EXISTS availability text NOT NULL DEFAULT 'AVAILABLE';
ALTER TABLE og.bank ADD COLUMN IF NOT EXISTS availability_reason text;
ALTER TABLE og.bank ADD COLUMN IF NOT EXISTS availability_since timestamptz;

ALTER TABLE og.bank DROP CONSTRAINT IF EXISTS bank_availability_check;
ALTER TABLE og.bank ADD CONSTRAINT bank_availability_check
    CHECK (availability IN ('AVAILABLE', 'UNAVAILABLE'));

-- An UNAVAILABLE bank always says why and since when; an AVAILABLE one carries no reason.
ALTER TABLE og.bank DROP CONSTRAINT IF EXISTS bank_availability_reason_check;
ALTER TABLE og.bank ADD CONSTRAINT bank_availability_reason_check CHECK (
    (availability = 'AVAILABLE' AND availability_reason IS NULL)
    OR (availability = 'UNAVAILABLE' AND availability_reason IN ('REGULATED_NO_CONTRACT')
        AND availability_since IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS ix_bank_unavailable ON og.bank (bank_id) WHERE availability = 'UNAVAILABLE';
