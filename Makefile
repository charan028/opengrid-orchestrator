.PHONY: test-unit test-int lint format typecheck dupcheck coverage migrate check

PY ?= python

test-unit:
	cd orchestrator && $(PY) -m pytest tests/unit -q

test-int:
	cd orchestrator && $(PY) -m pytest tests/integration -q

lint:
	cd orchestrator && $(PY) -m ruff check src tests tools
	cd orchestrator && $(PY) -m ruff format --check src tests tools

format:
	cd orchestrator && $(PY) -m ruff format src tests tools
	cd orchestrator && $(PY) -m ruff check --fix src tests tools

typecheck:
	cd orchestrator && $(PY) -m mypy src

dupcheck:
	cd orchestrator && $(PY) tools/dupcheck.py

coverage:
	cd orchestrator && $(PY) -m pytest tests/unit -q \
		--cov=opengrid.core --cov=opengrid.trace --cov-report=term-missing --cov-fail-under=85

migrate:
	cd orchestrator && $(PY) -m opengrid.platform.db migrate

# Full pre-merge gate (BUILD.md S5a). Mirrors orchestrator/tools/check.sh.
check: lint typecheck dupcheck test-unit coverage
