set -euo pipefail
cd /opt/opengrid/src
git add -A
git status --short | head -50
echo "=== diffstat ==="
git diff --cached --stat | tail -20
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "Merge pass: schema/ledger/fleet integration fixes, deploy fixes, e2e smoke test

- migrations 0004 (bank_id uuid->text across og.reservation/og.grant, FK to og.bank),
  0005 (og.lease_state for guardian epoch/seq)
- ledger: GrantRecord/GrantBackend + persist_grants insert-only writer for og.grant
- fleet: HubCapabilitySnapshot + hub_capabilities() per-hub read for the allocator's
  engine-wiring gap (see qa/merge-notes.md)
- deploy/scripts/deploy.sh: source secrets.env/api_keys.env before running migrations
  (every prior deploy failed at the migration step without this)
- deploy/systemd/og-{engine,guardian,feeds,safestop,settle}.service: fix ExecStart to
  invoke each package's .main entry point instead of the bare package
- api/app.py: mount opengrid.ui under [ui].base_path (/og) instead of the app root,
  fixing the live UI 404 at https://base.tocy-net.net/og/
- tools/check.sh: also run the integration-sims (ogsim) suite from its own venv
- tests-e2e/smoke.py: end-to-end smoke test against acceptance items A1-A11
- qa/merge-notes.md: cross-agent interface notes and remaining gaps (allocator engine
  wiring, fleet topology seeding, safestop pubkey CLI format)
"
git push origin main
git rev-parse HEAD
