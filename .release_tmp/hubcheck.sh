set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== hub_state health distribution ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT health, count(*) FROM og.hub_state GROUP BY health"
echo "=== hub_state row count vs og.hub count ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT (SELECT count(*) FROM og.hub) AS hubs, (SELECT count(*) FROM og.hub_state) AS hub_states"
echo "=== sample hub_state rows ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT hub_id, health, last_seen_at, lease_epoch FROM og.hub_state ORDER BY last_seen_at DESC NULLS LAST LIMIT 5"
