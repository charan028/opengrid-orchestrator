set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== opportunity states ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT service_type, state, count(*) FROM og.opportunity op JOIN og.contract c ON c.contract_id=op.contract_id GROUP BY 1,2"
echo "=== obligation states ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT service_type, state, count(*) FROM og.obligation GROUP BY 1,2"
echo "=== plan count/status ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT plan_mode, solver_status, count(*) FROM og.plan GROUP BY 1,2 ORDER BY 3 DESC LIMIT 10"
