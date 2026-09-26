set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== open alerts ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT rule, severity, summary, opened_at FROM og.alert WHERE cleared_at IS NULL ORDER BY opened_at DESC LIMIT 10"
echo "=== settle heartbeat ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT process, now()-ts AS age FROM og.heartbeat WHERE process='og-settle'"
echo "=== bank loads (bank-000) ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT value FROM og.feed_obs WHERE source='scada' AND product='bank-000' AND series='APPARENT_POWER_KVA' ORDER BY ts DESC LIMIT 3"
