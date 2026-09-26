set -euo pipefail
cd /opt/opengrid/src
tar -xzf /tmp/src_sync.tgz -C /opt/opengrid/src
git add -A
git status --short | head -80
echo "=== diffstat ==="
git diff --cached --stat | tail -20
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "Security hardening pass: CSRF protection, safestop seed-print fix, mosquitto ACL; verify intake wiring live

- opengrid.api.csrf: double-submit-cookie CSRF middleware (Origin/Referer allowlist +
  per-session token), wired over api and ui routes in api/app.py; base.html carries
  hx-headers so every HTMX screen echoes the token with no per-template changes
- opengrid.safestop.keys keygen: no longer prints the raw seed by default (--print-seed
  opt-in only), matching opengrid.guardian.keys.keygen's existing behavior (F-05)
- /etc/mosquitto/opengrid.acl (server-side): removed the blanket ogtest/# pattern that
  applied to every og_* user unconditionally (F-04); documented the test-workspace
  toggle in deploy/README.md; confirmed all 10 og-*/og-sim-* units stayed connected
  through a mosquitto restart
- tests-e2e/smoke.py: A2 now reads GET /og/api/fleet/summary (O(1) count) instead of
  paging the default 200-row /fleet/hubs list
- qa/merge-notes.md: live verification after redeploy -- A1/A2/A4/A9/A10/A11(injection)
  now pass; A4/A5/A6/A8 still fail, but the root cause has moved from the bank-id bug
  (now fixed) to intake not generating ERCOT_ENERGY/ERCOT_AS opportunities and the one
  DIST_DEFERRAL opportunity never getting selected by the LP -- documented for the
  contracts/selector owner, not fixed here
- CONTRIBUTING.md, WORKBOARD.md: added at repo root for external contributors joining
  through Gitea (wp/* branches, routed through merge for the gate + server integration)
"
git push origin main
git rev-parse HEAD
