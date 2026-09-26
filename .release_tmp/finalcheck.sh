set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== og-engine NRestarts ==="
systemctl show og-engine -p NRestarts
echo "=== guardian verdict outcomes ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT outcome, count(*) FROM og.verdict GROUP BY 1"
echo "=== recent verdict veto reasons ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT outcome, unnest(vetoed_rule_ids) AS rule, count(*) FROM og.verdict WHERE created_at > now() - interval '3 minutes' GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15"
echo "=== signed batches ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT count(*) FROM og.verdict WHERE outcome='PASS' AND signature IS NOT NULL"
echo "=== commitments (customers) ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT count(*), count(DISTINCT c.customer_id) FROM og.commitment cm JOIN og.obligation o ON o.obligation_id=cm.obligation_id JOIN og.contract c ON c.contract_id=o.contract_id"
echo "=== obligation states ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT service_type, state, count(*) FROM og.obligation GROUP BY 1,2"
echo "=== energy status rows ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT count(*), count(*) FILTER (WHERE at_risk) FROM og.obligation_energy_status"
echo "=== settle pnl/invoice ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT (SELECT count(*) FROM og.pnl) AS pnl, (SELECT count(*) FROM og.invoice_line) AS invoice"
