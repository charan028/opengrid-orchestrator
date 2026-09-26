set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== feed_obs by source/product ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT source, product, count(*), max(ts) FROM og.feed_obs GROUP BY 1,2 ORDER BY 1,2"
echo "=== feed_status ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT * FROM og.feed_status"
