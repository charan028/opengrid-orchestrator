set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== heartbeats ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT process, ts, now() - ts AS age FROM og.heartbeat ORDER BY process"
date
