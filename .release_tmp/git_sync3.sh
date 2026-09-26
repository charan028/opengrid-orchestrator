set -euo pipefail
cd /opt/opengrid/src
tar -xzf /tmp/src_sync.tgz -C /opt/opengrid/src
git add -A
git status --short | head -60
echo "=== diffstat ==="
git diff --cached --stat | tail -20
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "Dispatch-live pass: fleet topology seeding, allocator/engine wiring, guardian lease_state, safestop key fix, health alerts

- opengrid.fleet.seed: idempotent og.hub/og.bank loader matching ogsim.fleet's id scheme
  (hub-NNNNN/bank-NNN), read from integration-sims/config/fleet.yaml; wired into deploy.sh
  after migrations, and POST /og/api/admin/seed-fleet-topology (operator-only)
- opengrid.engine.gateways (new): real FleetGateway/LedgerGateway/ScadaGateway/ScheduleGateway
  adapters; opengrid.engine.main now wires opengrid.ledger.configure() (was never called
  anywhere -- every selector.run_gate -> ledger.reserve() call was raising RuntimeError) and
  calls allocator.run_cycle for real every 2s instead of NotImplementedError
- opengrid.fleet.pg_backend.upsert_hub_states: single multi-row INSERT via psycopg.sql
  composition instead of ~2000 per-row round trips per cycle (was starving the 2s tick budget
  and keeping every hub perpetually stuck at stale health); fixes ruff S608 too
- opengrid.guardian.repo.PgLeaseStatePort: durable og.lease_state (epoch/seq) read/write,
  replacing InMemoryLeaseStatePort so G-13 freshness survives a guardian restart
- opengrid.safestop.keys: keygen CLI pubkey format bug fixed (was writing key_id + hex space
  separated, ogsim's loader only accepts bare hex) -- guardian's own keygen already had this right
- opengrid.health: new ALR-SCADA-OVERLOAD alert rule (bank load over kva_rating, tuned to the
  anomaly catalogue's default bank_overload injection); condition_key_for extended for bank_id
- tools/check.sh, tests-e2e/smoke.py, migrations 0004/0005 landed in the previous merge pass

Two real blockers found live, documented in qa/merge-notes.md, not fixed here (outside owned
paths / need architecture sign-off):
- selector.gate._configured_bank_ids() generates bank-01..bank-NN (2-digit, 1-indexed);
  real topology is bank-000..bank-039 (3-digit, 0-indexed) -- disjoint id spaces, blocks
  every selector.run_gate call with LookupError
- og-sim-fleet publishes one telemetry burst after (re)start then goes idle without crashing
  or logging -- confirmed via og.telemetry/og.hub_state timestamps vs a healthy og-engine
  heartbeat; blocks A2 and, downstream, guardian's own independent hub telemetry cache (A6/A8)
"
git push origin main
git rev-parse HEAD
