# 07 — Automated Chaos Harness (design)

Status: **Design only. Nothing here is built.** This document turns the manual process-kill runbook of
`04-mvp-s-test-plan.md` §4.6 (TS-C-01…TS-C-06 and TS-C-03b) and the composite K8 proof TS-06-23 into a design for an
automated harness. It also covers the availability row of `02b-mvp-s-spec-platform.md`, which names a
`tests/chaos/kill_each_process.py`. It feeds the Q3 chaos lane.

The lead's answers to the design questions (2026-09-26) are recorded in §13 as **lead default, pending owner
confirmation**. Nothing on the server changes until the owner approves the checklist in §14.

Code references were first written against `main @ f3b3365`. The blocker status in §3, the release path and the
metrics sources are refreshed to `main @ 434d230` (2026-09-26); every blocker found in the code is now fixed there.
The owner's decisions of 2026-09-26 are applied:
- per-workspace broker accounts (B2);
- the base server as the permanent host (§13 Q3, Appendix A);
- the PostgreSQL checkpoint cadence (§9.4).

`o/` stands for `orchestrator/src/opengrid/`, and `ogsim/` for `integration-sims/src/ogsim/`.

## 1. Design decisions

| # | Decision |
|---|---|
| D1 | Kill by exact systemd unit with `systemctl kill --kill-whom=all --signal=SIGKILL <unit>`. Never use `pkill`, `killall` or `kill <pid>`. The chaos and production processes run the same command line (`python -m opengrid.engine.main`, and so on), so any pattern that matches one also matches the other. |
| D2 | Targets are dedicated chaos-workspace units, `ogt-<process>@chaos<N>.service`, never `og-*`. This is enforced at the OS (polkit), by the harness allowlist, by a per-unit identity preflight, and by a production sentinel that aborts the run if any `og-*` unit changes state (§5). |
| D3 | There are two kill modes. **R** is a crash followed by systemd's automatic restart, which tests `Restart=always`. **H** is a crash with the automatic restart cancelled, so the process stays down for the outage window that the §4.6 runbook requires (§6). |
| D4 | Hub-side truth comes from an independent MQTT observer, orchestrator-side truth from Postgres. No assertion depends only on a counter held in the memory of a process the harness is about to kill (§9.2). |
| D5 | The chaos stack gets its own per-workspace MQTT account (`ogw_<ws>`), DB role, signing keys, ports and MQTT client IDs. Three code and config blockers (§3) must be fixed before the first run on the shared server. |
| D6 | One fault at a time, under live load, in a seeded order. Each experiment ends as PASS, FAIL, INVALID (the harness could not produce the fault cleanly) or ABORTED (a safety gate fired). |

## 2. Scope

**In scope:** SIGKILL of each of these, one at a time, plus the TS-06-23 pair (`og-engine` and `og-guardian` together):
- the orchestrator processes: `og-feeds`, `og-engine`, `og-guardian`, `og-safestop`, `og-settle`, `og-api`;
- the simulator processes: `ogsim.fleet` and `ogsim.scada` (the §4.6 "og-sim" row), and optionally `ogsim.market` and
  `ogsim.control`.

**Out of scope for the shared server:**
- TS-N-07 and TS-N-08 (restart PostgreSQL, restart Mosquitto). Both services are shared with production on the
  server, so restarting either one is a production outage. These tests need a dedicated host or the local compose
  stack (`dev/docker-compose.yml`).
- Network partitions, disk-full, clock skew (K12) and forged-command injection (K3). These are later extensions
  (§12).

## 3. Blockers found in the code (fix before the first run)

**B1: hard-coded MQTT client IDs.** Each process connects with a fixed ID:

| Process | Client ID | Where |
|---|---|---|
| `og-engine` | `"og-engine"` | `o/engine/__init__.py:661` |
| `og-guardian` | `"og-guardian"`, `"og-guardian-tel"` | `o/guardian/main.py:170`, `o/guardian/mqtt_io.py:55` |
| `og-safestop` | `"og-safestop"` | `o/safestop/main.py:108` |
| `og-api` | `"og-api-scenario"` | `o/api/routers/scenario.py:55` |

`[mqtt].client_id_prefix` is configured (`orchestrator.toml:21`, `test.toml:21`) but nothing reads it;
`o/platform/mqtt.py:92-106` passes the caller's ID through unchanged. MQTT allows one session per client ID, so the
broker drops the existing connection when a second client connects with the same ID.

A chaos `og-engine` on the shared broker would therefore disconnect the production engine, and the two would keep
taking over each other's session on every reconnect. The same risk exists today for anyone who runs an orchestrator
process in a workspace.

Fix:
- build the ID as `<client_id_prefix>-<OG_WS if set>-<process>`, with the prefix unique per environment: `og` in
  production, `ogt` for chaos;
- refuse to start when `env` is not `prod` and the prefix is `og`.

This is owned by the architect (`platform/mqtt.py`) and each process owner (its call site).

**Status at `434d230`: fixed.** `compose_client_id` (`o/platform/mqtt.py:109`) builds the ID from the prefix,
`OG_WS` and the process, and raises `MqttIdentityError` (`:103`, `:119`) instead of connecting with a production
identity outside production.

**B2: shared broker identities.** Workspaces reuse the production MQTT users (`og_engine`, `og_guardian`, and so on).
BUILD.md §5 says the ACL allows `ogtest/<ws>` "for all og users", and the same users may also publish under `og/v1`. A
chaos process with a wrong topic root would publish into production.

Fix: per-workspace broker accounts, owner decision 2026-09-26, being implemented. Each workspace gets its own
`ogw_<ws>` account with readwrite on `ogtest/<ws>/#` only, and the shared `ogtest/#` pattern for the `og_*` users is
removed.
- A chaos instance uses its workspace's account (`ogw_chaos`, `ogw_chaos1` … `ogw_chaos9`) for all its processes.
- This design proposes one more account per chaos workspace, the read-only `ogw_<ws>_ro`, for the MQTT observer
  (§9.1), so a harness bug cannot publish into the stack it measures.
- Per-workspace accounts do not fix B1: client IDs are global on the broker, whichever account connects.

**Status at `434d230`: built (D-13).** `deploy/mosquitto/provision_ws_users.py` (and `.sh`) provision the per-workspace
`ogw_<ws>` accounts. The chaos names and the proposed `_ro` observer accounts are still to add (§14).

**B3: the simulator ignores `OG_MQTT_ROOT` and pins production keys.**
- The topic root is read as `raw.get("topic_root", os.environ.get("OG_MQTT_ROOT", "og/v1"))`
  (`ogsim/common/config.py:55`), so the YAML value wins over the environment variable.
- The checked-in `integration-sims/config/fleet.yaml:8` and `scada.yaml:8` both set `topic_root: og/v1`.
- The default config path is relative, `integration-sims/config/fleet.yaml` (`config.py:134`, and `:231` for scada),
  so it resolves against the process's working directory.

A sim started from a workspace root (`/opt/opengrid/work/<ws>`, where `tools/remote.ps1` runs commands) therefore
loads that YAML and publishes on the production root `og/v1`, whatever `OG_MQTT_ROOT` says.

The same YAML pins the production guardian public key (`fleet.yaml:37`). Key paths have no environment override at all
(`config.py:94-97, 160-163`), so such a sim would also accept production-signed batches.

The orchestrator gets this right: `o/platform/config.py:89-95` applies `OG_DB` and `OG_MQTT_ROOT` after loading the
TOML, so the environment always wins.

Fix (sims owner):
- the environment wins over the YAML, as on the orchestrator side;
- key paths get environment overrides;
- the default config path resolves against the package, not the working directory;
- `ogsim` refuses `og/v1` outside production.

Independently, chaos sim units point `OGSIM_FLEET_CONFIG` and `OGSIM_SCADA_CONFIG` at rendered chaos YAMLs (§4.2),
and the workspace's broker account (B2) cannot publish under `og/v1` even if a setting is wrong.

**Status at `434d230`: fixed.** Outside a process explicitly marked production (`OGSIM_ENV=prod`,
`ogsim/common/config.py:106`), ogsim no longer falls back to the production topic root or key paths. It raises
`WorkspaceConfigError` instead (`:110`).

**Related K8 finding (not a harness blocker): the hub sim releases a fleet stop without a signature.**
- `ogsim/fleet/__main__.py:124-131` handles `<root>/stop/...` messages. For any payload that parses as JSON but is
  empty or false (`{}`, `null`, `[]`, `0`, `false`), it calls `apply_stop_event("RELEASE", …)` without verifying
  anything.
- The scope is read from the topic (`parts[-2]`). `og-safestop` publishes on `stop/fleet/<id>`,
  `stop/zone/<zone>/<id>` and `stop/bank/<bank>/<id>` (`o/safestop/events.py:47-51`), so only the fleet scope lines up.
- Any client allowed to publish on `stop/fleet/#` can therefore release a fleet stop unsigned. That includes the
  stop-only authority's own MQTT user, and, until the per-workspace accounts (B2) land, every `og_*` user in the
  test roots.
- K8 and crypto.md §2.3 require a RELEASE signed by the guardian (`ogsim/fleet/stop.py:15-19, 51-66`).
- A genuinely zero-length payload, which is what `interfaces/mqtt/topics.md:27` calls a release, never reaches that
  branch, because `json.loads` rejects it (`__main__.py:106-109`).
- The spec line and the sim therefore disagree, and both contradict the signing rule.

Fix: releases only through a guardian-signed RELEASE event. A zero-length retained payload is broker housekeeping and
never changes stop state. The owners are the sims owner and the architect (`topics.md`). §9.3's K8 row checks for
this.

**Status at `434d230`: fixed.**
- `ogsim/fleet/__main__.py:110` rejects any stop payload that is not a non-empty object.
- The hub now tracks outstanding stops by `stop_id` (`ogsim/fleet/stop.py:99-152`), so an old signed RELEASE cannot
  lift a newer stop either.
- A related dormant edge case, the scope-wide `issued_at` backstop, is recorded in `13-known-limitations.md` DM-1.

**Handled by configuration, not code (§4):**
- **Ports.** `og-api` binds 8080 in both configs (`test.toml:100`), the guardian metrics server defaults to 9103
  (`o/guardian/main.py:166`), and the market sim to 8090 (`ogsim/market/config.py:86`). Port 18090 is already used by
  `tests/integration/feeds/test_market_sim.py`.
- **Test keys.** They live in `/tmp` (`test.toml` `key_path`), which every unit sees as private under
  `PrivateTmp=true`.
- **Raw waveform store.** `pq_ingest.blob_store_dir` defaults to the production store `/var/lib/opengrid/pq_waveform`.
  `test.toml` now sets a workspace-relative `var/pq_waveform` (`orchestrator/config/test.toml:116`), and the chaos
  config sets its own (§4.2).
- **No `sudo`.** The server has none (02b §9.1).
- **Metrics.** The guardian serves `/metrics`, and the engine now does too (`o/engine/metrics.py:56`,
  `[metrics].engine_port` 9101, loopback only). The chaos port block gives each its own port (§4.2).
- **API identity.** Every API call the harness makes must carry the chaos instance's own proxy secret
  (`X-OG-Proxy-Auth`, checked against `OG_API_PROXY_SECRET` in `o/api/auth.py:82`) and a chaos operator's
  `X-Remote-User`. The chaos secret is generated per instance, lives only in that instance's env file (§4.1) and is
  never shared with production. The harness never reads `/etc/opengrid` on the base server.

## 4. The chaos stack (what gets killed)

### 4.1 Units

The deploy owner adds template units next to the production ones:

| Unit | Process |
|---|---|
| `ogt-feeds@.service`, `ogt-engine@.service`, `ogt-guardian@.service`, `ogt-safestop@.service`, `ogt-settle@.service`, `ogt-api@.service` | same `ExecStart` module as the matching `deploy/systemd/og-*.service` |
| `ogt-sim-fleet@.service`, `ogt-sim-scada@.service`, `ogt-sim-market@.service`, `ogt-sim-control@.service` | same module as the matching `og-sim-*.service` |
| `ogt-stack@.target` | `Wants=` all ten |

The instance `%i` is the workspace: `chaos`, or `chaos1` to `chaos9` for parallel stacks. Differences from production,
all deliberate:

- **Code and environment:**
  - `WorkingDirectory` and `PYTHONPATH` point to `/opt/opengrid/work/%i/…`, the tree `tools/remote.ps1` already syncs.
  - `Environment=OG_WS=%i OG_DB=og_t_%i OG_MQTT_ROOT=ogtest/%i OG_CONFIG=/etc/opengrid/chaos/%i.toml` (§4.2).
  - The sim units also set `OGSIM_FLEET_CONFIG` and `OGSIM_SCADA_CONFIG` to the rendered chaos YAMLs
    (`/etc/opengrid/chaos/%i-fleet.yaml`, `/etc/opengrid/chaos/%i-scada.yaml`), which carry the chaos topic root and
    key paths (B3).
- **Sandboxing:** identical to production (`ProtectSystem=strict`, `NoNewPrivileges=true`, `PrivateTmp=true`), with
  narrower write access (below). `ReadWritePaths=` only confines writes in combination with `ProtectSystem=strict`, so
  that setting is mandatory.
- **Env file:** only `EnvironmentFile=/etc/opengrid/chaos/%i.env`, which holds the chaos DB and MQTT passwords. Never
  `secrets.env` or `api_keys.env`. The chaos feeds read the chaos market sim, not ERCOT, EIA or NWS, so no external
  API quota is used.
- **Keys:** `/var/lib/opengrid/chaos/%i/{guardian,safestop}_ed25519.{key,pub}`, generated per workspace. A batch
  signed in the chaos stack must never verify on a production hub, and the reverse.
- **Users:** services run as `User=ogchaos-svc`, a user that cannot read `/etc/opengrid/secrets.env`, with
  `ReadWritePaths=/var/lib/opengrid/chaos/%i /var/log/opengrid/chaos`. The harness itself runs as a separate user,
  `ogchaos` (§5).
- **Restart policy:** `Restart=always` and `RestartSec=2`, the same as production. Mode R measures exactly this.
- **Priority:** lower than production (`CPUWeight=20`, `IOWeight=20`, `Nice=10`), so production wins any contention
  on the shared host. `MemoryMax` values are smaller than production's (§13, Q3 gives the 2,000-hub budget).
- **Lifecycle:** `PartOf=ogt-stack@%i.target` and no `WantedBy=multi-user.target`, so a chaos stack never starts on
  boot.

### 4.2 Configuration

`orchestrator/config/chaos.toml` (architect) is `test.toml` plus the chaos settings. The deploy setup script renders
it per instance to `/etc/opengrid/chaos/%i.toml` with that instance's port block. It also renders the sim YAMLs
(`%i-fleet.yaml`, `%i-scada.yaml`) from `integration-sims/config/{fleet,scada}.yaml` with:
- `topic_root: ogtest/%i`;
- the chaos public-key paths;
- the chaos market and control ports.

- **Ports.** For instance N (`chaos` is 0), the base is B = 28000 + 100·N:
  - `og-api` on B+80;
  - market sim on B+90, which the feeds' `base_url`s point to;
  - sim control on B+91;
  - guardian `/metrics` on B+3;
  - engine `/metrics` on B+1, once the invariants agent adds it.

  For `chaos` that is 28080, 28090, 28091, 28003 and 28001.
- **Fleet size:** nightly runs use 2,000 hubs and 40 banks, matching production, at low priority (lead default, §13
  Q3). Development runs and dry runs use 200 hubs and 8 banks, as in `test.toml`.
- **Other settings:**
  - key paths as in §4.1;
  - `pq_ingest.blob_store_dir` and every other writable path under `/var/lib/opengrid/chaos/<ws>/` (with
    `ReadWritePaths` limited to that tree, a missed setting fails to write rather than landing in a production
    directory);
  - `client_id_prefix = "ogt"` (effective after B1);
  - the workspace's MQTT account `ogw_<ws>` (B2);
  - a DB user of its own (§4.3).

### 4.3 Database

`og_t_<ws>` is owned by a chaos role, `ogt_<ws>`, that has no privileges on the production database `og`. The harness
reads through a read-only role, `ogt_<ws>_ro`.

### 4.4 Reset

`reset <ws>`:
1. Stop `ogt-stack@<ws>.target`.
2. Drop and recreate `og_t_<ws>`.
3. Run `python -m opengrid.platform.db migrate` (`Makefile:30`) and `python -m opengrid.fleet.seed`
   (`o/fleet/seed.py:264`).
4. Clear the retained messages under `ogtest/<ws>/#` (stop, lease) with the chaos user, using zero-length retained
   publishes while the stack is stopped. The restarted sims then start with no stop engaged. This is housekeeping on
   a stopped stack, not a release path.
5. Start the target.
6. Wait for steady state (§6.1).

A reset is also the fallback way to clear an engaged safe stop. At `434d230` a two-person, guardian-signed RELEASE
exists (`o/guardian/stop_release.py`; the API request/approve steps in `o/api/routers/safestop.py`). Stop experiments
therefore end with a real release where they can (§6.6), and fall back to a reset if the release fails.

## 5. Safe targeting (defence in depth)

Each layer works on its own. L1 to L4 would each stop a mistaken kill by themselves.

**L1: OS (polkit).** The harness runs as `ogchaos`, never as root (root bypasses polkit). The rule below lets that
user start, stop, restart and kill chaos units and nothing else. The deploy owner finalizes and tests it:

```javascript
// /etc/polkit-1/rules.d/60-ogchaos.rules
polkit.addRule(function (action, subject) {
  if (subject.user !== "ogchaos") return polkit.Result.NOT_HANDLED;
  if (action.id !== "org.freedesktop.systemd1.manage-units") return polkit.Result.NO;
  var unit = action.lookup("unit") || "";
  var verb = action.lookup("verb") || "";
  var chaosUnit =
    /^ogt-(feeds|engine|guardian|safestop|settle|api|sim-(fleet|scada|market|control))@chaos[0-9]?\.service$/.test(unit) ||
    /^ogt-stack@chaos[0-9]?\.target$/.test(unit);
  var allowedVerb = ["start", "stop", "restart", "kill"].indexOf(verb) >= 0;
  return chaosUnit && allowedVerb ? polkit.Result.YES : polkit.Result.NO;
});
```

Even a harness bug cannot touch an `og-*` unit: the D-Bus call is refused.

The rule needs a systemd version that passes the `unit` and `verb` details for `manage-units`. On an older version
those lookups come back empty and the rule denies everything. That fails closed but leaves the harness unable to run,
so the deploy owner records the server's systemd version and tests the rule both ways: a chaos kill is allowed, an
`og-*` kill is denied.

**L2: allowlist.** The harness resolves each logical target (for example `engine`) to exactly one unit name with the
same pattern and the configured workspace. It refuses:
- any name that starts with `og-`;
- any glob;
- any unit missing from `systemctl list-units --all 'ogt-*@<ws>.*'`;
- any unit whose `FragmentPath` is not `/etc/systemd/system/ogt-<process>@.service`.

**L3: identity preflight.** For each target, the harness runs a read-only
`systemctl show <unit> -p Environment -p EnvironmentFiles -p User -p WorkingDirectory -p ControlGroup` and requires:
- `Environment` has `OG_DB=og_t_<ws>` and `OG_MQTT_ROOT=ogtest/<ws>`. Sim units must also have their key paths under
  `/var/lib/opengrid/chaos/<ws>/`. `og/v1` and the database `og` never appear.
- `EnvironmentFiles` is exactly `/etc/opengrid/chaos/<ws>.env`.
- `User=ogchaos-svc`, and `WorkingDirectory` is under `/opt/opengrid/work/<ws>/`.

`-p Environment` prints only the unit's `Environment=` lines, which hold no secrets. The harness never reads or prints
the env files.

The unit's environment is not enough, because the settings that matter most live in the files it points to. So L3
also parses the rendered `/etc/opengrid/chaos/<ws>.toml` and `<ws>-{fleet,scada}.yaml`, which hold no secrets, and
requires:
- the MQTT topic root is `ogtest/<ws>`;
- the database is `og_t_<ws>`;
- every key path, `pq_ingest.blob_store_dir` and every other writable path is under `/var/lib/opengrid/chaos/<ws>/`;
- every port is inside the instance's block;
- `client_id_prefix` is not `og`;
- the MQTT username is `ogw_<ws>`, and the observer's is `ogw_<ws>_ro`.

At runtime, the workspace's broker account (`ogw_<ws>`, B2) is the backstop: it cannot publish or subscribe outside
`ogtest/<ws>/#`.

**L4: production sentinel.** Before the run and after every step, the harness reads `ActiveState`, `NRestarts` and
`InvocationID` of every `og-*.service`. These reads are unprivileged and read-only. If any value changes, the run
becomes ABORTED.

The harness also refuses to start unless every `og-*` unit is `active`: chaos never runs while production is already
degraded. The harness has no DB or MQTT access to production at all.

**L5: resource guard.** Before each experiment, the harness checks that `MemAvailable` is at least 4 GiB and the
1-minute load average is below the core count. Otherwise it waits, and after 10 minutes the run is ABORTED. The units'
CPU and IO weights (§4.1) keep production ahead during an experiment.

**L6: abort and cleanup.** There is one abort path, triggered by Ctrl-C, a failed gate or the file
`/run/ogchaos/<ws>.abort`. It:
- starts every chaos unit the harness stopped;
- writes the report as ABORTED;
- exits.

It never touches an `og-*` unit.

**L7: secrets hygiene.**
- The harness never prints environment files, passwords or private keys.
- Evidence bundles contain query results, unit properties and message metadata only.
- The MQTT observer logs topics, hub ids, timestamps and numeric fields, never credentials.

**L8: workspace reservation.** Listing `chaos*` in BUILD.md §5 is only a convention: `tools/remote.ps1:20` accepts
any workspace name. So nothing else may use a chaos workspace while an experiment runs, enforced technically:
- `og_t_chaos*` databases revoke `CONNECT` from every role except the chaos roles;
- the per-workspace broker accounts (B2) confine every other workspace to its own root, so none can reach
  `ogtest/chaos*/#`;
- `remote.ps1` refuses `-Ws chaos*`.

The harness also takes an exclusive lock, `/run/ogchaos/<ws>.lock`. Before each experiment it checks that
`pg_stat_activity` shows no session on `og_t_<ws>` from a role other than the chaos roles.

## 6. Fault injection

### 6.1 Steady state

Every experiment starts from steady state:
- every chaos unit is `active`;
- every orchestrator heartbeat in `og.heartbeat` is fresher than `heartbeat_interval_s` × `heartbeat_miss_threshold`
  (5 s × 3 = 15 s, `test.toml:80-81`; the engine uses the same product, `o/engine/__init__.py:505`);
- at least 95 % of hubs have fresh telemetry;
- at least one firm obligation is `DELIVERING` with an active commitment;
- the guardian's PASS share over the last 60 s is no worse than the campaign baseline minus 10 points;
- no `ALR-PROCESS-DOWN` alert is open.

If steady state is not reached within 300 s, the experiment is INVALID.

### 6.2 Kill primitive

```
systemctl kill --kill-whom=all --signal=SIGKILL ogt-engine@chaos.service
```

Before and after the kill, the harness records `systemctl show <unit> -p MainPID -p InvocationID -p NRestarts -p
ActiveState -p SubState -p Result -p ExecMainCode -p ExecMainStatus`. The kill counts as confirmed when both hold:
- the old `MainPID` is gone;
- the unit reports `Result=signal` and `ExecMainStatus=9`.

Otherwise the experiment is INVALID. `--kill-whom=all` also kills short-lived children, such as the guardian's
`chronyc` subprocess.

### 6.3 Mode R: crash, then automatic restart

1. Kill the unit.
2. Check that systemd restarted it: `NRestarts` went up by 1, the `InvocationID` is new, and `ActiveState=active`
   within `RestartSec` (2 s) plus the unit's startup budget (default 30 s).
3. Run the recovery assertions (§7) for 120 s.

This covers 02b's availability checks (a) to (d). Check (e), that `og-safestop` can still engage a stop while
`og-guardian` or `og-engine` is down, is covered only by the mode-H stop sub-steps of TS-C-02 and TS-C-03 and by
TS-06-23.

### 6.4 Mode H: crash, then hold down

1. Kill the unit, then immediately run `systemctl stop <unit>`. After a SIGKILL the unit sits in the `auto-restart`
   state for `RestartSec`, and a stop issued in that window cancels the pending restart. The process stays dead, and
   its graceful shutdown never runs.
2. Check that `NRestarts` did not go up between the two calls. If the restart won the race, the experiment is INVALID
   and is retried; it never counts as PASS or FAIL.
3. Hold for the outage window, 120 s by default. That is longer than `lease_ttl_s` (30 s, `test.toml:61`) plus the
   sim's `lease_hold_after_expiry_s` (5 s, the code default at `ogsim/common/config.py:75`; `fleet.yaml:16` has the
   same value), so the lease path runs to completion.
4. Run `systemctl start <unit>` and observe recovery for 120 s.

Mode H needs no runtime drop-ins and no `daemon-reload`, so polkit only has to allow the four `manage-units` verbs.

### 6.5 Never used

- `pkill`, `killall`, `kill <pid>`;
- `systemctl restart` or a plain `systemctl stop`, both graceful, so neither is a crash;
- anything that touches `postgresql`, `mosquitto` or an `og-*` unit.

### 6.6 Campaign

- Experiments run one at a time in a seeded random order; the seed goes in the report. After each experiment the stack
  must return to steady state before the next one starts.
- Experiments that engage a safe stop run last. They end with a real two-person release through the chaos API:
  chaos operator A requests, operator B approves, as the D-12 test operators og-op-a/og-op-b do on dev. That release
  is itself an assertion: a self-approval is refused (403), and the stop lifts only after B approves. A reset (§4.4)
  is the fallback if the release fails. See §13 Q2.
- At the start, the harness injects a firm call (for example `PARTNER_CAPACITY`, as in step 6 of the §5 demo script)
  through the chaos API's scenario endpoint (`o/api/routers/scenario.py:33`), so there is a committed obligation to
  protect.
- The sim control's random profile stays off, so runs are deterministic, unless an experiment says otherwise.

### 6.7 Stop sub-steps: the operator stop CLI

K8 must not depend on `og-api` (lead default, §13 Q1). Today the only stop intake is `og-api`'s Postgres NOTIFY to
`og-safestop` (`o/safestop/main.py:76-81`). So the stop sub-steps engage through an operator CLI on the host. It is a
**dependency**, to be built by the safestop owner:

- **Command:**

  ```
  python -m opengrid.safestop.cli engage --scope {FLEET|ZONE|BANK} --ref <id> --reason <text> \
      --operator <id> --confirm <scope-ref>
  ```

  `--confirm` must repeat the scope reference; this replaces the UI's two-step confirmation. There is no release
  subcommand, because the stop key can only stop.
- **Engage path:** it loads the stop-only key through `og-safestop`'s own loader (`o/safestop/keys.py:78`). It builds
  the event with `o/safestop/events.py:54` and runs the same engage sequence as `SafestopService.engage`
  (`o/safestop/service.py:50`): trace, then the `og.stop_event` row, then the retained publish. It therefore works
  with `og-api` down and with the `og-safestop` daemon down.
- **MQTT client ID:** its own, `<prefix>-<ws>-safestop-cli`. It never reuses the daemon's ID, or it would disconnect
  the daemon (B1).
- **Who can run it:**
  - Production: operators run it as root over SSH (there is no `sudo`). The stop key stays readable only by root and
    the service user.
  - Chaos stack: the harness runs it with the chaos key, which is readable by the `ogchaos` group (§14).

The API path is still exercised where it matters: TS-C-03b checks that it is refused visibly while `og-safestop` is
down.

## 7. Experiments

Modes (lead default, §13 Q4):
- Every row runs in mode R, which checks the restart (§6.3) and the recovery column.
- TS-C-01 (feeds), TS-C-02 (engine), TS-C-03 (guardian), TS-C-03b (safestop) and TS-C-04 (sims) also run in mode H.
  TS-C-01's mode H asserts the NO_NEW_COMMITMENTS gate. R2 (`main` `6470cfa`) built it: og-engine skips intake
  (`o/engine/gates.py:51-60`) and the selector gate selects nothing new (`o/selector/gate.py:532-602`), so the row
  is expected to pass. Its hold must outlast the chaos config's `[feeds.staleness] ercot_price_fresh_s`. That is
  600 s in `test.toml:43-49`, which `chaos.toml` inherits; production uses 2,700 s since the R2 hotfix, so
  `chaos.toml` must keep the short window.
- TS-06-23 is mode H by definition.

In an R-only row (TS-C-05, TS-C-06) the outage lasts only the restart gap: `RestartSec` plus startup,
roughly 2 to 30 s. So its outage column is checked only for what that gap can show, and the degraded-mode behaviour
is covered elsewhere, as noted in the row.

Stop sub-steps use the operator CLI (§6.7). Stop latency is judged against the spec's per-scope ramp window: BANK
30 s, ZONE 60 s, FLEET 120 s (`o/safestop/events.py:18`, 02a §6.5). The sim itself ramps in 4 s
(`ogsim/common/config.py:76`), and the report records the measured time.

| ID | Kill | Expected (test plan §4.6) | Assert during the outage | Assert after recovery |
|---|---|---|---|---|
| TS-C-01 (R, H) | `ogt-feeds` | Feeds go stale; "no new commitments" after the staleness threshold; existing commitments unaffected | Every commitment active at the kill is unchanged (K13). R: the restart gap is shorter than the staleness threshold, so degraded mode is not expected. H: the feed is held past the staleness threshold; `NO_NEW_COMMITMENTS` is active (`og.degraded_mode_state`), and 0 new `og.commitment` rows are created while it is active. The gate is built in R2, so the assertion is expected to pass | Feeds fresh; no commitment created from stale data; commitments continue at the next gate |
| TS-C-02 (R, H) | `ogt-engine` | Hubs hold their lease, then go to local autonomy (K6/K7); guardian idle; `og-safestop` can still engage a stop (K8); state rebuilt from Postgres | No new `og.command_batch` rows. Each leased hub's `p_kw` follows its last acked `applied_p_kw` until `expires_at` + 5 s, then local autonomy, never earlier (K7). `ALR-PROCESS-DOWN` for the engine. Stop sub-step (H only, last in the campaign): a BANK stop through the operator CLI gives an `og.stop_event` ENGAGE row, a retained stop message, and in-scope hubs at 0 within the BANK window of 30 s (K8) | New epoch = previous `max(og.lease_state.epoch) + 1` (`o/engine/pg_backend.py:38`), strictly higher (K6). No `STALE_EPOCH`/`STALE_SEQ` rejects for new-epoch batches. Commitments intact (K13); reservation sums ≤ capability (K2). Grants and acks resume within 3 cycles. The trace verifies (K11) |
| TS-C-03 (R, H) | `ogt-guardian` | TIMEOUT semantics: the engine holds its last grant, no new commands execute; `og-safestop` unaffected | No new `og.verdict` rows. Every batch on `cmd/+/batch` verifies with the chaos guardian key, and no ack is `accepted` for a batch without a signed verdict (K3). The engine sees the guardian as unavailable (heartbeat older than 15 s, `o/engine/__init__.py:505`) and holds. Hubs follow the lease path (K7). An L2 BLOCK injected on one bank during the outage is met by that bank's hubs at the latest at lease expiry + hold, 35 s (K5; documented limitation, §13 Q6). Optional stop sub-step through the CLI, as in TS-C-02 (K8) | Signing resumes: first PASS verdict within 5 cycles. The first signed batches respect G-02 to G-06 and any active L2 instruction (K4, K5). G-20 passes (K12). No `TIMEOUT` verdict carries a signature |
| TS-C-03b (R, H) | `ogt-safestop` | Dispatch unaffected; a stop attempted while it is down is visibly refused; readiness returns on restart | Verdict and command rates within ±10 % of baseline. A propose + confirm through the chaos API gets 503 from confirm (`o/api/routers/safestop.py:74-96`) and no ENGAGE row appears. (H only) The operator CLI still engages a BANK stop with the daemon down, within the 30 s window (K8, §6.7) | No ENGAGE row ever appears for the refused API request. The request path is Postgres NOTIFY, which is not durable, so none should. An API stop request engages again; reset afterwards |
| TS-C-04 (R, H) | `ogt-sim-fleet`, and `ogt-sim-scada` | Telemetry stops; the fleet twin marks hubs stale within one detection cycle; the engine holds; no commands into a void | `og.hub_state.last_seen_at` stops advancing and hubs leave `online`; `ALR-HUB-OFFLINE-RATIO` opened; no discharge signed for a hub that is not online (G-01 `HUB_SOC_NOT_LIVE`, `o/guardian/service.py:180-183`) | Telemetry resumes, hubs back online, acks resume. A restarted sim re-initializes its physical state (SoC), so the K1 baseline is reset at the restart |
| TS-C-05 (R) | `ogt-settle` | M&V and billing stop advancing; health evaluation stops (it runs in `og-settle`, `o/settle/main.py:86-105`); no data loss or double count on restart | No `og.meter_interval` or `og.pnl` rows while the unit is down. Health evaluation pauses with the process, so the harness reads heartbeats itself rather than relying on `ALR-PROCESS-DOWN` | Processing resumes from the last interval with no gap. Each (obligation, interval) has at most one active `og.meter_interval` and one active `og.pnl` row (`ux_meter_active`, `ux_pnl_active`). Trace pruning resumes; the trace verifies (K11) |
| TS-C-06 (R) | `ogt-api` | Operators lose the console; nothing else is affected; the UI comes back with the correct state | The API port refuses connections; verdict, command and ack rates within ±10 % of baseline. Stopping without `og-api` is proven by the CLI sub-steps of TS-C-02, TS-C-03b and TS-06-23 | The API's health endpoint (`o/api/routers/health.py:82`) shows every process up, consistent with `og.heartbeat` |
| TS-06-23 (H) | `ogt-engine` and `ogt-guardian` | A fleet stop engages through `og-safestop`'s authority alone | A FLEET stop through the operator CLI gives an ENGAGE row, and every hub reaches 0 within the FLEET window of 120 s. Both killed units stay dead for the whole experiment | Reset (§4.4) |

## 8. Expected K7 and K8 behaviour per kill

| Killed | K7: degrade, don't trip | K8: stop authority |
|---|---|---|
| `og-feeds` | Degraded mode "no new commitments"; existing commitments keep delivering | Unaffected |
| `og-engine` | Hubs hold the last signed setpoint until the lease expires plus 5 s, then go to local autonomy; firm obligations never step to 0 before that | A scoped stop engages with the engine down (operator CLI, §6.7) |
| `og-guardian` | The engine holds its last grant (a hold, not a stop); hubs follow the lease path | A scoped stop engages without the guardian (operator CLI) |
| `og-safestop` | No effect on dispatch | While it is down, API stop requests are refused visibly (503) and never engage later; the operator CLI still engages (lead default, §13 Q1) |
| `og-settle` | No effect on dispatch; health evaluation pauses | Unaffected |
| `og-api` | No effect on dispatch | The UI stop path is unavailable; the operator CLI still engages (lead default, §13 Q1) |
| `ogsim.fleet` / `ogsim.scada` | The engine holds; no commands for stale hubs | Not applicable: the "hubs" are down |

## 9. Measurements

### 9.1 Sources

Every source is independent of the processes the harness kills.

- **MQTT observer.** A subscriber with the read-only account `ogw_<ws>_ro` (proposed, B2) on `ogtest/<ws>/#` records:
  - telemetry: `soc_kwh`, `p_kw`, `health`, `seq`, `epoch` (`interfaces/mqtt/telemetry.schema.json`);
  - acks: `accepted`, `applied_p_kw`, `reject_reason` (`ack.schema.json`);
  - command batches, with each signature verified against the chaos guardian public key;
  - leases (`expires_at`) and stop messages.

  While `og-engine` is down it is the only source of hub-side truth, because the engine is the process that persists
  telemetry and acks to Postgres.
- **Postgres,** through the read-only role:
  - dispatch: `og.verdict`, `og.command_batch`, `og.command_ack`, `og.lease_state`, `og.hub_state`;
  - obligations and commitments: `og.commitment`, `og.reservation`, `og.grant`;
  - stop, health and trace: `og.stop_event`, `og.heartbeat`, `og.alert`, `og.trace`;
  - settlement: `og.meter_interval`, `og.pnl`.
- **Guardian `/metrics`** on the chaos port: `og_guardian_verdicts_total` and `og_guardian_clock_offset_ms`.
- **systemd unit properties** (§6.2).
- **Host metrics from sysstat** (`sar`), which is enabled on the server: CPU, memory and I/O over each experiment, for
  the report and the L5 guard.
- **The invariants agent's durable records:** `og.invariant_check` and `og.invariant_violation` (migration 0014), and a
  scheduled trace verify every 300 s. These shipped on `main` after `f3b3365`, as reported by the lead; this design
  has not yet checked their schema, so the exact names are confirmed when this branch is rebased.
- **The Prometheus counters** `og_reserve_breaches_total` and `og_double_sold_kwh_total`, as a secondary signal only
  (§9.2).

### 9.2 Why counters alone are not enough under chaos

A Prometheus counter lives in the memory of the process that increments it. Killing that process resets the counter to
0 and loses every increment since the last scrape, and a breach just before a kill is exactly what chaos testing is
looking for. So:

- The harness treats a counter that goes down as a reset and sums the segments.
- The durable record is the evidence. For each experiment window, the harness reads `og.invariant_violation` and
  requires zero new rows for the invariants in §9.3. It also reads `og.invariant_check` to confirm the checks
  actually ran during the window: a window with no checks is INVALID, not PASS.
- Every counter assertion has a direct measurement behind it (§9.3). If the two disagree, that is itself a FAIL.
- A value that is a constant today is never evidence. The API's invariant counters are hard-coded to 0
  (`o/api/routers/health.py:34`, `o/api/routers/dispatch.py:120-121`).

### 9.3 Continuous invariant assertions

These are checked for the whole campaign. Any violation fails the experiment that is running.

| K | Assertion | Source |
|---|---|---|
| K1 | Every telemetry sample has `soc_kwh ≥ r_kwh − ε` for its hub; no new K1 row in `og.invariant_violation` | observer, `og.hub.r_kwh`; `og.invariant_violation` |
| K2 | Per hub, bank and interval, the sum of active reservations ≤ capability; no new K2 row in `og.invariant_violation` | `og.reservation`; `og.invariant_violation` |
| K3 | Every batch on `cmd/+/batch` verifies with the chaos guardian key; every `accepted=true` ack belongs to a batch whose verdict is PASS or PARTLY_VETOED and has a signature | observer; `og.command_ack` joined to `og.verdict` |
| K4 | Every telemetry sample has \|`p_kw`\| ≤ the hub's rating; a hub's consecutive samples change by at most its ramp limit × Δt plus tolerance (`o/core/physics.py:34`); the fleet-wide change per cycle stays within the G-05 cap. Bank kVA is not asserted while the scada sim reports loads far above rating (`o/guardian/service.py:213-215`) | observer; `og.hub.p_kw` |
| K5 | While an L2 instruction is active for a scope, no signed batch commands that scope beyond it (BLOCK/ESTOP: 0; LIMIT: aggregate ≤ limit). The scope's aggregate telemetry meets it within 2 cycles when the stack is healthy, and at the latest at lease expiry + hold when the guardian or engine is down | observer (`scada/instruction/#`, batches, telemetry) |
| K6 | Per bank, the (epoch, seq) of accepted batches strictly increases, and after an engine restart the epoch is strictly higher | observer (batches and acks); `og.lease_state` |
| K7 | No `TIMEOUT` verdict has a signature; each leased hub's `p_kw` follows its last acked `applied_p_kw` until `expires_at` + `lease_hold_after_expiry_s` | `og.verdict`; observer |
| K8 | No RELEASE row in `og.stop_event` (the stop key can only stop). While the stack runs, nothing is published on `stop/#` except signed ENGAGE events; a JSON-empty payload releases a fleet stop in the sim (§3). Stop latency within budget | `og.stop_event`; observer |
| K10 | Every signed verdict's batch has a `trace_pre_image_id` that exists in `og.trace` | SQL |
| K11 | `verify()` passes for every stream after each recovery. The scheduled verify runs every 300 s, which can fall outside a 120 s recovery window, so the harness also triggers one on demand. The next scheduled result must pass as well | the chaos API's `POST …/trace/verify` (`o/api/routers/billing.py:107`); the scheduled verify's record |
| K13 | Every commitment active at a kill keeps its `committed_kw`; any reduced grant carries an allowed reason code; no new `K13_LOCK_VIOLATION` row. A mode-H engine or sim outage longer than 30 s is expected to add `K13_OUTAGE_GAP` rows (a grant-activity gap, reported separately by design, `o/invariants/checks.py` `classify_dip`); the harness records them as evidence, not failures | `og.commitment`, `og.grant`, trace; `og.invariant_violation` |

The K7 hold check is inferred from `p_kw`, because the sim tracks lease state internally
(`ogsim/fleet/lease.py:30-49`) but does not publish it. If telemetry carried the lease state (§11, item 4), the
assertion could be exact.

### 9.4 Timing budgets

Each budget is a parameter; the defaults come from config.

| Budget | Default | Source |
|---|---|---|
| Heartbeat down | 15 s | `test.toml:80-81` |
| Lease | 30 s | `test.toml:61` |
| Hold after lease expiry | 5 s | `ogsim/common/config.py:75` (code default; the production sims load no YAML) |
| Stop ramp (pass/fail bound) | BANK 30 s, ZONE 60 s, FLEET 120 s | `o/safestop/events.py:18` |
| Stop ramp (sim behaviour, recorded) | 4 s | `ogsim/common/config.py:76` |
| Cycle | 2 s | `test.toml:64` |
| Restart (mode R) | `RestartSec` 2 s + 30 s startup | unit files |
| Outage (mode H) | 120 s | test plan §4.6 |
| Recovery | 120 s | test plan §4.6 |
| PostgreSQL checkpoint | every 15 min (`checkpoint_timeout`; `max_wal_size` 4 GB, `wal_compression` zstd) | server configuration, owner 2026-09-26 |

A checkpoint can land inside an experiment and raise I/O latency. At 15 minutes, a 70 to 110 minute campaign sees
about five to eight. For each experiment the report records:
- whether a checkpoint fell inside its window, from `pg_stat_checkpointer` (PostgreSQL 17);
- the host's I/O and CPU, from sysstat.

Latency-sensitive assertions (the restart budget, cycle timing) are judged with that context. A timing miss that
coincides with a checkpoint is re-run once before it counts as a FAIL.

## 10. Harness architecture (for the Q3 build)

- **Location.** `orchestrator/tests/chaos/`, the path 02b names, under a `chaos` pytest marker. It is collected only
  when `OG_CHAOS_WS` is set, so it is never part of the unit or integration runs.
- **Modules:**
  - `planner`: experiments, seed, order.
  - `safety`: L1 to L8, and the only module that calls `systemctl`.
  - `inject`: kill, stop and start, always through `safety`.
  - `observe`: the MQTT observer, DB poller, metrics scraper and unit poller, each with timeouts.
  - `assertions`: pure functions over recorded samples, unit-testable without a server.
  - `report`.
- **Where it runs.** On the server, as `ogchaos`, over SSH, in the same way `tools/remote.ps1` runs commands as
  `opengrid`. Never as root.
- **When it runs** (lead default, §13 Q5): no continuous chaos. Two triggers:
  - nightly, from a timer (§14) that starts at 00:30 server time with a hard stop at 02:45, so it never overlaps the
    03:00 production backup (`deploy/cron/opengrid`);
  - on demand before each release.

  A nightly campaign at 2,000 hubs is estimated at 70 to 110 minutes; the 00:30 to 02:45 window allows 135. The
  estimate comprises:
  - 7 mode-R experiments at 3 to 5 minutes each;
  - 5 mode-H experiments at 5 to 7 minutes each;
  - TS-06-23;
  - up to four resets of about 5 minutes each.

  The first runs measure the real figure.
- **Dry run.** A dry-run mode prints the plan, resolves the units and runs the read-only checks (L2 to L5, L8)
  without killing anything.
- **Report.** JSON and Markdown per campaign in `qa/chaos/<date>-<ws>/` (qa-owned). It contains:
  - the seed and the timeline;
  - the result of each experiment;
  - every failed assertion with its query and values;
  - unit properties before and after;
  - the production sentinel readings.

  No secrets.

## 11. Ownership and order of work

Status as of 2026-09-26, as reported by the lead:

1. **Code fixes: with the safety agent.**
   - B1: client IDs built as prefix + `OG_WS` + process, refused outside production.
   - B3: the environment wins over the YAML, key-path overrides, a package-relative config path, and `og/v1` refused
     without a production marker.
   - The `test.toml` blob-store override.
   - The K8 unsigned-release fix (`ogsim/fleet/__main__.py`, `topics.md`).
   - Per-workspace MQTT accounts `ogw_<ws>` (B2): being implemented, owner decision 2026-09-26.
2. **Config in the repo:**
   - `orchestrator/config/chaos.toml`: architect.
   - `chaos` workspaces added to the BUILD.md §5 list, and `remote.ps1` refusing them: lead.
3. **Invariants agent: shipped.**
   - Durable `og.invariant_check` and `og.invariant_violation` (migration 0014).
   - The scheduled trace verify every 300 s.
4. **Live-path agent: queued.** The engine `/metrics` endpoint.
5. **Safestop owner: dependency.** The operator stop CLI (§6.7).
6. **Deploy: waiting for owner approval** of the §14 checklist. Nothing on the server changes before that.
7. **Sims (optional; makes K7 exact):** publish each hub's lease state (`leased`, `holding`, `local_autonomy`) in
   telemetry.
8. **Q3 lane:** the harness (§10). First on the local compose stack (`dev/docker-compose.yml`) for the observers and
   assertions, then on `chaos` once items 1, 2, 5 and 6 are done.

## 12. Later extensions

- **K12 clock skew,** after the G-20 fail-open fix: a fake `chronyc` on the chaos guardian's `PATH` reports a skewed
  offset; assert that verdicts become TIMEOUT holds with no signature.
- **K3 negative control:** publish a forged or unsigned batch on the chaos topic root; assert that every hub rejects
  it (`BAD_SIGNATURE`).
- **TS-N-07/08** (Postgres and Mosquitto restarts) on a dedicated host.
- **Network faults** with `tc` or nftables inside a network namespace that holds only the chaos units.
- **Follow-up to §13 Q6:** `og-safestop` subscribes to utility L2 BLOCK and ESTOP instructions (`scada/instruction/#`)
  and issues a scoped stop itself. An L2 instruction would then be honoured within the scope's ramp window even while
  the guardian or engine is down. The chaos experiment for it: inject a BLOCK with the guardian held down, and assert
  that the stop engages before the lease expires.

## 13. Questions and lead defaults (pending owner confirmation)

The lead answered these on 2026-09-26. Each answer is the default until the owner confirms or changes it.

1. **Stop path with `og-api` down.** Today the only stop intake is `og-api`'s Postgres NOTIFY
   (`o/safestop/main.py:76-81`).
   *Lead default, pending owner confirmation:* a second intake is required, and the design must not rely on
   `og-api` for K8. The answer is an operator CLI on the host that signs with `og-safestop`'s stop-only key directly
   (§6.7). It is a dependency, owned by the safestop owner.
2. **Ending stop experiments.** *Lead default, pending owner confirmation (updated 2026-09-26):* every stop experiment
   ends with a real two-person release (§6.6), with a workspace reset (§4.4) as the fallback. The earlier default,
   "a reset until a guardian-signed RELEASE exists", no longer applies: the two-person RELEASE is on main at `434d230`.
3. **Scale and host.** The base server is the permanent hosting solution (owner decision 2026-09-26).
   *Lead default, pending owner confirmation:* nightly chaos runs there, within its limits: 2,000 hubs at low
   priority (§4.1), guarded by the L4 production sentinel and the L5 resource guard.

   The limit to watch is memory. The production units' memory caps add up to 8.75 GiB (`MemoryMax` in
   `deploy/systemd/*.service`), and the test plan puts the server's free memory at about 14 GB for the whole production
   stack (`04-mvp-s-test-plan.md` TS-N-05). A chaos stack with the same caps would not fit next to production if both
   hit their caps. So:
   - the chaos caps total about 5.5 GiB at 2,000 hubs (for example engine 1.5G, fleet sim 2G, the rest 256M to 512M);
   - the first nightly runs record each unit's peak memory, with sysstat for the host view, to set the caps properly;
   - L5 pauses or aborts whenever available memory drops below 4 GiB.

   A separate host remains optional; Appendix A gives its sizing and cost.
4. **Modes.** *Lead default, pending owner confirmation (updated 2026-09-26):* mode R for every process; mode H also
   for og-feeds, the engine, guardian, safestop and sims. og-feeds' mode H asserts 0 new commitments while
   NO_NEW_COMMITMENTS is active; R2 built that gate. The consequence:
   - `og-feeds`' degraded mode ("no new commitments") is not reached in the restart gap; TS-02-05/07 cover it with a
     stubbed stale feed. The 2026-09-26 review found that mode displayed but not enforced. R2 enforces it at the
     intake and selector gates (not at contract admission). The mode-H `og-feeds` kill, held past the chaos
     config's 600 s window, tests that enforcement end to end.
   - For `og-settle` and `og-api`, the outage behaviour is observed only for the restart gap: settlement and health
     evaluation pause, and the console is lost.
5. **Cadence.** *Lead default, pending owner confirmation:* nightly (the §14 timer, 00:30 to 02:45), plus on demand
   before each release. No continuous chaos.
6. **L2 during an outage.** An L2 BLOCK or LIMIT that arrives while the guardian or engine is down cannot be enforced
   by new commands. Hubs keep their last setpoint until the lease expires plus the hold: 35 s at the test settings.
   *Lead default, pending owner confirmation:* acceptable for MVP-S, as a documented limitation (this item). The
   follow-up in §12 makes `og-safestop` react to L2 BLOCK and ESTOP itself.

## 14. Server changes that need the owner's approval (checklist)

Nobody changes the server's configuration for this design until the owner approves this list. Each item is owned by
deploy unless marked otherwise, and each is pending owner approval.

1. **Units:** the ten template units `ogt-*@.service` and `ogt-stack@.target` in `/etc/systemd/system/` (§4.1). They
   are never enabled for boot.
2. **Users:**
   - `ogchaos` runs the harness;
   - `ogchaos-svc` runs the chaos services;
   - a group, `ogchaos`, gives read access to the chaos keys.

   No `sudo` for any of them.
3. **Polkit rule:** `/etc/polkit-1/rules.d/60-ogchaos.rules` (§5 L1). Before relying on it, record the systemd
   version and test it both ways: a chaos kill is allowed, an `og-*` kill is denied.
4. **Directories:**
   - `/etc/opengrid/chaos/`;
   - `/var/lib/opengrid/chaos/<ws>/`;
   - `/var/log/opengrid/chaos/`;
   - `/run/ogchaos/`, created through `tmpfiles.d`.
5. **Per-instance files:**
   - the rendered `<ws>.toml`, `<ws>-fleet.yaml` and `<ws>-scada.yaml`;
   - `<ws>.env`, holding the chaos DB and MQTT passwords, `root:ogchaos-svc`, mode 0640.
6. **Keys:** per-workspace guardian and safestop Ed25519 key pairs, generated on the server under
   `/var/lib/opengrid/chaos/<ws>/`. Private keys never leave the server.
7. **Mosquitto:**
   - the chaos workspaces' accounts `ogw_chaos` … `ogw_chaos9` (readwrite `ogtest/<ws>/#` only). These come with the
     per-workspace account change already being implemented (B2), so they need only adding for the chaos names;
   - the read-only observer accounts `ogw_<ws>_ro`, proposed here;
   - applied by a reload, not a restart, after confirming the installed version reloads the password and ACL files
     without dropping connections.
8. **PostgreSQL:**
   - the roles `ogt_<ws>` (owner) and `ogt_<ws>_ro` (read-only), and the database `og_t_<ws>`;
   - `CONNECT` revoked from every other role;
   - `pg_hba` entries if needed, applied by a reload, not a restart.
9. **Timer:** `ogchaos-nightly.timer` and its service, which run the harness as `ogchaos` from 00:30 with a hard stop
   at 02:45, clear of the 03:00 backup (§10).
10. **Operator stop CLI:** installed with the normal deploy once the safestop owner has built it (§6.7). Production
    use keeps the stop key readable only by root and the service user.
11. **Not server config, listed for completeness:** `remote.ps1` refusing `chaos*` workspaces (lead or architect), and
    `orchestrator/config/chaos.toml` (architect).

## Appendix A. Optional separate host (sizing and cost)

Not planned: the base server is the permanent host, and nightly chaos runs there (§13 Q3). This appendix is kept only
in case the owner later wants chaos fully off the production machine.

A self-contained host carries its own PostgreSQL 17 and Mosquitto, which also makes TS-N-07/08 possible.

Sizing:
- 8 vCPU: the 2 s engine loop and the selector's MILP gates are CPU-bound (02b).
- 32 GB RAM: the 2,000-hub caps plus PostgreSQL and Mosquitto.
- At least 100 GB SSD: the database is recreated at every reset.

Cost:
- On existing hardware it costs no cash, only that share of the host.
- As a cloud reference, Hetzner's CCX33 (8 dedicated vCPU, 32 GB RAM) lists at €138.49 a month in Germany and
  Finland, or $165.99 a month in the US (ASH/HIL). Those prices exclude IPv4 and VAT and follow the price adjustment
  of 15 June 2026 (docs.hetzner.com, "Price Adjustment 15 June 2026", read 2026-09-26).
- At the listed hourly rate of $0.2660, an instance that exists only for the nightly window would cost about
  2.5 h × 30 × $0.2660 ≈ $20 a month, plus snapshot storage.
