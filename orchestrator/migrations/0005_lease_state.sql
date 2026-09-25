-- Merge fix (A4, BUILD.md merge task): og.lease_state -- the per-bank epoch/sequence state the
-- guardian needs to reject stale/replayed command batches (02a S6 lease/epoch check) and that
-- og-engine bumps on every command batch it proposes for a bank.
--
-- Today the guardian reads the batch itself from the RT_ALLOCATION trace pre-image
-- (og.trace, decision_type='RT_ALLOCATION', read via opengrid.guardian.repo.PgProposalPort) -- that
-- hand-off is being kept as-is (see qa/merge-notes.md) rather than adding a command_batch_item table,
-- since the trace pre-image already carries the full batch and is itself the append-only, hash-chained
-- record the guardian is meant to re-derive its verdict from. What was missing is durable storage for
-- the lease epoch/sequence *state* itself (as opposed to the one-shot batch payload) so the guardian
-- can detect an out-of-order or replayed batch across process restarts, not just within one in-memory
-- run. This table is that missing state; the guardian's own read/write code against it belongs to the
-- safety fix agent (recorded in qa/merge-notes.md), not to merge.

SET search_path TO og;

CREATE TABLE og.lease_state (
    bank_id     text PRIMARY KEY REFERENCES og.bank(bank_id),
    epoch       bigint NOT NULL DEFAULT 0,
    seq         bigint NOT NULL DEFAULT 0,
    updated_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE og.lease_state IS
    'Per-bank (epoch, seq) high-water mark for command-batch freshness (02a S6 lease check). '
    'Written by og-engine when it proposes a batch; read by og-guardian to reject a batch whose '
    '(epoch, seq) does not strictly advance the stored value, guarding against replay/out-of-order '
    'delivery across restarts of either process.';
