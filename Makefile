.PHONY: test-unit test-int lint format typecheck dupcheck coverage migrate check bootstrap-check schema-check schema-snapshot

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

# Consolidated schema snapshot (orchestrator/schema/og_schema.sql): pg_dump --schema-only of a fresh database
# after every migration. schema-check fails when the migrations no longer produce exactly the committed file;
# schema-snapshot regenerates it (commit the result with the migration that changed it). Test cluster only.
SCHEMA_DB ?= og_t_schema
schema-check:
	bash deploy/scripts/create_schema.sh --fresh-db --check-snapshot \
		--db-port $(BOOT_PORT) --db-name $(SCHEMA_DB) --db-role $(BOOT_ROLE) --etc $(BOOT_ETC)

schema-snapshot:
	bash deploy/scripts/create_schema.sh --fresh-db --snapshot orchestrator/schema/og_schema.sql \
		--db-port $(BOOT_PORT) --db-name $(SCHEMA_DB) --db-role $(BOOT_ROLE) --etc $(BOOT_ETC)

# --- performance and scalability (tests-perf/README.md, tests-perf/HANDOFF.md; report 07-delivery/17) --------
# Default target: the Docker dev stack (compose project ogperf). The base target needs the production watchdog.
PERF_PY ?= .venv-perf/bin/python
PERF_TARGET ?= compose
PERF_STEPS ?= 1000 2500 3500 5000 7500
PERF_STEP_MIN ?= 15
PERF_SOAK_MIN ?= 60
PERF_RUN ?= tests-perf/.run
PERF_ASSETS ?= docs/orchestrator/07-delivery/assets/perf
.PHONY: perf-campaign perf-scale perf-analyze perf-clean perf-check

perf-campaign:
	$(PERF_PY) tests-perf/campaign.py --fresh --target $(PERF_TARGET) --guard on --steps "$(PERF_STEPS)" --step-min $(PERF_STEP_MIN) --soak-min $(PERF_SOAK_MIN)

perf-scale:
	$(PERF_PY) tests-perf/campaign.py --fresh --target $(PERF_TARGET) --guard on --steps "$(PERF_STEPS)" --step-min $(PERF_STEP_MIN) --soak-min 0 --no-stress

perf-analyze:
	$(PERF_PY) tests-perf/analyze.py --run $(PERF_RUN) --charts $(PERF_ASSETS) --md $(PERF_RUN)/data/steps.md

perf-clean:
	docker compose -p ogperf -f dev/docker-compose.yml -f tests-perf/compose/docker-compose.perf.yml --profile orchestrator down -v
	rm -rf $(PERF_RUN) tests-perf/compose/generated

perf-check:
	cd tests-perf && ../$(PERF_PY) -m ruff check . && ../$(PERF_PY) -m ruff format --check .
	cd tests-perf && ../$(PERF_PY) -m mypy --config-file mypy.ini perfenv.py targets.py sampler.py stress.py analyze.py campaign.py compose/seed_extra.py
	cd tests-perf && ../$(PERF_PY) -m pytest -q -p no:cacheprovider test_perf_harness.py
