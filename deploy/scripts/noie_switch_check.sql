-- deploy/scripts/noie_switch_check.sql -- READ-ONLY pre/post check for the D-37 NOIE switch (r3.4.1).
--
-- D-37: LZ_LCRA and LZ_RAYBN become regulated (NOIE-style utility) territory. K13 commitment lock: every
-- COMMITTED or DELIVERING obligation that already holds a live reservation on one of their banks must
-- complete untouched; the switch applies to NEW commitments only (grandfathering: opengrid.market.
-- territory.grandfathered_banks, used by the selector, the allocator and the guardian).
--
-- Lists those obligations, their contract's market, the banks and the reserved window. Small by design:
-- starts from the live obligations (ix_obligation_state), joins their reservations (ix_reservation_obligation).
-- Changes nothing (the whole run is a READ ONLY transaction).
--
--   psql "$DSN" -v ON_ERROR_STOP=1 -f deploy/scripts/noie_switch_check.sql

\set ON_ERROR_STOP on
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '15s';

\echo '== banks per switching zone'
SELECT zone, count(*) AS banks, min(bank_id) AS first_bank, max(bank_id) AS last_bank
FROM og.bank WHERE zone IN ('LZ_LCRA', 'LZ_RAYBN') GROUP BY zone ORDER BY zone;

\echo '== COMMITTED/DELIVERING obligations with a live reservation on an LZ_LCRA/LZ_RAYBN bank'
WITH live AS (
    SELECT o.obligation_id, o.contract_id, o.service_type, o.state, o.window_start, o.window_end,
           o.committed_qty_kw
    FROM og.obligation o
    WHERE o.state IN ('COMMITTED', 'DELIVERING')
)
SELECT l.obligation_id, l.service_type, l.state, c.market, c.utility_id,
       l.window_start AT TIME ZONE 'America/Chicago' AS window_start_ct,
       l.window_end AT TIME ZONE 'America/Chicago' AS window_end_ct,
       l.committed_qty_kw, b.zone, count(DISTINCT r.bank_id) AS banks,
       round(max(r.amount) FILTER (WHERE r.kind = 'POWER_KW'), 1) AS max_bank_kw
FROM live l
JOIN og.contract c ON c.contract_id = l.contract_id
JOIN og.reservation r ON r.obligation_id = l.obligation_id AND r.released_at IS NULL
JOIN og.bank b ON b.bank_id = r.bank_id AND b.zone IN ('LZ_LCRA', 'LZ_RAYBN')
GROUP BY l.obligation_id, l.service_type, l.state, c.market, c.utility_id, l.window_start, l.window_end,
         l.committed_qty_kw, b.zone
ORDER BY l.window_start, l.obligation_id, b.zone;

\echo '== summary: live obligations per service type touching the switching zones'
SELECT o.service_type, o.state, count(DISTINCT o.obligation_id) AS obligations,
       min(o.window_start) AT TIME ZONE 'America/Chicago' AS first_start_ct,
       max(o.window_end) AT TIME ZONE 'America/Chicago' AS last_end_ct
FROM og.obligation o
JOIN og.reservation r ON r.obligation_id = o.obligation_id AND r.released_at IS NULL
JOIN og.bank b ON b.bank_id = r.bank_id AND b.zone IN ('LZ_LCRA', 'LZ_RAYBN')
WHERE o.state IN ('COMMITTED', 'DELIVERING')
GROUP BY o.service_type, o.state ORDER BY 1, 2;

\echo '== territory rows today (og.utility, og.asset utility_id for the switching zones)'
SELECT utility_id, name, territory_zones FROM og.utility ORDER BY utility_id;
SELECT zone, asset_class, coalesce(utility_id, '(none: ERCOT competitive)') AS utility_id, count(*)
FROM og.asset WHERE zone IN ('LZ_LCRA', 'LZ_RAYBN') GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;

ROLLBACK;
