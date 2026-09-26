set -euo pipefail
cd /opt/opengrid/src
tar -xzf /tmp/docs_sync.tgz -C /opt/opengrid/src
git add docs/team/lane-correctness.md docs/team/lane-ux-perf-demo.md
git status --short
if git diff --cached --quiet; then
  echo "nothing to commit"
  exit 0
fi
git commit -m "Add per-developer lane docs (docs/team/lane-correctness.md, lane-ux-perf-demo.md) for Gitea contributors"
git push origin main
git rev-parse HEAD
