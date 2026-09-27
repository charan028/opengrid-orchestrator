# OpenGrid on Kubernetes: installing from scratch

`deploy/k8s/install.sh` installs the solution on a new Kubernetes cluster: the orchestrator (every og-* service,
the HTMX UI and the API), Postgres 17, the Mosquitto broker with the production ACL, the migrations and seeds, the
data-lifecycle CronJob and the ogsim integration simulators (market, fleet, scada, control, utility). It first lists
and checks every prerequisite. It stops on any failure, and every phase is safe to re-run.

> **Status (r3.4.3, chart 0.2.0, 2026-09-27): lint-verified only, never deployed to a real cluster.** The chart
> passes `helm lint --strict` (helm 3.22.0 and 4.3.0) and `kubeconform -strict` (Kubernetes 1.30 and 1.33 schemas,
> five value sets, grid link and its Service included). The scripts pass `shellcheck`, and `install.sh --dry-run`
> runs end to end. **No r3.4.3 smoke test was run** (see [Known gaps](#known-gaps)). The last smoke test is
> r3.4.1's (chart 0.1.0): every workload ran from the built images in one isolated network namespace with podman,
> using the chart's rendered ConfigMaps. `create_schema.sh` applied migrations 0001 to 0044; the seed Job and
> `bootstrap_check.py --live` passed; every heartbeat was fresh; `/og/api/health` returned 200; and the gateway
> returned 401 without credentials and 200 as operator. No Kubernetes cluster was available, so the first real
> install is also the first integration test of the Kubernetes objects themselves (scheduling, PVCs, Services,
> Ingress, NetworkPolicies). Treat it as a pilot, and read [Known gaps](#known-gaps).
>
> Chart 0.2.0 (`appVersion: "r3.4.3"`) adds the r3.4.3 objects: og-sim-utility, the optional D-34 grid link, the
> D-35 AS poll switch, the og-safestop metrics port and the D-37 NOIE blocks; see
> [r3.4.3 features on Kubernetes](#r343-features-on-kubernetes).

This page covers Kubernetes only. To run on a base server (the production host), see [../BOOTSTRAP.md](../BOOTSTRAP.md).
The installer reuses the same building blocks as that script:

| Step | Reused from |
|---|---|
| role, database, schema `og`, migrations 0001 to latest | `deploy/scripts/create_schema.sh` (bootstrap phases c and d) |
| seeds, in order, then the count check | `deploy/scripts/bootstrap_from_scratch.sh --phase e` |
| sim configs with the approved zone blocks | `deploy/scripts/gen_sim_overrides.py` (bootstrap phase f) |
| MQTT ACL | `deploy/scripts/render_mosquitto_acl.sh` (bootstrap phase g) |
| Ed25519 keys | the orchestrator's keygen CLIs, run by bootstrap phase h inside the image (`og-entrypoint keygen-tar`) |
| basic auth and the proxy secret | `deploy/apache/opengrid.conf`, unchanged except the `/ogsim/` upstream (bootstrap phase i) |
| service commands and memory limits | `deploy/systemd/*.service` |
| verification | `deploy/scripts/bootstrap_check.py --live` |

## What gets deployed

All object names start with the release name (default `opengrid`, set with `--release`).

| Workload | Kind | Image | Notes |
|---|---|---|---|
| og-engine, og-guardian, og-safestop, og-feeds, og-settle | Deployment (1 replica, `Recreate`) | `opengrid-orchestrator` | Singletons: two engines or two guardians must never run at the same time. Each has a `wait-schema` init container and a readiness probe on its `og.heartbeat` row. og-engine (9101), og-guardian (9103) and, from chart 0.2.0, og-safestop (9106) also get a metrics Service and a `/metrics` liveness probe. og-settle also runs the health evaluator and, in r3.4.3, the D-38 delivery verification job (`[delivery].enabled = true`, every 15 s); the PQ waveform ingest runs in og-engine. With the grid link enabled, og-engine mounts Secret `og-gridlink` and reads it through `OG_GRID_LINK_CONFIG`. og-settle is the only one with a PVC (30 GiB). |
| og-api + `gateway` sidecar | Deployment | `opengrid-orchestrator` + `httpd` | og-api keeps its loopback bind (localhost:8080). Apache in the same pod (port 8081) enforces basic auth and adds the proxy secret, as production's Apache does. An `htpasswd` init container builds the password file from `og-ui-users`. |
| Postgres 17.11 | StatefulSet + PVC (20 GiB) | `postgres` | Optional: use an existing server with `--external-db`. |
| Mosquitto 2.0.22 | Deployment | `eclipse-mosquitto` | Port 1883. No anonymous access. Init containers render the production ACL and one hashed password per account in `mosquitto.users` (og_engine, og_guardian, og_api, og_safestop, og_sim, og_simctl, og_sim_customer, og_sim_utility; og_gridlink only with the grid link). The ACL grants, og_sim_utility's included, come from `render_mosquitto_acl.sh`; og_gridlink's block from `provision_grid_link_user.render_acl`. Retained messages live on an emptyDir. |
| migrate, seed | Job | `opengrid-orchestrator` | migrate runs `create_schema.sh` with the bundled server's superuser password (with `--external-db`, only the migration runner). seed runs r3.4.3's `bootstrap_from_scratch.sh --phase e` (`noie_switch_seed.sql` always, the trucks, the topology last; phase f runs as a non-root user). Both are named by a hash of their inputs (image tag; tag plus zones for seed), so re-runs with the same inputs do nothing. |
| og-lifecycle | CronJob, **suspended**, every 10 min | `opengrid-orchestrator` | Matches production's disabled `og-lifecycle.timer`. Scheduled next to og-settle, whose PVC holds its cold store. |
| og-sim-market (8090), -fleet, -scada, -control (8091), -utility; -customer off | Deployment | `opengrid-sims` | The same set that `ogsim.target` starts. market and control get a Service. fleet and scada get a `sim-configs` init container (the approved zone blocks). og-sim-utility (r3.4.3, `ogsim.utility_aen`) is the Austin Energy EMS simulator: MQTT user `og_sim_utility`, and it calls the utility API as `og-util-aen` through the gateway. |
| Ingress | `/og/`, `/ogsim/` go to the gateway | | Works with any controller; no controller-specific annotations. TLS only with `--tls-secret`. |
| NetworkPolicies | ingress rules only | | A default deny, then: Postgres from `db-client` pods, Mosquitto from `mqtt-client` pods, the gateway port from anywhere, engine, guardian and safestop metrics from `db-client` pods, the grid-link ports from `gridLink.service.allowedCidrs` (only when that Service is enabled), sim-control from the gateway only, sim-market from `db-client` pods and sim-control. Egress stays open (og-feeds calls ERCOT, EIA and NWS). |

The UI and API live in `og-api` (`/og/`). "health" is the evaluator inside og-settle, plus `/og/api/health`. Neither
is a separate service, which matches `deploy/systemd`.

Every pod runs as a non-root user (10001 for OpenGrid, 999 for Postgres, 1883 for Mosquitto) with a read-only
root filesystem, no capabilities and the RuntimeDefault seccomp profile. The orchestrator's configuration is the
release's `orchestrator.toml` plus the `<release>-config` ConfigMap's `overrides.toml`, merged at start by
`k8s_support.py render-config`: the Postgres and Mosquitto Services replace the loopback hosts, `[metrics]
bind_host` becomes `0.0.0.0`, the health evaluator reads the engine's metrics Service, `[feeds.ercot_as_poll] enabled` follows
`asPoll.enabled` (default false), and `[api] csrf_allowed_hosts` is `--host`. `[market_sim]` is gone from the
release config and from the overrides in r3.4.3. Add more with the
`config.extraOverrides` value (TOML tables).

## 1. Prerequisites

`install.sh --phase prereqs` checks every item below and prints a table with PASS, WARN and FAIL. On any FAIL it stops
without changing anything (under `--dry-run` it reports the FAILs and continues).

| Check | Requirement |
|---|---|
| kubectl | any recent client |
| helm | 3.14 or newer (v3 or v4; tested with 3.22.0 and 4.3.0) |
| container engine | docker or podman (`--engine`, default: whichever is installed). Needed to build the images and, while `og-signing-keys` doesn't exist, to generate the signing keys (the keygen CLIs run inside the image). |
| openssl, awk, sed, sort, tar, mktemp | secret generation and TOML parsing (FAIL if missing) |
| curl | the registry and ingress checks (WARN if missing: they are skipped) |
| release tree | migrations, `orchestrator.toml`, `deploy/apache/opengrid.conf`, `dev/seed`, `bootstrap_from_scratch.sh` and the chart are present |
| cluster reachable | `kubectl get --raw /readyz` succeeds |
| Kubernetes version | 1.30 or newer (`kubeVersion` of the chart) |
| RBAC | create rights for Deployments, StatefulSets, Jobs, CronJobs, Secrets, ConfigMaps, Services, PVCs, Ingresses and NetworkPolicies, plus the namespace if it doesn't exist |
| StorageClass | a default one, or `--storage-class` (must exist) |
| ingress controller | a default IngressClass, or `--ingress-class` (must exist) |
| capacity | at least one Ready, schedulable node; free CPU and memory (allocatable minus the requests of running pods in other namespaces) covers the chart's rendered requests; the largest node has at least 20 GiB of ephemeral storage |
| container registry | `https://<registry>/v2/` answers 200 or 401 (FAIL only when the images phase pushes; `--registry` is required unless `--no-push`) |
| owner keys | `--api-keys-file` holds `ERCOT_PUBLIC_API_KEY_PRIMARY` and `EIA_API_KEY` (or pass `--no-api-keys`, a WARN); `--anthropic-key-file`, when given, must hold `ANTHROPIC_API_KEY` (without it, a WARN: the copilot runs its no-model tier) |
| external database | with `--external-db`, `--db-password-file` is readable |
| inputs | `--host` (public FQDN) |

NetworkPolicies take effect only on a CNI that enforces them, such as Calico or Cilium. The checks can't detect
that, so confirm it yourself.

## 2. Sizing

Requests (the scheduler reserves these) and limits (from the systemd `MemoryMax` values):

| Component | CPU request | Memory request / limit |
|---|---|---|
| og-engine | 500m | 1 GiB / 2 GiB |
| og-api (+ gateway) | 250m | 544 MiB / 1.1 GiB |
| og-guardian, og-feeds, og-settle, og-safestop | 450m total | 896 MiB / 1.75 GiB total |
| Postgres | 500m | 1 GiB / 2 GiB |
| Mosquitto | 50m | 32 MiB / 128 MiB |
| og-sim-fleet | 500m | 1 GiB / 3 GiB |
| other simulators (market, scada, control) | 150m | 352 MiB / 1 GiB |
| **Total (steady state)** | **about 2.4 cores** | **about 4.8 GiB / 11 GiB** |

The migrate and seed Jobs request 200m and 512 MiB each (limit 1 GiB), the lifecycle job 100m and 256 MiB.

| | Minimum | Recommended |
|---|---|---|
| Nodes | 1 | 2 or more (og-settle and the lifecycle job share a node through the RWO volume) |
| CPU (allocatable) | 4 vCPU | 8 vCPU |
| Memory (allocatable) | 12 GiB | 16 GiB |
| Persistent storage | 50 GiB (Postgres 20 GiB, og-settle 30 GiB) | SSD-backed, 100 GiB |
| Ephemeral storage per node | 20 GiB | 40 GiB |

No CPU limits are set, so the engine's dispatch cycle is never throttled. The fleet simulator's 3 GiB limit fits
2,500 hubs (LZ_AEN). With `--noie-blocks` (3,500 hubs), raise `sims.fleet.resources`.

## 3. Step by step

```bash
# 0. From a clean checkout of the release (the git short sha becomes the image tag).
cd opengrid-orchestrator

# 1. The owner-supplied keys: same format as /etc/opengrid/api_keys.env and ai_agent.env.
#    Keep them outside the repository; the installer only checks that they are present.
ls -l ~/og-keys/api_keys.env ~/og-keys/ai_agent.env

# 2. Preview: every check runs, nothing changes, and the chart is rendered to ./render.
deploy/k8s/install.sh --dry-run --render-dir ./render \
  --registry registry.example.com/opengrid --host og.example.com \
  --api-keys-file ~/og-keys/api_keys.env --anthropic-key-file ~/og-keys/ai_agent.env

# 3. Install: prereqs, images, namespace, secrets, deploy, verify.
deploy/k8s/install.sh \
  --registry registry.example.com/opengrid --host og.example.com --tls-secret og-tls \
  --api-keys-file ~/og-keys/api_keys.env --anthropic-key-file ~/og-keys/ai_agent.env

# 4. Get a UI password (the installer never prints one):
kubectl -n opengrid get secret og-ui-users -o jsonpath='{.data.operator}' | base64 -d; echo
```

The deploy phase runs `helm upgrade --install --wait --wait-for-jobs` once. Postgres starts first; the migrate Job
waits for it. The seed Job and every og-* pod (through a `wait-schema` init container) wait for the migrations.
Helm returns when all of them are ready, up to `--timeout` (default 30m). The first install takes about 15 to
25 minutes, most of it the image build and the fleet seed.

### Phases and options

Default phases: `prereqs,images,namespace,secrets,deploy,verify`. `--phase` selects a comma-separated subset.

| Phase | What it does |
|---|---|
| `prereqs` | the checks of section 1 (read-only) |
| `images` | builds `opengrid-orchestrator` and `opengrid-sims` from `deploy/k8s/images` (build arg `OG_REVISION` = the tag) and pushes them |
| `namespace` | creates the namespace (label `app.kubernetes.io/part-of=opengrid`) and, with `--pull-secret-file`, the pull Secret `og-pull` |
| `secrets` | the Secrets below; present keys are never touched |
| `deploy` | `helm upgrade --install` of `deploy/k8s/chart`; under `--dry-run`, `helm lint` plus `helm template` into `--render-dir` |
| `verify` | section 4 |
| `migrate`, `seed` | extra phases: delete that Job, then run `deploy` again so it re-runs |
| `uninstall` | extra phase: `helm uninstall`; keeps the Secrets and PVCs unless `--purge --yes` deletes the namespace |

| Option | Default | Meaning |
|---|---|---|
| `--dry-run` | off | read-only: checks run, every change is printed instead |
| `--namespace NS`, `--release NAME` | `opengrid`, `opengrid` | |
| `--registry REG` | none | image registry prefix; required to push |
| `--tag TAG` | git short sha (12 characters) | a checkout with uncommitted changes is refused; `latest` is refused by the chart |
| `--engine docker\|podman` | whichever is installed | |
| `--skip-build` | off | the images for `--tag` must already be in the registry |
| `--no-push` | off | build only (kind, k3d) |
| `--host FQDN` | none (required) | Ingress rule and CSRF allowed host |
| `--ingress-class NAME`, `--storage-class NAME` | the cluster default | |
| `--tls-secret NAME` | none (plain HTTP at the Ingress) | an existing `kubernetes.io/tls` Secret |
| `--external-db HOST[:PORT]`, `--db-password-file F` | bundled Postgres; port 5432 | |
| `--api-keys-file F` / `--no-api-keys` | one of them required | |
| `--anthropic-key-file F` | none | optional |
| `--pull-secret-file F` | none | a `.dockerconfigjson`; creates `og-pull` |
| `--zones LIST` | `LZ_AEN` | enabled zone blocks |
| `--noie-blocks` (alias `--d32`) | off | adds `LZ_LCRA,LZ_RAYBN` (D-37 NOIE blocks) |
| `--grid-link-config F`, `--grid-link-certs DIR` | none (grid link off) | the `[grid_link]` override and its TLS files, put into Secret `og-gridlink` |
| `--demo-customers` | off | verify also runs `dev/scripts/seed_demo_customers.py` |
| `--values FILE` | none | extra helm values file (repeatable) |
| `--timeout DUR` | `30m` | helm and rollout wait |
| `--render-dir DIR` | a temp dir (removed on exit) | where `--dry-run` writes the rendered manifests |
| `--purge --yes` | off | with `uninstall`: delete the namespace, its Secrets and all data |

The installer sets these chart values: `image.registry`, `image.tag`, `ingress.host`, `zones`, `customerUsers`
(the keys of `[api.roles.customer]`), `gateway.apacheConf` (the content of `deploy/apache/opengrid.conf`), and,
when given, `ingress.className`, `ingress.tlsSecret`, the two `storageClass` values, `postgres.external.*` and
`image.pullSecrets`. Everything else has a default in `chart/values.yaml`.

Common variations:

| Goal | Command |
|---|---|
| Re-run a phase | `install.sh --phase deploy,verify ...` (every phase converges) |
| Use images already pushed | `--skip-build --tag <sha>` |
| Local cluster (kind, k3d) | `--no-push`, then load the images into the cluster; use `--registry ""` if you need to |
| Existing Postgres 17 | create the role and database first: `create_schema.sh --no-migrate --db-host db.example.com --etc DIR`, with `OG_PG_ADMIN_PASSWORD` exported (read it with `read -rs`, never on the command line), where `DIR/secrets.env` holds the role's `OG_DB_PASSWORD`; then `--external-db db.example.com:5432 --db-password-file ~/og-keys/dbpass`. The migrate Job then runs only the migrations. |
| Also enable LZ_LCRA and LZ_RAYBN (D-37 NOIE blocks: regulated, UNAVAILABLE, no contract; `noie_switch_seed.sql` always runs) | `--noie-blocks` (`--d32` is an alias, kept from D-32, which D-37 superseded in r3.4.2; runs the seed Job again) |
| D-34 grid link (off by default) | `--grid-link-config grid_link.toml --grid-link-certs DIR`: the `[grid_link]` override (as written by `deploy/scripts/grid_link_enable_loopback.sh`) and its TLS files go into Secret `og-gridlink`, which og-engine reads through `OG_GRID_LINK_CONFIG`; the MQTT user `og_gridlink` and its ACL block are added. Loopback (listen on 127.0.0.1 in the og-engine pod) exposes nothing. With `--values` `gridLink.service.enabled: true` plus `allowedCidrs`, the ports become a ClusterIP Service behind a NetworkPolicy. |
| D-35 ERCOT AS instruction poller (off by default) | `--values` with `asPoll.enabled: true` |
| Demo customers after install | `--phase verify --demo-customers` |
| Re-run migrations or seeds | `--phase migrate` or `--phase seed` |
| Turn on the data lifecycle | after the I/O check in [BOOTSTRAP.md section 9](../BOOTSTRAP.md#9-data-lifecycle): `kubectl -n opengrid patch cronjob opengrid-lifecycle -p '{"spec":{"suspend":false}}'`, or `--values` with `lifecycle.suspend: false` |
| Remove the software, keep data and secrets | `--phase uninstall` |
| Remove everything, data included | `--phase uninstall --purge --yes` (irreversible) |

### Secrets

| Secret | Keys | Source |
|---|---|---|
| `og-db` | `OG_DB_PASSWORD`, `POSTGRES_PASSWORD` (with `--external-db`: `OG_DB_PASSWORD` only, from `--db-password-file`) | generated (`openssl rand -hex 24`) |
| `og-mqtt` | `OG_MQTT_ENGINE_PASSWORD`, `OG_MQTT_GUARDIAN_PASSWORD`, `OG_MQTT_API_PASSWORD`, `OG_MQTT_SAFESTOP_PASSWORD` | generated (hex) |
| `og-mqtt-sim` | `OG_MQTT_SIM_PASSWORD`, `OG_MQTT_SIMCTL_PASSWORD`, `OG_MQTT_CUSTOMER_PASSWORD`, `OG_MQTT_UTILITY_PASSWORD` (og_sim_utility, r3.4.3) | generated (hex) |
| `og-gridlink` | the `[grid_link]` override and its TLS files; og_gridlink's MQTT password is `OG_MQTT_GRIDLINK_PASSWORD` | `--grid-link-config` and `--grid-link-certs` (only with the grid link) |
| `og-api-proxy` | `OG_API_PROXY_SECRET` | generated (hex) |
| `og-ui-users` | one key per Apache account: operator, viewer, tester, the `[guardian] stop_release_authorised_operators` (og-op-a, og-op-b), the `og-cust-*` accounts of `[api.roles.customer]` and, from chart 0.2.0, the `[api.roles.utility]` accounts (og-util-aen) | generated (`openssl rand -base64 24`) |
| `og-signing-keys` | `guardian_ed25519.key/.pub`, `safestop_ed25519.key/.pub`, `trace_anchor_ed25519.key/.pub` | the keygen CLIs, inside the image (`--network none`) |
| `og-owner-keys` | every `KEY=VALUE` of `--api-keys-file` (ERCOT, EIA) | your file, replaced on every run that passes it; created empty with `--no-api-keys` |
| `og-ai-agent` | every `KEY=VALUE` of `--anthropic-key-file` (Anthropic) | your file; optional |
| `og-pull` | `.dockerconfigjson` | `--pull-secret-file` (namespace phase) |

Generated values go into a private temp directory, which is removed on exit. They reach the API server through
`kubectl create` or `kubectl patch --patch-file`, never `kubectl apply`, which would copy them into an annotation.
The installer never prints them and never rotates a key that is already present; a partial `og-signing-keys` stops
the run (restore it from backup). **Back up `og-signing-keys`**: the simulated hubs, and later real ones, trust its
public keys.

## 4. Verify

`install.sh --phase verify` checks:

- that every Deployment and the StatefulSet of the release roll out
- that the migrate and seed Jobs succeeded
- that `/og/api/health` answers 200 (probed from inside og-api at localhost:8080, because the endpoint accepts
  loopback callers only)
- that the gateway answers `/og/` without credentials with 401 (localhost:8081 inside the pod)
- that og-engine's `/metrics` answers 200 (port 9101; the guardian (9103) and safestop (9106, r3.4.2) metrics ports
  have liveness probes)
- after 45 s, that the `og.heartbeat` rows of feeds, engine, guardian, safestop, settle and api are fresh
- `bootstrap_check.py --live` inside og-api: migrations, hubs per zone, banks, the substation asset, utilities, the
  toll contract, the customer contracts, transformers, the charge window, firmware, fresh telemetry and zero
  invariant violations; in r3.4.3 also the mobile trucks (`--expect-trucks`, because the release ships
  `dev/seed/mobile_trucks_seed.sql`)
- that `http(s)://<host>/og/` answers 401 through the Ingress (https with `--tls-secret`; a WARN if DNS doesn't
  point at it yet)
- with `--demo-customers`, that `seed_demo_customers.py` succeeds

The expected counts are the same as on the base server; see [BOOTSTRAP.md section 0](../BOOTSTRAP.md#0-automated-deployscriptsbootstrap_from_scratchsh),
"Check on the test cluster".

## 5. Troubleshooting

| Symptom | Look at | Usual cause |
|---|---|---|
| prereqs: `default StorageClass` FAIL | `kubectl get storageclass` | None is marked default: pass `--storage-class`. |
| prereqs: `ingress controller` FAIL | `kubectl get ingressclass` | No controller is installed, or none is the default: pass `--ingress-class`. |
| prereqs: `cluster CPU` or `cluster memory` FAIL | `kubectl describe nodes` | Other workloads have reserved the room. Add a node or lower the requests with `--values`. |
| helm times out; pods stuck in `Init:0/1` | `kubectl -n opengrid logs deploy/opengrid-engine -c wait-schema` | The migrate Job failed: `kubectl -n opengrid logs job/opengrid-migrate-<hash>` (the `create_schema.sh` output). |
| seed Job fails | `kubectl -n opengrid logs job/opengrid-seed-<hash>` | Its output is `bootstrap_from_scratch.sh --phase e` plus `bootstrap_check.py`. Fix the cause, then run `--phase seed`. |
| Postgres Pending | `kubectl -n opengrid describe pvc` | The StorageClass cannot provision. |
| og-api not ready | `kubectl -n opengrid logs deploy/opengrid-api -c api` | The DB password or proxy secret is missing, or a migration is missing. |
| 401 with correct credentials | `kubectl -n opengrid logs deploy/opengrid-api -c htpasswd` | The account is not in `og-ui-users`: re-run `--phase secrets` (it adds missing accounts, og-util-aen included from chart 0.2.0), then restart og-api. |
| 403 from og-api after login | gateway logs | The proxy secret differs between the containers. Both read `og-api-proxy`; restart the pod. |
| MQTT "not authorised" | `kubectl -n opengrid logs deploy/opengrid-mosquitto -c passwd` | A password Secret key is missing: re-run `--phase secrets`, then restart Mosquitto. |
| hubs not fresh in verify | `kubectl -n opengrid logs deploy/opengrid-sim-fleet` | The fleet simulator is still starting (allow 1 to 2 min), or the guardian public key is missing. |
| og-feeds degraded | og-feeds logs, `/og/` Health page | Installed with `--no-api-keys`: re-run `--phase secrets --api-keys-file ...` and restart og-feeds. |
| a NetworkPolicy blocks something | `kubectl -n opengrid get netpol` | The CNI enforces the policies and a caller is missing a label. Set `networkPolicy.enabled=false` to confirm. |

To run commands inside the orchestrator image:
`kubectl -n opengrid exec deploy/opengrid-api -c api -- og-entrypoint check`. The entrypoint's usage text lists
every subcommand (`og-entrypoint` with no arguments): `engine guardian safestop feeds settle api lifecycle migrate
seed check keygen-tar mqtt-acl wait-db wait-schema probe-http probe-heartbeat demo-customers` in the orchestrator
image, `sim-market sim-fleet sim-scada sim-control sim-customer sim-configs` in the sims image.

## r3.4.3 features on Kubernetes

Chart 0.2.0 (`appVersion: "r3.4.3"`) covers the r3.4.2 and r3.4.3 additions. Every row below is lint-verified only
(`helm lint`, `kubeconform`); none has run in a container smoke test or on a cluster (see [Known gaps](#known-gaps)).

| Feature | On Kubernetes (r3.4.3, chart 0.2.0) |
|---|---|
| Migrations through 0050 (0045 to 0047, 0050), and the later r3.4.3 migrations 0051, 0053 and 0054 | Come with the image: the migrate Job runs `create_schema.sh`, which applies every file in `orchestrator/migrations/`. No chart change needed. |
| D-38 delivery verification (og-settle job, migrations 0050, 0051 and 0054, live alerts, AT_RISK) | Runs: it is part of og-settle and on by default (`[delivery].enabled = true`). No chart change needed. |
| Obligation lifecycle step in the background, bounded propose timeout, AS capacity settled at the cleared MCPC | Run inside og-engine and og-settle; no chart change needed. |
| og-safestop `/metrics` on port 9106 | Exposed: a metrics Service, a container port, a `/metrics` liveness probe and a NetworkPolicy rule (from `db-client` pods). Readiness still uses the heartbeat. |
| AS-POLL (D-35, `[feeds.ercot_as_poll]`) | Off by default. `asPoll.enabled: true` (with `--values`) sets `[feeds.ercot_as_poll].enabled` in the overrides. |
| Grid link (D-34, DNP3) | Off by default. `--grid-link-config` and `--grid-link-certs` create Secret `og-gridlink`, mounted in og-engine through `OG_GRID_LINK_CONFIG`, and add the `og_gridlink` MQTT user (`OG_MQTT_GRIDLINK_PASSWORD`) and its ACL block (`deploy/mosquitto` is now in the image). Loopback is the default and exposes nothing; `gridLink.service` adds a ClusterIP Service plus a NetworkPolicy limited to `allowedCidrs`. See [grid-link.md](../../docs/orchestrator/07-delivery/integrations/grid-link.md). |
| Utility customer API (D-33, account og-util-aen) | The installer now adds the `[api.roles.utility]` accounts to `og-ui-users`, and `deploy/apache/opengrid.conf` already admits og-util-aen, so the account can log in through the gateway. |
| AE utility simulator (`ogsim.utility_aen`, og-sim-utility) | Runs as a Deployment, as in production's `ogsim.target`. MQTT user `og_sim_utility` (`OG_MQTT_UTILITY_PASSWORD` in `og-mqtt-sim`; ACL grant: read `og/v1/scenario/cmd`, rendered by `render_mosquitto_acl.sh`). It calls the utility API as og-util-aen through the gateway. |
| D-37 NOIE blocks | `--noie-blocks` (alias `--d32`) adds LZ_LCRA and LZ_RAYBN; the seed Job always runs `noie_switch_seed.sql`. |

## Updating the pins

- **Python dependencies:** `images/constraints-*.txt` is production's `pip freeze` (base, 2026-09-26, r3.4) with
  the test tools removed. Refresh it from `/opt/opengrid/venv` and `/opt/ogsim/venv` when production's venvs change.
- **Base and third-party images:** pinned by tag plus the multi-arch index digest, in the Dockerfiles'
  `PYTHON_IMAGE` (python 3.13.15-slim-trixie) and in `chart/values.yaml` under `thirdParty` (postgres
  17.11-trixie, eclipse-mosquitto 2.0.22, httpd 2.4.68-trixie).
- **Chart:** bump `chart/Chart.yaml` `version` on every template change, and `appVersion` per release (0.2.0 and
  r3.4.3 in r3.4.3). Third-party digests are unchanged from 0.1.0.
- **OpenGrid images:** tagged with the git short sha. `latest` is refused.

## Known gaps

- **Never run on a real cluster.** Everything above was validated offline. The first real install should be on a
  disposable cluster.
- **No r3.4.3 smoke test.** Chart 0.2.0 went into r3.4.3 as lint-verified only; the last smoke test is r3.4.1's
  (chart 0.1.0, migrations 0001 to 0044). The container smoke test of the r3.4.3 additions hasn't run: og-sim-utility
  end to end, the grid-link Secret mount and `og_gridlink`, the safestop metrics probe (9106), and migrations 0045
  to 0050 with the NOIE seed in the seed Job. The migrations merged into r3.4.3 after the chart was cut (0051, 0053,
  0054) are not smoke-tested either. The owner removed podman from base after r3.4.1 and no image builds or smoke
  runs happen on the production host. The test will run on a non-production machine (the workstation's Docker,
  after its performance campaign) or on a real cluster.
- The smoke test had no network, so og-feeds could not reach ERCOT, EIA or NWS, and its ticks failed as expected.
  Live feeds on Kubernetes are untested.
- Backups: production's `deploy/scripts/backup.sh` and the cron jobs aren't ported. Use your platform's volume
  snapshots or a Postgres backup operator, and back up `og-signing-keys`.
- The Postgres standby and failover setup (`deploy/ha`) is not ported. The bundled Postgres is a single instance.
- The ERCOT price backfill (bootstrap phase k) isn't a phase here. Run it with
  `kubectl exec deploy/opengrid-feeds -- python /opt/opengrid/current/orchestrator/tools/ercot_backfill.py --days 14`.
- The trace journal fallback of every og-* process except og-settle uses an emptyDir. Entries written while
  Postgres was down are lost if the pod is deleted before they are replayed.
- PQ waveform blobs (`[pq_ingest] blob_store_dir`, `/var/lib/opengrid/pq_waveform`) are written by og-engine, whose
  `/var/lib/opengrid` is an emptyDir, so they are lost when the pod is replaced. The comment in `values.yaml` that
  places them on og-settle's PVC is out of date.
- The scada simulator's MariaDB history file (`/var/lib/opengrid/import/...`) isn't shipped, so it uses its
  default base load.
- `hadolint` wasn't run (not installed; its GPL licence is outside this lane's permissive-licence rule).
  `shellcheck`, `helm lint` and `kubeconform` were run.
