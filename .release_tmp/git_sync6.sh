set -euo pipefail
cd /opt/opengrid/src
git fetch origin main
git status --short
echo "=== ahead/behind ==="
git rev-list --left-right --count HEAD...origin/main || true
git merge --ff-only origin/main
tar -xzf /tmp/src_sync.tgz -C /opt/opengrid/src
git add -A
git status --short | head -80
echo "=== diffstat ==="
git diff --cached --stat | tail -20
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "Dispatch-live pass: commitments fix verification, health evaluator wiring, SCADA feed_obs persistence, 39.2 kWh / 11 kW fleet resize

- opengrid.settle.main: wire opengrid.health.run(pool, cfg) as a third concurrent loop
  (was never called by any process anywhere -- no ALR-* alert could ever fire); fix
  _PROCESS_NAME 'og-settle' -> 'settle' to match opengrid.health.model.ALL_PROCESSES
- opengrid.fleet: add record_scada_observation, called from ingest_scada_signal, so
  SCADA readings land in og.feed_obs (source='scada') for guardian's G-03 check and the
  ALR-SCADA-OVERLOAD alert -- both already read that table, nothing ever wrote it
- opengrid.fleet.__init__: fix a pre-existing mypy arg-type error (HubState.health)
- migration 0008 applied live (NONSPIN -> NSPIN product code fix)
- verified live: A1/A9/A10/A11 (both injection and alert) pass; A2 passes except in the
  brief post-restart telemetry-catchup window; A4 (grants) passes; A4/A5/A6/A8 (selector
  commitments) still fail -- opportunities now generate for ERCOT_AS/DIST_DEFERRAL but
  the LP never selects any into COMMITTED (qa/merge-notes.md S16); guardian vetoes are
  100% G-14 (trace pre-image not found), not G-03 -- not a side effect of the battery/
  inverter resize, flagged for the energy-sufficiency agent (S17)
- fleet resize to 39.2 kWh / 11 kW (already landed in all 4 files by the time this pass
  reseeded); verified all 2,000 og.hub rows show e_kwh=39.2, r_kwh=7.84, p_kw=11
"
git push origin main
git rev-parse HEAD
