#!/usr/bin/env bash
# Pre-merge quality gate (BUILD.md S5a). The merge agent runs this before integrating any branch/path.
# Fails fast (set -e) on the first violation; run each `make` target individually to see the rest.
#
# A7 (merge task): this now also exercises the integration-sims (`ogsim`) suite, in its own venv, so a
# merge never green-lights an orchestrator change that silently broke the wire contract the simulators
# validate against (interfaces/). `opengrid` and `ogsim` still share no code (BUILD.md S1) -- this just
# runs both suites back to back from one entry point.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "== ruff check =="
python -m ruff check src tests tools

echo "== ruff format --check =="
python -m ruff format --check src tests tools

echo "== mypy (strict on opengrid.core, default elsewhere) =="
python -m mypy src

echo "== dupcheck (no re-implemented core formulas, no ogsim<->opengrid import) =="
python tools/dupcheck.py

echo "== unit tests =="
python -m pytest tests/unit -q

echo "== coverage (>=85% on core/trace) =="
python -m pytest tests/unit -q \
    --cov=opengrid.core --cov=opengrid.trace --cov-report=term-missing --cov-fail-under=85

# --- integration-sims (ogsim) -------------------------------------------------------------------
# Runs from its own venv (/opt/ogsim/venv on the server; OGSIM_VENV overrides for local dev) since
# opengrid and ogsim intentionally never share a virtualenv or a dependency lock (BUILD.md S1: "they
# share no code"). Skipped with a warning (not a failure) when that venv doesn't exist, e.g. on a
# workstation that only has the orchestrator's .venv -- the server-side merge run is what gates a
# release (BUILD.md S6/deploy README).
SIMS_DIR="$REPO_ROOT/../integration-sims"
OGSIM_VENV="${OGSIM_VENV:-/opt/ogsim/venv}"
OGSIM_PY="$OGSIM_VENV/bin/python"

if [[ -x "$OGSIM_PY" && -d "$SIMS_DIR" ]]; then
    echo "== integration-sims: ruff check =="
    (cd "$SIMS_DIR" && "$OGSIM_PY" -m ruff check src tests)

    echo "== integration-sims: mypy =="
    (cd "$SIMS_DIR" && "$OGSIM_PY" -m mypy src)

    echo "== integration-sims: unit tests =="
    (cd "$SIMS_DIR" && "$OGSIM_PY" -m pytest tests -q)
else
    echo "== integration-sims: SKIPPED (no venv at $OGSIM_VENV -- set OGSIM_VENV or run on the server) =="
fi

echo "All checks passed."
