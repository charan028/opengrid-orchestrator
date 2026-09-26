# dev/Makefile -- WP D0 local dev stack targets.
#
# BUILD.md's top-level Makefile (repo root) is owned by the `architect` role (directory
# ownership table) and already exists without a dev-stack target; rather than edit a path this
# WP doesn't own, `dev-up`/`dev-down`/`dev-test` live here instead. Run them as
# `make -C dev dev-up` from the repo root, or `cd dev && make dev-up`.

.PHONY: dev-up dev-up-full dev-down dev-test dev-reset dev-keys dev-acl dev-logs

COMPOSE ?= docker compose -f docker-compose.yml

# Prefer the repo's own venv if present (Linux/Mac layout, then Windows/Git-Bash layout).
PY ?= $(shell [ -x ../.venv/bin/python ] && echo ../.venv/bin/python || \
       ( [ -x ../.venv/Scripts/python.exe ] && echo ../.venv/Scripts/python.exe || echo python3 ))

secrets:
	@if [ ! -f secrets ]; then \
		echo "dev/secrets not found -- copying dev/secrets.example (DEV-ONLY placeholder passwords)"; \
		cp secrets.example secrets; \
	fi

.env:
	@if [ ! -f .env ]; then \
		echo "dev/.env not found -- copying dev/.env.example"; \
		cp .env.example .env; \
	fi

dev-acl:
	$(PY) scripts/gen_mosquitto_acl.py

dev-keys:
	sh scripts/gen-keys.sh

## Bring up Postgres + Mosquitto + migrate + the 4 simulators (orchestrator processes run
## from your IDE/venv -- see dev/README.md).
dev-up: secrets .env dev-acl dev-keys
	$(COMPOSE) up -d --build postgres mosquitto-init mosquitto migrate sim-market sim-fleet sim-scada sim-control

## Same as dev-up, plus the optional `orchestrator` profile (all 6 orchestrator processes,
## containerized, for contributors who don't want a local venv).
dev-up-full: secrets .env dev-acl dev-keys
	$(COMPOSE) --profile orchestrator up -d --build

dev-down:
	$(COMPOSE) down

## Full reset: also drops the named volumes (Postgres data, Mosquitto password/data/log) and
## the generated dev keys, so the next dev-up starts from nothing. Destructive -- confirm
## before running against a stack you care about.
dev-reset:
	$(COMPOSE) down -v
	rm -rf keys/*.key keys/*.pub

## Run the integration tests against the dev stack. See dev/README.md for the exact env vars
## this needs beyond OG_CONFIG (orchestrator/tests/integration/conftest.py keys off OG_DB/
## OG_MQTT_ROOT directly, not OG_CONFIG, for its Postgres/MQTT fixtures).
dev-test:
	cd .. && OG_CONFIG=dev/config/dev.toml OG_DB=og_test OG_MQTT_ROOT=og/v1 \
		PGHOST=127.0.0.1 PGPORT=5432 MQTT_HOST=127.0.0.1 MQTT_PORT=1883 \
		$(CURDIR)/$(PY) -m pytest orchestrator/tests/integration -q

dev-logs:
	$(COMPOSE) logs -f
