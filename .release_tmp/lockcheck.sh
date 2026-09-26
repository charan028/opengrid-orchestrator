set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== long-running / blocked queries ==="
psql -h 127.0.0.1 -U opengrid -d og -c "
SELECT pid, now() - query_start AS duration, wait_event_type, wait_event, state, left(query, 100)
FROM pg_stat_activity
WHERE state != 'idle' AND pid != pg_backend_pid()
ORDER BY duration DESC LIMIT 15
"
