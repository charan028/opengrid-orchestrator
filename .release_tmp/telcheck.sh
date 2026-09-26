set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== distinct telemetry ts values, last 20 ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT ts, count(*) FROM og.telemetry GROUP BY ts ORDER BY ts DESC LIMIT 20"
