set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
psql -h 127.0.0.1 -U opengrid -d og -c "
SELECT 'heartbeat' AS what, now() - ts AS age FROM og.heartbeat WHERE process='engine'
UNION ALL
SELECT 'hub_state max last_seen_at', now() - max(last_seen_at) FROM og.hub_state
UNION ALL
SELECT 'telemetry max ts', now() - max(ts) FROM og.telemetry
"
echo "--- rowcount affected sanity: hub_state distinct last_seen_at values ---"
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT count(DISTINCT last_seen_at) FROM og.hub_state"
