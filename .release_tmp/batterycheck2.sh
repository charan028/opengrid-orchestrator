set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== og.hub e_kwh/r_kwh/p_kw distribution ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT e_kwh, r_kwh, p_kw, count(*) FROM og.hub GROUP BY 1,2,3"
echo "=== guardian verdict outcomes/reasons ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT outcome, unnest(vetoed_rule_ids) AS rule, count(*) FROM og.verdict GROUP BY 1,2 ORDER BY 3 DESC LIMIT 20"
