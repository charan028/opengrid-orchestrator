# OpenGrid Orchestrator — local dev stack (WP D0)

A local `docker compose` stack that mirrors production's topology (see `deploy/README.md`) at
laptop scale, so contributors and their AI agents can run integration tests without access to
the base server (192.168.5.35).

**Status: runs on Docker Engine 29.8.0 (Docker Desktop, WSL2, Windows 11), 2026-09-26.** Both
`dev-up` and `dev-up-full` (`--profile orchestrator`) come up healthy: `migrate` applies every
migration and seeds 8 banks / 200 hubs, the four sims publish telemetry over MQTT, og-engine runs
its cycles and gates, og-guardian signs verdicts, and og-api answers on 127.0.0.1:8080. Every
published port binds to 127.0.0.1 only.

The first real run found dev/-only bugs, all fixed here:

- shell scripts checked out with CRLF on Windows broke the Postgres init script and
  `mosquitto-init` (`dev/.gitattributes` now forces LF for `*.sh`);
- `dev/secrets.example` was loaded after `dev/secrets` as an `env_file`, so its blank values
  overrode the real ones (it is no longer loaded);
- the Mosquitto password file was root-owned with mode 600, so the broker's own user could not read
  it (`gen-mosquitto-passwd.sh` now chowns it);
- the configs lacked the D-12 identities (`[api.roles]`, the two-person release allow-list), the
  guardian public key og-safestop needs to relay a release, the sim-market token URL, and
  `dev/config/tdsp_tariffs.toml`;
- the orchestrator image lacked `python-multipart` (see `dev/docker/orchestrator.Dockerfile`);
- `gen-keys.*` fell back to a bare `python` in a worktree without its own `.venv`
  (`OPENGRID_VENV_PYTHON` now points it at a shared one).

The static checks made before any Docker run are kept at the end.

## What's in the stack

| Service | Image / build | Purpose |
|---|---|---|
| `postgres` | `postgres:17` | databases `og` (app data) + `og_test` (pytest), owner `opengrid` |
| `mosquitto-init` | `eclipse-mosquitto:2` | one-shot: hashes `dev/secrets`' passwords into a Mosquitto password file |
| `mosquitto` | `eclipse-mosquitto:2` | broker, same 6-user ACL model as production (`dev/mosquitto/opengrid.acl`) |
| `migrate` | `dev/docker/orchestrator.Dockerfile` | one-shot: `opengrid.platform.db migrate` then `opengrid.fleet.seed` |
| `sim-market` | `dev/docker/sims.Dockerfile` | `ogsim.market` (ERCOT/EIA/NWS stand-in), port 8090 |
| `sim-fleet` | `dev/docker/sims.Dockerfile` | `ogsim.fleet`, 200 hubs / 8 banks by default |
| `sim-scada` | `dev/docker/sims.Dockerfile` | `ogsim.scada`, 8 banks to match `sim-fleet` |
| `sim-control` | `dev/docker/sims.Dockerfile` | `ogsim.control` anomaly control plane (REST + web UI), port 8091 |
| `og-*` (6 services) | `dev/docker/orchestrator.Dockerfile` | **optional** `orchestrator` profile: the orchestrator's own 6 processes, containerized |

The orchestrator's processes (`og-feeds`/`og-engine`/`og-guardian`/`og-safestop`/`og-settle`/
`og-api`) are **not started by default** — per BUILD.md, run them from your IDE or venv against
this stack's published ports. Use the optional `orchestrator` compose profile
(`make -C dev dev-up-full`) if you'd rather not set up a local venv.

Switching the orchestrator between live ERCOT/EIA/NWS and this stack's simulator is
configuration only (`dev/config/dev.toml`'s `[feeds.*]` base URLs already point at
`sim-market`) — never a code path, matching `deploy/README.md`'s "Switching live APIs vs. the
market simulator" section.

## Prerequisites

- Docker Engine + the `docker compose` CLI plugin, v2.20 or newer (the compose file uses the
  `env_file: [{path, required}, ...]` long form for optional secrets files, added in that
  version). `docker compose version` should print at least that.
- GNU Make, for the `dev/Makefile` targets below (WSL/Git Bash/macOS/Linux have it; on plain
  Windows, run the equivalent `docker compose` commands directly — each Makefile target's body
  is one or two commands, readable in `dev/Makefile`).
- Python (the repo's `.venv` at the repo root, or any Python 3.11+ with PyYAML) for the ACL
  generator and key generator scripts, which run on the host, not in a container.
- Ports free on your machine: 5432 (Postgres), 1883 (Mosquitto), 8090 (market sim), 8091
  (control sim), and 8080 if you use the `orchestrator` profile. Override any of them in
  `dev/.env` (see `dev/.env.example`).

## Quickstart

```bash
cd dev
make dev-up
```

`dev-up` will, in order:

1. Copy `dev/secrets.example` → `dev/secrets` and `dev/.env.example` → `dev/.env` if they don't
   exist yet (both gitignored — **never commit them**; the placeholder passwords are DEV-ONLY
   and are fine to use as-is for a fully local stack).
2. Regenerate `dev/mosquitto/opengrid.acl` from `dev/scripts/gen_mosquitto_acl.py` (mirrors
   production's `/etc/mosquitto/opengrid.acl` topic grants exactly, topic root `og/v1`).
3. Generate `dev/keys/{guardian,safestop}-dev.{key,pub}` via
   `dev/scripts/gen-keys.sh` (skips regenerating a keypair that already exists — pass
   `--force` to rotate).
4. `docker compose up -d --build` for Postgres, Mosquitto, `migrate`, and the 4 simulators.

Watch it come up:

```bash
docker compose -f dev/docker-compose.yml ps
docker compose -f dev/docker-compose.yml logs -f migrate   # should print "Applied N migration(s)" then "seeded 8 banks, 200 hubs"
```

Bring it down: `make dev-down` (keeps the named volumes — Postgres data, Mosquitto secrets —
so the next `dev-up` doesn't re-migrate/re-seed from scratch). For a full wipe, `make dev-reset`
(drops the named volumes and the generated dev keys).

### Running the orchestrator itself

From your IDE or venv, against the stack above:

```bash
cd orchestrator
OG_CONFIG=../dev/config/dev.toml python -m opengrid.api.main      # http://127.0.0.1:8080/og/
OG_CONFIG=../dev/config/dev.toml python -m opengrid.engine.main
OG_CONFIG=../dev/config/dev.toml python -m opengrid.guardian.main
OG_CONFIG=../dev/config/dev.toml python -m opengrid.safestop.main
OG_CONFIG=../dev/config/dev.toml python -m opengrid.settle.main
OG_CONFIG=../dev/config/dev.toml python -m opengrid.feeds.main
```

(Run each from the repo root instead, with `OG_CONFIG=dev/config/dev.toml`, if you prefer —
`dev/config/dev.toml`'s `guardian.key_path`/`safestop.key_path` are relative paths,
`dev/keys/guardian-dev.key`/`dev/keys/safestop-dev.key`, so pick one working directory and be
consistent.) Or skip all of this and run `make dev-up-full` to get all 6 as containers instead
(`dev/config/docker.toml`, used automatically by that profile).

## Running the integration tests

The task brief for this stack says:

```bash
OG_CONFIG=dev/config/dev.toml pytest orchestrator/tests/integration
```

**This alone is not sufficient** with the test suite as it exists today —
`orchestrator/tests/integration/conftest.py` (owned by the `architect` role, not this WP) skips
every integration test unless `OG_DB` is set, and its Postgres/MQTT fixtures read `OG_DB`,
`OG_MQTT_ROOT`, `PGHOST`, `PGPORT`, `MQTT_HOST`, `MQTT_PORT` directly from the environment —
not from `OG_CONFIG`. Several per-suite `conftest.py`/test files *do* read `OG_CONFIG` (falling
back to `orchestrator/config/test.toml` if unset) for their own Postgres connection. So to
actually exercise the dev stack, set both:

```bash
export OG_CONFIG=dev/config/dev.toml
export OG_DB=og_test
export OG_MQTT_ROOT=og/v1
export PGHOST=127.0.0.1 PGPORT=5432
export MQTT_HOST=127.0.0.1 MQTT_PORT=1883
export OG_DB_PASSWORD=<same value as dev/secrets' OG_DB_PASSWORD>
cd .. # repo root
.venv/bin/python -m pytest orchestrator/tests/integration -q
```

or just `make -C dev dev-test`, which sets all of the above for you (reads the password
requirement the same way `dev/secrets` does — export `OG_DB_PASSWORD` yourself first, or
`source`/parse `dev/secrets` in your shell, since Make doesn't read that file for you).

Tests that need MQTT auth will additionally need the relevant `OG_MQTT_<ROLE>_PASSWORD` set to
match `dev/secrets` (e.g. `OG_MQTT_ENGINE_PASSWORD` for anything touching `opengrid.engine`'s
MQTT client) — most existing integration tests only touch Postgres, so this is usually not
needed yet.

## Injecting anomalies

`sim-control` runs on `http://localhost:8091` (published from the container's port 8091):

```bash
# Web UI
open http://localhost:8091/            # or just visit it in a browser

# CLI (talks to the running control plane over REST)
cd integration-sims
OGSIM_CONTROL_URL=http://localhost:8091 python -m ogsim.control list-types
OGSIM_CONTROL_URL=http://localhost:8091 python -m ogsim.control inject \
    --type price_spike --target np6-905-cd --params '{"value_usd_per_mwh": 5000}' --duration 300
OGSIM_CONTROL_URL=http://localhost:8091 python -m ogsim.control list-active
OGSIM_CONTROL_URL=http://localhost:8091 python -m ogsim.control random status
OGSIM_CONTROL_URL=http://localhost:8091 python -m ogsim.control run-scenario \
    ../integration-sims/scenarios/price_spike_during_delivery.yaml
```

Autonomous random-mode anomalies are on by default (`integration-sims/config/random.yaml`,
profile `normal`) — every sim rolls its own catalogue as a Poisson process even if you never
inject anything manually. `random set-profile --profile calm|stressed|chaos` and
`random pause`/`random resume` control that.

## Calling og-api, and the live UI tests (dev proxy)

og-api believes `X-Remote-User` only together with `X-OG-Proxy-Auth` equal to
`OG_API_PROXY_SECRET` (`opengrid.api.auth`). In production Apache sets both; the dev stack has no
Apache, so a browser or `curl` pointed straight at `127.0.0.1:8080` gets `401`.
`tests-e2e/functional` sends the secret itself (it reads `dev/secrets`). For a browser, or
`tests-e2e/ui` in live mode, run the dev proxy, which stands in for Apache:

```bash
python dev/scripts/dev_proxy.py            # 127.0.0.1:8088 -> og-api on 127.0.0.1:8080
OG_UI_BASE_URL=http://127.0.0.1:8088 PYTHONPATH=orchestrator/src python -m pytest tests-e2e/ui -q
```

It listens on loopback only, replaces any client `X-OG-Proxy-Auth` with the one from
`dev/secrets` (never printed), passes the client's `X-Remote-User` through as the Basic-Auth user
Apache would set, and streams responses, so SSE works. Any local process can therefore choose an
identity through it: use it on the dev stack only, never deploy it. Identities with a role in
`dev/config/*.toml` `[api.roles]`: `operator`, `viewer`, `og-op-a`, `og-op-b`.

`/og/api/health` answers only connections from loopback *inside* the og-api container, so from the
host it returns `403` even through the proxy (Docker's port forwarding arrives from the bridge
gateway). The System Health screen itself works, since og-api reads its own API from inside.

## Scale runs (12-scale-test-plan)

`docs/orchestrator/07-delivery/12-scale-test-plan.md` runs the fleet at 2,000 and 10,000 hubs.
`dev/docker-compose.scale.yml` does that on this stack with dev-only presets,
`dev/config/fleet.{2k,10k}.dev.yaml` and `scada.{2k,10k}.dev.yaml` (50 hubs per bank, as
production), picked by `OG_SCALE_PRESET`. It runs as its own compose project, so the scale
database never mixes with the normal one; stop the normal stack first (same host ports):

```bash
cd dev
docker compose -f docker-compose.yml --profile orchestrator stop
OG_SCALE_PRESET=10k docker compose -p ogscale -f docker-compose.yml -f docker-compose.scale.yml --profile orchestrator up -d --build
docker compose -p ogscale -f docker-compose.yml logs migrate      # "seeded 200 banks, 10000 hubs"
# og-engine's /metrics is loopback-only inside its container:
docker compose -p ogscale -f docker-compose.yml exec -T og-engine python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:9101/metrics').read().decode())"
docker stats --no-stream                                           # CPU and memory per container
docker compose -p ogscale -f docker-compose.yml -f docker-compose.scale.yml --profile orchestrator down -v
docker compose -f docker-compose.yml --profile orchestrator up -d
```

This is a laptop-scale approximation of the plan (one host, Docker Desktop), not the server run
the plan describes; report it as such.

## Resetting

- `make dev-down` — stop containers, keep data (Postgres volume, Mosquitto password volume).
- `make dev-reset` — stop containers, **drop** the Postgres/Mosquitto volumes and the generated
  `dev/keys/*.key`/`*.pub` files, so the next `dev-up` starts completely fresh (re-migrates,
  re-seeds, regenerates keys). Destructive — only your local dev data, never anything on the
  base server.
- To reseed the fleet topology without a full reset (e.g. after editing
  `dev/config/fleet.dev.yaml`): `docker compose -f dev/docker-compose.yml run --rm migrate`.

## Troubleshooting

If something does not come up, capture and report, in whichever channel/PR you're using:

- `docker compose -f dev/docker-compose.yml version` and OS/Docker version.
- The exact command that failed and its full output.
- `docker compose -f dev/docker-compose.yml logs <service>` for whichever service didn't come
  up (`postgres`, `mosquitto-init`, `mosquitto`, `migrate`, `sim-market`, `sim-fleet`,
  `sim-scada`, `sim-control` are the likely suspects, roughly in startup order).

Known risk areas worth checking first if something's wrong:

- **`mosquitto-init` failing**: `mosquitto_passwd` needs all 6 `OG_MQTT_*_PASSWORD` values
  present — check `dev/secrets` exists and `docker compose ... logs mosquitto-init`.
- **`sim-fleet`/`sim-scada` never publishing telemetry**: check they log a successful MQTT
  connect to `mosquitto:1883` as `og_sim`, and that `dev/mosquitto/opengrid.acl` was actually
  regenerated (`make -C dev dev-acl` and diff against the committed copy) before `mosquitto`
  started — the ACL is baked in at container start, not hot-reloaded.
- **`migrate` failing to reach `sim-fleet`'s hub count**: `opengrid.fleet.seed` and
  `dev/docker-compose.yml`'s `sim-fleet` service must both point at
  `dev/config/fleet.dev.yaml` (`OG_FLEET_SIM_CONFIG` / `OGSIM_FLEET_CONFIG` respectively) — if
  someone changes one without the other, telemetry from real hub ids will be dropped as
  "unknown hub_id" (the exact bug `opengrid/fleet/seed.py`'s module docstring describes from
  production).
- **Guardian-signed commands rejected by `sim-fleet`**: the dev keypair
  (`dev/keys/guardian-dev.pub`) must be the *public* key mounted into `sim-fleet` and the
  matching *private* key (`dev/keys/guardian-dev.key`) must be what `og-guardian`/`opengrid.
  engine` actually sign with (`dev/config/dev.toml`'s `[guardian].key_path`, or
  `dev/config/docker.toml`'s if you're using the `orchestrator` profile) — a `dev-reset`
  followed by only running `dev-keys` again for one side but not restarting the other will
  desync them.
- **Port already in use**: change the relevant `*_PORT` in `dev/.env` and re-run `make dev-up`.

## Verification performed before the first Docker run

Docker was not available where this stack was first built (neither locally nor on the base
server), so the following were checked instead. They still apply whenever these files change:

- `docker-compose.yml` parses with `yaml.safe_load` and passes a structural sanity check
  (every declared service has an `image`/`build`, every `depends_on` target and named `volumes`
  reference exists, the `orchestrator` profile has exactly 6 services).
- Every module path used in a compose `command:` (`opengrid.platform.db`, `opengrid.fleet.seed`,
  `opengrid.feeds.main`, `opengrid.engine.main`, `opengrid.guardian.main`,
  `opengrid.safestop.main`, `opengrid.settle.main`, `opengrid.api.main`, `ogsim.market`,
  `ogsim.fleet`, `ogsim.scada`, `ogsim.control`) imports cleanly in the local `.venv`.
- `dev/config/dev.toml` and `dev/config/docker.toml` parse as valid TOML;
  `dev/config/fleet.dev.yaml` and `dev/config/scada.dev.yaml` parse as valid YAML with the
  expected `hub_count`/`bank_count`.
- `dev/scripts/gen_mosquitto_acl.py`'s output was diffed against production's
  `/etc/mosquitto/opengrid.acl` (read read-only over ssh, per BUILD.md) — identical per-user
  topic grants, only the topic root parametrized (both happen to be `og/v1`).
- `dev/scripts/gen-keys.ps1` was run locally: it produced a 32-byte guardian private seed, a
  32-byte safestop private seed, and 65-character-hex `.pub` files, matching
  `opengrid.guardian.keys`/`opengrid.safestop.keys`'s own format exactly (they're the same
  code).

What these could not show (that `docker compose up` succeeds, that the containers reach each
other, that Mosquitto accepts the password file, that telemetry and commands flow end to end) was
confirmed by the first real run on 2026-09-26; see Status at the top.
