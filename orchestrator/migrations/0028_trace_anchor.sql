-- 0028: K11 external anchoring audit log (00-invariants.md, adversarial review). Additive only.
-- 0024-0027 exist (0027 reserved/unused as of this writing); 0028 is the next free number.
--
-- og.trace_anchor records every periodic publish of the trace chain's checkpoint hash to a location
-- OUTSIDE the database (opengrid.trace.anchoring.publish_anchor): a signed JSON file under
-- [trace].anchor_dir (default /var/lib/opengrid/anchors in production) plus a second, independent copy
-- under [trace].anchor_secondary_dir. og.trace_checkpoint.anchor_ref (already a column since
-- 0001_init.sql) is stamped with the primary anchor file's path; this table is the structured, queryable
-- audit trail opengrid.invariants' anchor-freshness check reads.

CREATE TABLE IF NOT EXISTS og.trace_anchor (
    anchor_id       uuid PRIMARY KEY,
    checkpoint_id   uuid REFERENCES og.trace_checkpoint(checkpoint_id),
    published_at    timestamptz NOT NULL DEFAULT now(),
    checkpoint_hash text NOT NULL,
    primary_path    text NOT NULL,
    secondary_path  text,
    key_id          text,
    signature       text
);
CREATE INDEX IF NOT EXISTS ix_trace_anchor_published ON og.trace_anchor (published_at DESC);
