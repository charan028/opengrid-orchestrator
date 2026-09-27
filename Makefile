.PHONY: test-unit test-int lint format typecheck dupcheck coverage migrate check bootstrap-check

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

# From-scratch seed check (deploy/BOOTSTRAP.md): phases c-e into a FRESH database on the disposable test
# cluster (5433; --fresh-db is refused on 5432), then deploy/scripts/bootstrap_check.py asserts the counts.
# Run as root on the base server, from a clone; secrets for the test role go to BOOT_ETC, never /etc/opengrid.
BOOT_DB ?= og_t_boot
BOOT_PORT ?= 5433
BOOT_ROLE ?= og_boot
BOOT_ETC ?= /srv/ogwork/bootstrap/etc
bootstrap-check:
	bash deploy/scripts/bootstrap_from_scratch.sh --phase c-e --fresh-db \
		--db-port $(BOOT_PORT) --db-name $(BOOT_DB) --db-role $(BOOT_ROLE) --etc $(BOOT_ETC)
