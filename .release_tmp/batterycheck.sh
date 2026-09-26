set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== og.hub e_kwh/r_kwh distribution ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT e_kwh, r_kwh, count(*) FROM og.hub GROUP BY 1,2"
echo "=== sim/scada units ==="
systemctl is-active og-sim-fleet og-sim-scada
