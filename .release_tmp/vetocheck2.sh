set -a
. /etc/opengrid/secrets.env
set +a
export PGPASSWORD=$OG_DB_PASSWORD
echo "=== og.verdict columns ==="
psql -h 127.0.0.1 -U opengrid -d og -c "\d og.verdict"
echo "=== veto reasons overall ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT outcome, unnest(vetoed_rule_ids) AS rule, count(*) FROM og.verdict GROUP BY 1,2 ORDER BY 3 DESC LIMIT 15"
echo "=== recent verdicts via command_batch join ==="
psql -h 127.0.0.1 -U opengrid -d og -c "SELECT v.outcome, unnest(v.vetoed_rule_ids) AS rule, count(*) FROM og.verdict v JOIN og.command_batch cb ON cb.command_batch_id = v.command_batch_id WHERE cb.created_at > now() - interval '3 minutes' GROUP BY 1,2 ORDER BY 3 DESC"
