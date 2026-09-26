set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== og.hub e_kwh/p_kw distribution ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT e_kwh, p_kw, count(*) FROM og.hub GROUP BY 1,2 ORDER BY 1"
echo "=== og.bank kva_rating distribution ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT kva_rating, count(*) FROM og.bank GROUP BY 1"
