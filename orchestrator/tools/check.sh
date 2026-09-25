#!/usr/bin/env bash
# Pre-merge quality gate (BUILD.md S5a). The merge agent runs this before integrating any branch/path.
# Fails fast (set -e) on the first violation; run each `make` target individually to see the rest.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

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

echo "All checks passed."
