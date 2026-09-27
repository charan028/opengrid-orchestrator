-- 0053: og.verdict.published_at (r3.4.3, SAFETY with DISPATCH, 2026-09-27). Additive only.
--
-- The guardian stamps it once a signed (PASS) batch's commands are published (guardian.main, after
-- publish_command_batch). For G-04's utility-scale anchor both sides count a batch as "signed" only when
-- outcome = 'PASS' AND published_at IS NOT NULL: the engine's anchor read and the guardian's startup reload
-- (guardian.repo.load_signed_anchors). A PASS whose publish failed stays NULL and never becomes an anchor.
-- NULL on every row written before this migration.

SET search_path TO og;

ALTER TABLE og.verdict ADD COLUMN IF NOT EXISTS published_at timestamptz;
