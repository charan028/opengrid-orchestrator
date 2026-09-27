# OpenGrid on Kubernetes: installing from scratch

`deploy/k8s/install.sh` installs the complete solution on a new Kubernetes cluster: the orchestrator (every og-*
service, the HTMX UI and the API), Postgres 17, the Mosquitto broker with the production ACL, the migrations and
seeds, the data-lifecycle CronJob and the ogsim integration simulators. It first lists and checks every
prerequisite. It stops on any failure, and every phase is safe to re-run.

> **Status (r3.4.3, chart 0.2.0, 2026-09-27): validated offline only, never deployed to a real cluster.** The chart passes
> `helm lint --strict` (helm 3.22.0 and 4.3.0) and `kubeconform -strict` (Kubernetes 1.30 and 1.33 schemas). The
> scripts pass `shellcheck`, the two images build, and `install.sh --dry-run` runs end to end. A smoke test ran every
> workload from the built images in one isolated network namespace with podman, using the chart's rendered
> ConfigMaps. In that test, `create_schema.sh` applied every migration through 0050; the seed Job and `bootstrap_check.py --live`
> passed; every heartbeat was fresh; `/og/api/health` returned 200; the gateway returned 401 without credentials and
> 200 as operator. No Kubernetes cluster was available, so the first real install is also the first integration test
> of the Kubernetes objects themselves (scheduling, PVCs, Services, Ingress, NetworkPolicies). Treat it as a pilot,
> and read [Known gaps](#known-gaps).

This page covers Kubernetes only. To run on a base server (the production host), see [../BOOTSTRAP.md](../BOOTSTRAP.md).
The installer reuses the same building blocks as that script:

| Step | Reused from |
|---|---|
| role, database, schema `og`, migrations 0001 to latest | `deploy/scripts/create_schema.sh` (bootstrap phases c and d) |
| seeds, in order, then the count check | `deploy/scripts/bootstrap_from_scratch.sh --phase e` |
| sim configs with the approved zone blocks | `deploy/scripts/gen_sim_overrides.py` (bootstrap phase f) |
| MQTT ACL | `deploy/scripts/render_mosquitto_acl.sh` (bootstrap phase g) |
| Ed25519 keys | the orchestrator's keygen CLIs, run by bootstrap phase h inside the image |
| basic auth and the proxy secret | `deploy/apache/opengrid.conf`, unchanged (bootstrap phase i) |
| service commands and memory limits | `deploy/systemd/*.service` |
| verification | `deploy/scripts/bootstrap_check.py --live` |

## What gets deployed

| Workload | Kind | Image | Notes |
|---|---|---|---|
| og-engine, og-guardian, og-safestop, og-feeds, og-settle | Deployment (1 replica, `Recreate`) | `opengrid-orchestrator` | Singletons: two engines or two guardians must never run at the same time. og-settle also runs the health evaluator and the PQ ingest. |
| og-api + `gateway` sidecar | Deployment | `opengrid-orchestrator` + `httpd` | og-api keeps its loopback bind. Apache in the same pod enforces basic auth and adds the proxy secret, as production's Apache does. |
| Postgres 17.11 | StatefulSet + PVC | `postgres` | Optional: use an existing server with `--external-db`. |
| Mosquitto 2.0.22 | Deployment | `eclipse-mosquitto` | No anonymous access. One generated password per account, plus the production ACL. |
| migrate, seed | Job | `opengrid-orchestrator` | migrate runs `create_schema.sh` with the bundled server's superuser password. Both are named by a hash of their inputs, so re-runs with the same inputs do nothing. |
| og-lifecycle | CronJob, **suspended** | `opengrid-orchestrator` | Matches production's disabled `og-lifecycle.timer`. |
| og-sim-market, -fleet, -scada, -control, -utility (-customer off) | Deployment | `opengrid-sims` | The same set that `ogsim.target` starts. og-sim-utility (r3.4.3) is the Austin Energy EMS simulator: MQTT user `og_sim_utility`, and it calls the utility API as `og-util-aen` through the gateway. |
| Ingress | `/og/`, `/ogsim/` go to the gateway | | Works with any controller; no controller-specific annotations. |
| NetworkPolicies | ingress rules only | | Restrict the ports that are loopback-only in production. |

The UI and API live in `og-api` (`/og/`). "health" is the evaluator inside og-settle, plus `/og/api/health`. Neither
is a separate service, which matches `deploy/systemd`.

## 1. Prerequisites

`install.sh --phase prereqs` checks every item below and prints a table with PASS, WARN and FAIL. On any FAIL it stops
without changing anything.

| Check | Requirement |
|---|---|
| kubectl | any recent client |
| helm | 3.14 or newer (v3 or v4; tested with 3.22.0 and 4.3.0) |
| container engine | docker or podman. Needed to build the images and to generate the signing keys (the keygen CLIs run inside the image). |
| openssl, awk, sed, sort, tar, curl | secret generation, TOML parsing, and the registry and ingress checks |
| cluster reachable | `kubectl get --raw /readyz` succeeds |
| Kubernetes version | 1.30 or newer |
| RBAC | create rights for Deployments, StatefulSets, Jobs, CronJobs, Secrets, ConfigMaps, Services, PVCs, Ingresses and NetworkPolicies, plus the namespace |
| StorageClass | a default one, or `--storage-class` |
| ingress controller | a default IngressClass, or `--ingress-class` |
| capacity | free CPU and memory (allocatable minus the running pods' requests) covers the chart's requests; the largest node has at least 20 GiB of ephemeral storage |
| container registry | `https://<registry>/v2/` answers 200 or 401 |
| owner keys | `--api-keys-file` holds `ERCOT_PUBLIC_API_KEY_PRIMARY` and `EIA_API_KEY` (or pass `--no-api-keys`); `--anthropic-key-file` is optional |
| inputs | `--host` (public FQDN) and a complete release tree |

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
| other simulators | 150m | 352 MiB / 1 GiB |
| **Total (steady state)** | **about 2.4 cores** | **about 4.9 GiB / 11 GiB** |

| | Minimum | Recommended |
|---|---|---|
| Nodes | 1 | 2 or more (Postgres, og-settle and the lifecycle job share a node through the RWO volume) |
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

Common variations:

| Goal | Command |
|---|---|
| Re-run a phase | `install.sh --phase deploy,verify ...` (every phase converges) |
| Use images already pushed | `--skip-build --tag <sha>` |
| Local cluster (kind, k3d) | `--no-push`, then load the images into the cluster; use `--registry ""` if you need to |
| Existing Postgres 17 | create the role and database first: `create_schema.sh --no-migrate --db-host db.example.com --etc DIR`, with `OG_PG_ADMIN_PASSWORD` exported (read it with `read -rs`, never on the command line), where `DIR/secrets.env` holds the role's `OG_DB_PASSWORD`; then `--external-db db.example.com:5432 --db-password-file ~/og-keys/dbpass`. The migrate Job then runs only the migrations. |
| Also enable LZ_LCRA and LZ_RAYBN (D-37 NOIE blocks: regulated, UNAVAILABLE, no contract; `noie_switch_seed.sql` always runs) | `--noie-blocks` (`--d32` is an alias; runs the seed Job again) |
| D-34 grid link (off by default) | `--grid-link-config grid_link.toml --grid-link-certs DIR`: the `[grid_link]` override (as written by `deploy/scripts/grid_link_enable_loopback.sh`) and its TLS files go into Secret `og-gridlink`, which og-engine reads through `OG_GRID_LINK_CONFIG`; the MQTT user `og_gridlink` and its ACL block are added. Loopback (listen on 127.0.0.1 in the og-engine pod) exposes nothing. With `--values` `gridLink.service.enabled: true` plus `allowedCidrs`, the ports become a ClusterIP Service behind a NetworkPolicy. |
| D-35 ERCOT AS instruction poller (off by default) | `--values` with `asPoll.enabled: true` |
| Demo customers after install | `--phase verify --demo-customers` |
| Re-run migrations or seeds | `--phase migrate` or `--phase seed` |
| Turn on the data lifecycle | after the I/O check in BOOTSTRAP.md section 9: `kubectl -n opengrid patch cronjob opengrid-lifecycle -p '{"spec":{"suspend":false}}'`, or `--values` with `lifecycle.suspend: false` |
| Remove the software, keep data and secrets | `--phase uninstall` |
| Remove everything, data included | `--phase uninstall --purge --yes` (irreversible) |

### Secrets

| Secret | Contents | Source |
|---|---|---|
| `og-db` | `OG_DB_PASSWORD`, `POSTGRES_PASSWORD` | generated (`openssl rand -hex 24`) |
| `og-mqtt`, `og-mqtt-sim` | one password per MQTT account | generated |
| `og-api-proxy` | `OG_API_PROXY_SECRET` | generated |
| `og-ui-users` | one password per Apache account: operator, viewer, tester, the two-person-release operators, and the `og-cust-*` accounts from `orchestrator.toml` | generated |
| `og-signing-keys` | guardian, safestop and trace-anchor Ed25519 key pairs | the keygen CLIs, inside the image |
| `og-owner-keys`, `og-ai-agent` | ERCOT/EIA and Anthropic keys | your files, loaded as-is |

Generated values go into a private temp directory, which is removed on exit. They reach the API server through
`kubectl create` or `kubectl patch --patch-file`, never `kubectl apply`, which would copy them into an annotation.
The installer never prints them and never rotates a key that is already present. **Back up `og-signing-keys`**: the
simulated hubs, and later real ones, trust its public keys.

## 4. Verify

`install.sh --phase verify` checks:

- that every Deployment and the StatefulSet are ready
- that the migrate and seed Jobs succeeded
- that `/og/api/health` answers 200 (probed from inside og-api, because the endpoint accepts loopback callers only)
- that the gateway answers `/og/` without credentials with 401
- that og-engine's `/metrics` answers 200 (the guardian (9103) and safestop (9106, r3.4.2) metrics ports have liveness probes)
- that the `og.heartbeat` rows of feeds, engine, guardian, safestop, settle and api are fresh
- `bootstrap_check.py --live`: migrations, hubs per zone, banks, the substation asset, utilities, the toll contract,
  the customer contracts, transformers, the charge window, firmware, fresh telemetry and zero invariant violations
- that `https://<host>/og/` answers 401 through the Ingress (a WARN if DNS doesn't point at it yet)

The expected counts are the same as on the base server; see BOOTSTRAP.md section 0, "Check on the test cluster".

## 5. Troubleshooting

| Symptom | Look at | Usual cause |
|---|---|---|
| prereqs: `default StorageClass` FAIL | `kubectl get storageclass` | None is marked default: pass `--storage-class`. |
| prereqs: `ingress controller` FAIL | `kubectl get ingressclass` | No controller is installed, or none is the default: pass `--ingress-class`. |
| prereqs: capacity FAIL | `kubectl describe nodes` | Other workloads have reserved the room. Add a node or lower the requests with `--values`. |
| helm times out; pods stuck in `Init:0/1` | `kubectl -n opengrid logs deploy/opengrid-engine -c wait-schema` | The migrate Job failed: `kubectl -n opengrid logs job/opengrid-migrate-<hash>` (the `create_schema.sh` output). |
| seed Job fails | `kubectl -n opengrid logs job/opengrid-seed-<hash>` | Its output is `bootstrap_from_scratch.sh --phase e` plus `bootstrap_check.py`. Fix the cause, then run `--phase seed`. |
| Postgres Pending | `kubectl -n opengrid describe pvc` | The StorageClass cannot provision. |
| og-api not ready | `kubectl -n opengrid logs deploy/opengrid-api -c api` | The DB password or proxy secret is missing, or a migration is missing. |
| 401 with correct credentials | `kubectl -n opengrid logs deploy/opengrid-api -c htpasswd` | The account is not in `og-ui-users`: re-run `--phase secrets` (it adds missing accounts), then restart og-api. |
| 403 from og-api after login | gateway logs | The proxy secret differs between the containers. Both read `og-api-proxy`; restart the pod. |
| MQTT "not authorised" | `kubectl -n opengrid logs deploy/opengrid-mosquitto -c passwd` | A password Secret key is missing: re-run `--phase secrets`, then restart Mosquitto. |
| hubs not fresh in verify | `kubectl -n opengrid logs deploy/opengrid-sim-fleet` | The fleet simulator is still starting (allow 1 to 2 min), or the guardian public key is missing. |
| og-feeds degraded | og-feeds logs, `/og/` Health page | Installed with `--no-api-keys`: re-run `--phase secrets --api-keys-file ...` and restart og-feeds. |
| a NetworkPolicy blocks something | `kubectl -n opengrid get netpol` | The CNI enforces the policies and a caller is missing a label. Set `networkPolicy.enabled=false` to confirm. |

To run commands inside the orchestrator image:
`kubectl -n opengrid exec deploy/opengrid-api -c api -- og-entrypoint check`. The entrypoint's usage text lists
every subcommand (`og-entrypoint` with no arguments).

## Updating the pins

- **Python dependencies:** `images/constraints-*.txt` is production's `pip freeze` (base, 2026-09-26) with the test
  tools removed. Refresh it from `/opt/opengrid/venv` and `/opt/ogsim/venv` when production's venvs change.
- **Base and third-party images:** pinned by tag plus the multi-arch index digest, in the Dockerfiles'
  `PYTHON_IMAGE` and in `chart/values.yaml` under `thirdParty`.
- **Chart:** bump `chart/Chart.yaml` `version` on every template change, and `appVersion` per release.
- **OpenGrid images:** tagged with the git short sha. `latest` is refused.

## Known gaps

- **Never run on a real cluster.** Everything above was validated offline. The first real install should be on a
  disposable cluster.
- The smoke test had no network, so og-feeds could not reach ERCOT, EIA or NWS, and its ticks failed as expected.
  Live feeds on Kubernetes are untested.
- Backups: production's `deploy/scripts/backup.sh` and the cron jobs aren't ported. Use your platform's volume
  snapshots or a Postgres backup operator, and back up `og-signing-keys`.
- The Postgres standby and failover setup (`deploy/ha`) is not ported. The bundled Postgres is a single instance.
- The ERCOT price backfill (bootstrap phase k) isn't a phase here. Run it with
  `kubectl exec deploy/opengrid-feeds -- python /opt/opengrid/current/orchestrator/tools/ercot_backfill.py --days 14`.
- The trace journal fallback of every og-* process except og-settle uses an emptyDir. Entries written while
  Postgres was down are lost if the pod is deleted before they are replayed.
- The scada simulator's MariaDB history file (`/var/lib/opengrid/import/...`) isn't shipped, so it uses its
  default base load.
- `hadolint` wasn't run (not installed; its GPL licence is outside this lane's permissive-licence rule).
  `shellcheck`, `helm lint` and `kubeconform` were run.
