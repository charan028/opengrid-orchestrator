set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== command_batch count ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT count(*) FROM og.command_batch"
echo "=== verdict count/outcomes ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT outcome, count(*) FROM og.verdict GROUP BY outcome"
echo "=== guardian heartbeat ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT process, now()-ts AS age FROM og.heartbeat"
