# 15 — Data lifecycle on the node: partitions, retention, cold tier, rollups

Status: built 2026-09-26 for R3 (DATA-LIFECYCLE lane). Code: `orchestrator/src/opengrid/lifecycle/`. Schema:
`orchestrator/migrations/0033_data_lifecycle.sql`. Tests: `orchestrator/tests/unit/lifecycle/`,
`orchestrator/tests/integration/lifecycle/`.

Serves: 06 §4.3–4.4 (volumes, retention tiers, register R9), 06 §4.8 (audit and billing storage), 01 §7.1,
02a §1.14 and §8 (trace retention with checkpoints, K11), 05 RB-044 (disk budget and retention).

The node runs plain PostgreSQL 17. There is no TimescaleDB, and none is installed for R3. Everything here uses
native declarative partitioning, plpgsql and one Python job.

## 1. What is kept where, and for how long

The policy lives in `og.data_retention`, one row per table. The operator can change `hot_days`,
`keep_days`, `archive` and `legal_hold` with plain SQL, and the next hourly run applies the change. The shape
of each table is fixed in code, in the `opengrid.lifecycle.policy.DELETABLE_TABLES` whitelist: the time column,
how one day is exported and how rows are deleted. A table that is not in the whitelist is never deleted from,
whatever its policy row says.

| Table | Mode | Hot → cold export | Kept in PostgreSQL | Cold copy |
|---|---|---|---|---|
| `telemetry` (raw, per hub) | daily partitions | each closed day, the day after | **7 days**, then the partition is dropped | yes |
| `telemetry_1m` (rollup, the M&V source) | daily partitions | each closed day | **60 days** | yes |
| `telemetry_15m` (rollup) | table | each closed day | **kept** | yes |
| `command_batch` + its `verdict` rows | batched delete | at drop | **60 days** (a batch that a `calibration_attempt` references is kept) | yes (two archives: `command_batch`, `verdict`) |
| `command_ack` (command events) | batched delete | at drop | **60 days** | yes |
| `grant` | batched delete | at drop | **60 days** | yes |
| `pq_waveform_summary` | batched delete | at drop | **30 days** | yes |
| `pq_waveform_raw_index` + blob files | batched delete | — | **14 days**; a row with `retain_until` in the future is kept | no (the blob file is deleted with its row) |
| `feed_obs` | batched delete | at drop | **30 days** | yes |
| `plan_energy_value` | batched delete | — | **60 days** | no (derivable) |
| `lifecycle_run` (job log) | batched delete | — | 90 days | no |
| `invoice_line`, `pnl` | **never deleted** (`protected`) | monthly write-once export | forever | monthly |
| `meter_interval`, `performance` | **never deleted** (`protected`) | — | forever | — |
| `trace`, `trace_checkpoint` | **not this job** | — | per `og.retention_policy`, pruned only behind a checkpoint (K11, 02a §8.3) | — |

- `protected` rows carry a CHECK constraint that makes a deletion policy impossible
  (`keep_days` must be NULL and `mode` must be `NONE`).
- `legal_hold = true` stops every deletion for that table. Exports still run.
- `hot_days = 1` means that a closed UTC day is exported the day after it ends. This spreads the export work
  well ahead of the drop, and the drop itself then only re-verifies the export. `hot_days = NULL` means the day
  is exported at the moment it is dropped.
- A day is dropped once its end is at or before `today 00:00 UTC − keep_days`, so between `keep_days` and
  `keep_days + 1` days are always kept. All days are UTC days.

## 2. Mechanics

### 2.1 Daily partitions (`og.telemetry`, `og.telemetry_1m`)

- `og.ensure_telemetry_partitions(days_ahead int DEFAULT 7)` creates today − 1 through today + `days_ahead`. It
  wraps `og.lifecycle_ensure_daily_partitions(parent, days_back, days_ahead)`, and both are idempotent.
  - Partitions are named `telemetry_pYYYYMMDD` and cover `[00:00 UTC, next 00:00 UTC)`.
  - Each partition inherits the parent's `PRIMARY KEY (hub_id, ts)`, gets a BRIN index on `ts` and gets
    append-only autovacuum settings.
  - `lock_timeout` is 5 s.
- **The populated default partition.** Migration 0001 declared `og.telemetry` partitioned but created only
  `telemetry_default`, so every row landed there. With rows in the default, every `CREATE … PARTITION OF` would
  scan the default under an ACCESS EXCLUSIVE lock on `og.telemetry`. In that case the ensure function returns
  −1 and creates nothing. The job then **swaps** the default, and the swap moves no rows (`partitions.py`):
  1. `ADD CONSTRAINT lifecycle_swap_range CHECK (ts >= L AND ts < T) NOT VALID` on the default. This takes a
     brief lock and does not scan.
  2. `VALIDATE CONSTRAINT`. This scans under SHARE UPDATE EXCLUSIVE, so ingest is not blocked.
  3. One short transaction with a `lock_timeout`: `DETACH` the default, `RENAME` it `telemetry_pre<yyyymmdd>`,
     `ATTACH` it back as the range partition `[L, T)`, and create a new empty default. Postgres skips the
     attach scan because the constraint is already validated.

  Every row stays queryable through `og.telemetry` the whole time. `T` is the next UTC midnight. When midnight
  is less than 15 minutes away, `T` is the midnight after it, so that no in-flight row can fail the CHECK. `L`
  is `MINVALUE` on the first run. Rows up to `T` keep landing in the legacy range partition. From `T` on, rows
  land in the daily partitions. Retention drops the legacy partition like any other partition once `T` is more
  than `keep_days` old.

  If any step fails, the CHECK is removed and the table is exactly as it was before.

  *The brief proposed detaching the default and moving its rows in batches. The swap was chosen instead because
  it rewrites nothing and never hides rows from settle. A batched move would leave rows invisible until they
  were moved, and it would double the write volume.*
- `og.telemetry_1m` is partitioned the same way from the start. Its days-back window covers the rollup
  backfill.
- `og.pq_waveform_summary` and `og.feed_obs` are **not** converted to partitioned tables. Converting an
  existing table needs a rename and swap plus a copy or a dual-write window, and that cannot be done safely on
  the live database in this window. Both get BRIN time indexes, built with `CREATE INDEX CONCURRENTLY` by the
  job, and batched deletes. Section 7 lists them as a later conversion.

### 2.2 Retention executor (`retention.py`)

- **PARTITION mode:** the job takes each partition whose upper bound is at or before the cutoff, in order.
  1. It runs `DETACH` (brief ACCESS EXCLUSIVE, `lock_timeout`, 3 attempts). `DETACH … CONCURRENTLY` is not
     allowed while a default partition exists.
  2. It counts the rows per day in the detached table (one scan, no locks on the parent).
  3. It compares each count with the registered export and re-exports a day whose count differs (late rows).
  4. It re-verifies every export from disk.
  5. Only then does it run `DROP TABLE`.

  Any failure re-attaches the partition, records the error and stops that table. **Unarchived data is never
  dropped.**
- **DELETE mode:** the job works day by day from the oldest deletable row. The BRIN index narrows
  `min(ts) WHERE ts < cutoff` to the old ranges.
  1. It exports each archive of the table for that day and verifies it.
  2. It runs `DELETE … WHERE ctid = ANY(ARRAY(SELECT ctid … LIMIT n))`, where `n = [lifecycle].batch_rows`
     (at most 10,000). Each batch is one short autocommitted transaction.

  Two tables have special handling:
  - `command_batch` deletes its `verdict` rows first, in the same transaction.
  - `pq_waveform_raw_index` returns `blob_ref`. The job unlinks the file only when it resolves inside
    `[pq_ingest].blob_store_dir`, and it removes empty date and hub directories afterwards.
- `legal_hold` and `protected` block deletion. Tables outside the whitelist are skipped.
- Each run is written to `og.lifecycle_run` and traced with stream `lifecycle`, event_class `lifecycle` and
  decision_type `DATA_LIFECYCLE`. The trace records the counts per table: days exported, partitions dropped,
  rows deleted, blobs deleted and errors. Migration 0033 widens `og.trace`'s decision_type CHECK to a strict
  superset (NOT VALID, so there is no scan) and seeds `og.retention_policy('lifecycle', 1825)`.

### 2.3 Cold tier (`archive.py`)

- **Path:** `/srv/ogbackup/cold/<archive>/<yyyy>/<mm>/<dd>.csv.<zst|gz>` plus `<dd>.manifest.json`.
- **Contents:** CSV with a header row, produced by `COPY (SELECT …) TO STDOUT`. Count, min(ts), max(ts) and the
  COPY all run in one REPEATABLE READ snapshot, so the manifest describes exactly the rows in the file.
- **Codec:** zstd when the `zstandard` package is importable, else gzip from the standard library. The server
  venv has no `zstandard` today, so it writes `.csv.gz`. The codec is recorded in the manifest, and restore
  reads both. Parquet is not used, because `pyarrow` is not installed and not worth adding.
- **Write-once:**
  1. The file is written to `<name>.partial`.
  2. It is fsynced and renamed.
  3. Its mode is set to **0440**.
  4. The SHA-256 of the compressed bytes and the size are recorded in the manifest (also 0440) and in
     `og.lifecycle_archive`.
  5. The file is re-read from disk and its checksum compared **before any drop**. A day that later gains rows
     gets a new revision file (`<dd>.r2.csv.gz`), because an existing file is never overwritten.
- **Guards:** an export refuses to start when the cold volume has less than `[lifecycle].cold_min_free_gb`
  free. A failed export or verification means no drop. The error is recorded in the run and the trace, and the
  CLI exits 1.
- **Monthly billing export:** `/srv/ogbackup/cold/billing/<yyyy>/<mm>/{invoice_line,pnl}.csv.gz` plus
  `manifest.json`.
  - It covers the rows by `created_at`. Lines are insert-only and versioned, so a closed month never changes,
    and corrections land in a later month.
  - The combined manifest's `digest` is the SHA-256 over the member files' hashes (signed by checksum).
  - A month is written once, a day after it closes, and never rewritten. Nothing is deleted.

### 2.4 Rollups (`rollup.py`)

`og.telemetry_1m` and `og.telemetry_15m` hold one row per hub per bucket with these columns: `p_kw_avg/min/max`,
`soc_kwh_last`, `energy_in_kwh`, `energy_out_kwh`, `sample_count` and `computed_at`.

- **Energy convention:** `p_kw > 0` is charging (energy in) and `p_kw < 0` is discharging (energy out), which is
  settle's convention. `energy_*_kwh` is the minute's mean charging or discharging kW × 1/60 h. That is the same
  per-hub, per-minute averaging settle meters with. `sample_count` shows how much data stands behind each
  bucket.
- **15-minute buckets** are built from the 1-minute rows:
  - `p_kw_avg` is weighted by the sample count;
  - energies are sums;
  - `soc_kwh_last` is the last known value.
- **Incremental runs:** each rollup keeps a watermark in `og.lifecycle_watermark`.
  - Each run re-aggregates from `watermark − rollup_late_minutes` (default 15), so late telemetry is folded in.
  - Only complete minutes are aggregated. A 15-minute bucket is built only once the 1-minute watermark has
    passed its end.
  - The UPSERT recomputes whole buckets and rewrites a row only when its values changed. Re-running any range
    is therefore idempotent and adds no bloat.
  - A run does at most `rollup_max_hours_per_run` (default 6 h) of work in chunks of 15 minutes. A first run
    back-fills `rollup_backfill_hours` (default 24 h) over a few runs.

### 2.5 Performance settings (migration 0033 and `indexes.py`)

- **Autovacuum:**
  - `hub_state` is rewritten every 2 s. It vacuums at a fixed 5,000 dead rows, with `fillfactor 70` for HOT
    updates and no cost delay.
  - `heartbeat` is tuned the same way.
  - Append-mostly tables vacuum on inserts (`autovacuum_vacuum_insert_scale_factor 0.05`) and analyze at 2 %.
    This keeps the visibility map and the time-range statistics current.
- **BRIN time indexes:** built with `CREATE INDEX CONCURRENTLY` by the job, and an invalid leftover is rebuilt.
  They cover `feed_obs(ts)`, `pq_waveform_summary(ts)`, `pq_waveform_raw_index(ts)`,
  `command_batch(created_at)`, `grant(created_at)` and `plan_energy_value(created_at)`.
- Index findings for the hot queries, with EXPLAIN evidence, are in §6.

## 3. Operating it

- **Schedule:** a systemd timer, `og-lifecycle.timer`, runs every 10 minutes and starts
  `og-lifecycle.service` (Type=oneshot, User=opengrid), which runs
  `python -m opengrid.lifecycle run --cycle auto`.
  - A `fast` cycle ensures partitions (swapping the default when needed), builds indexes and runs the rollups.
  - `auto` upgrades a cycle to `hourly` when the last successful hourly run started 55 minutes or more ago.
    An `hourly` cycle adds the exports, retention and the monthly billing export.
  - A PostgreSQL advisory lock allows only one runner at a time.
  - The timer is recommended over a loop inside `og-settle`: it isolates failures and I/O, runs at lower
    CPU and I/O priority, needs no settle restart to change, and exit codes feed `OnFailure`.
- **Status:** `python -m opengrid.lifecycle status [--json]` shows for each table:
  - size, estimated rows and partitions;
  - the oldest row and the next drop;
  - `/srv/pgdata` and cold-tier usage;
  - the archive total and the last runs.

  Exit codes are 0 for OK, 1 for WARN and 2 for CRIT.
- **Alert thresholds (RB-044):**

  | Condition | Level |
  |---|---|
  | `/srv/pgdata` used ≥ 80 % | WARN |
  | `/srv/pgdata` used ≥ 90 % | CRIT |
  | cold tier free < `cold_min_free_gb` (exports, and therefore drops, stop) | CRIT |
  | last hourly run failed, or older than 2 h | WARN |

  These follow ALR-162's 80 % and 95 % levels, but CRIT is tighter because PostgreSQL must never fill its
  volume.
- **Restore for audits:** `python -m opengrid.lifecycle restore --archive telemetry --day 2026-09-20`
  1. It verifies the file's checksum.
  2. It loads the file into `og_restore.telemetry_20260920` (`LIKE og.telemetry`).
  3. It checks the row count against the manifest.

  Any archive name works: `telemetry`, `telemetry_1m`, `telemetry_15m`, `command_batch`, `verdict`,
  `command_ack`, `grant`, `pq_waveform_summary`, `feed_obs`, `billing_invoice_line`, `billing_pnl`. For
  billing, `--day` is the first day of the month. Drop the scratch table when the audit is done.
- **Legal hold:** `UPDATE og.data_retention SET legal_hold = true WHERE table_name = '…'` takes effect on the
  next hourly run.
- **Change retention:** update `keep_days` or `hot_days` the same way. Raising `keep_days` never needs data
  back. Lowering it drops more data on the next hourly run, after export.

`[lifecycle]` in `orchestrator.toml` (every key optional, defaults shown):

```toml
[lifecycle]
cold_root = "/srv/ogbackup/cold"   # opengrid:opengrid 0750
days_ahead = 7
days_back = 1
batch_rows = 10000                 # hard cap 10,000
lock_timeout_s = 5
rollup_backfill_hours = 24
rollup_late_minutes = 15
rollup_chunk_minutes = 15
rollup_max_hours_per_run = 6
pgdata_path = "/srv/pgdata"
disk_warn_pct = 80
disk_crit_pct = 90
cold_min_free_gb = 5
max_preexport_days_per_run = 1     # hot->cold pre-exports per table per hourly run (disk I/O bound)
# pq_blob_dir defaults to [pq_ingest].blob_store_dir
```

## 4. Differences from the production design (06 §4.4; deferred)

| Production (06) | Node, as built |
|---|---|
| TimescaleDB hypertables with 2 h uncompressed and 30 d compressed (10× ratio) | Native daily partitions, no compression in PostgreSQL. Raw is kept 7 d uncompressed, so the node budget is about 10× bigger per raw day. |
| Continuous aggregates for 1 min and 15 min | Watermarked UPSERT rollups (`rollup.py`). Same buckets, recomputed on late data. |
| Cold tier: Parquet in object storage, WORM, 7 years (raw ≥ 13 months) | CSV + gzip (zstd when installed) on the local `/srv/ogbackup` LV. Files are 0440 and write-once by convention, with no object lock, and the node has no 7-year horizon. |
| Monthly signed Parquet billing export to write-once storage | Monthly CSV and manifest "signed by checksum" on the same LV. There is no cryptographic signature and no WORM bucket yet. |
| Audit writer with INSERT only; no role deletes | Unchanged: trace pruning is still `opengrid.trace` (checkpoint-aware). This job never touches `og.trace`. |
| 13 months of 1-min M&V | 60 days (the node-lifetime cap), plus the cold copy |

Moving to production means replacing `partitions.py` and `rollup.py` with hypertables, compression and
continuous-aggregate policies, and replacing `archive.py`'s file sink with an object-storage writer. The
policy table, the whitelist and the manifest format carry over.

## 5. Capacity math

(M) means measured and (C) means computed from (M) or from the schema.

**Row size.**
- **Raw telemetry: ≈ 185 B/row including the primary-key index (M).** This was measured in the dlc
  workspace on 2026-09-26 with 2,000 hubs and about 1.5 M rows in one day partition (278 MB). It matches the
  schema estimate of about 115 B of heap plus about 60 B of randomly filled PK b-tree.
- 1-min and 15-min rollup rows: ≈ 130 B (C).
- Compressed cold CSV: ≈ 17 B/row of raw telemetry (C: about 100 B of CSV text at gzip ≈ 6×). The first
  archive runs will measure this.

**Raw telemetry per day.** 06 §4.1's normal cadence is 10 s. The live simulator currently sends every 2 s
(`[fleet].telemetry_interval_s = 2`).

| Load | Rows/day | PostgreSQL/day | Kept (7 d policy = up to 8 days on disk) | Cold/day |
|---|---|---|---|---|
| 2,000 hubs @ 10 s | 17.3 M | 3.2 GB | **26 GB** | 0.3 GB |
| 2,000 hubs @ 2 s | 86.4 M | 16 GB | **128 GB** ⚠ | 1.5 GB |
| 10,000 hubs @ 10 s | 86.4 M | 16 GB | **128 GB** ⚠ | 1.5 GB |
| 10,000 hubs @ 2 s | 432 M | 80 GB | does not fit | 7.3 GB |

**Rollups and other tables (C).**

| Table | 2,000 hubs | 10,000 hubs |
|---|---|---|
| `telemetry_1m`, 60 d | 2.9 M rows/day → 0.37 GB/day → **23 GB** | 14.4 M/day → 1.9 GB/day → **114 GB** ⚠ |
| `telemetry_15m`, kept | 25 MB/day → 9 GB/year | 125 MB/day → 46 GB/year |
| `grant` (2 s allocator cycle, one row per bank-obligation per cycle), 60 d | 40 banks: 1.7 M/day ≈ 0.4 GB/day → **≈ 24 GB** | 200 banks: ≈ 120 GB ⚠ |
| `command_batch` + `verdict`, 60 d | ≈ 0.35 GB/day → ≈ 21 GB | ≈ 105 GB ⚠ |
| `feed_obs` (2 s SCADA, 2 signals per bank), 30 d | 3.5 M/day ≈ 0.4 GB/day → 12 GB | 60 GB |

**Budget.** `/srv/pgdata` is 147 GB, with 14 GB in use on 2026-09-26. WARN is at 80 % (118 GB) and CRIT at
90 % (132 GB).

- **2,000 hubs @ 10 s** with the policy as seeded comes to about 26 + 23 + 24 + 21 + 12 ≈ **106 GB** plus
  trace (≈ 0.1 GB/day) and WAL. That fits under WARN, but only just.
- **2,000 hubs @ 2 s** (today's simulator) comes to about **210 GB** and does not fit. Until the cadence
  returns to 10 s, set:

  ```sql
  UPDATE og.data_retention SET keep_days = 3  WHERE table_name = 'telemetry';      -- 4 days x 16 GB = 64 GB
  UPDATE og.data_retention SET keep_days = 14 WHERE table_name IN ('grant', 'command_batch', 'command_ack');
  ```

  That brings it to about 64 + 23 + 6 + 5 + 12 ≈ **110 GB**.
- **10,000 hubs** (the test preset) needs, even at 10 s: telemetry 3 d, `telemetry_1m` 14 d, and `grant` /
  `command_batch` 7 d, which is ≈ 64 + 28 + 14 + 12 + 12 ≈ **130 GB**. Run it only for short windows and watch
  `status`.
- **The cold tier** (`/srv/ogbackup`, 56 GB free) holds about 37 days of 2 s raw telemetry at 2,000 hubs, or
  about 6 months at 10 s. It has no pruning of its own (§7).
- **This PostgreSQL has no compression.** Timescale's 10× compression (06 §4.3) is what makes 7 days of raw
  data cheap in the production design. The node gets the same headroom only by keeping less raw data and
  relying on the 1-min rollup.

**I/O.** The host's disk is the binding resource: 2026-09-26 17:10 CT showed pressure at ≈ 73 % "full". A
pre-export reads a whole raw day (16 GB at 2 s) and writes it compressed to the same disk. The job therefore
does at most `[lifecycle].max_preexport_days_per_run` pre-exports (default 1) per table per hourly run. On a
saturated disk, set `archive = false` for `telemetry`. The 1-min rollup and its cold copy remain the M&V
record.

## 6. Hot-query index check (EXPLAIN evidence)

The EXPLAIN (ANALYZE) run at realistic volume was **aborted on 2026-09-26 at 17:18 CT at the lead's request**,
before any plans were printed. Seeding a synthetic day into the dlc workspace database, which shares the
production cluster and disk, saturated the disk. The workspace database was dropped. Do not repeat it on the
base host. `tests/integration/lifecycle/explain_hot_queries.py` holds the queries. Run it only on a separate
host, or with `OG_DLC_SEED_HOURS` of at most 0.05 (about 50k rows), and read estimated plans.

The findings below come from the index definitions and the query shapes (static analysis):

| Hot query (owner) | Index used | Verdict |
|---|---|---|
| Fleet `hub_state` upsert, 500 rows per statement every 2 s (fleet) | `hub_state_pkey` | OK. 0033 adds `fillfactor 70` and fixed-count autovacuum so the updates stay HOT. |
| Guardian G-03, latest GOOD SCADA reading per bank (guardian) | `feed_obs_pkey (source, product, series, ts)`, backward scan with a `LIMIT 1` | OK |
| Guardian prior grant, `WHERE obligation_id = ? ORDER BY created_at DESC LIMIT 1` (guardian/ledger) | `ix_grant_obligation (obligation_id)`, then a sort of **every** grant of the obligation (≈ 43k rows/day at the 2 s cycle, up to 2.6 M at 60 d) | **Missing: `og.grant (obligation_id, created_at DESC)`** |
| Settle `cyc` CTE, `grant WHERE bank_id IN (…) AND created_at` range; api `grant WHERE bank_id = ? ORDER BY created_at DESC LIMIT 200` (settle, api) | No `bank_id` index. With 0033's BRIN on `created_at`, a bitmap scan of the time range plus a filter; the api query sorts the whole table. | **Missing: `og.grant (bank_id, created_at)`** |
| Settle `tel` CTE and zone charge: telemetry of a bank's or zone's hubs over a ts range (settle) | `telemetry_pYYYYMMDD_pkey (hub_id, ts)` per hub, and pruning to the day partitions | OK (partitioning now prunes) |
| API hub sparkline (api) | PK `(hub_id, ts)` | OK |
| Fleet map (`views_ext._FLEET_MAP_SQL`) (api) | `ix_grant_cycle` for `max(cycle_id)` and the join; `hub` and `hub_state` have 2k rows | OK |
| Health `SELECT MAX(ts) FROM og.feed_obs WHERE source = 'scada'` (health) | PK prefix `source` only: reads **every** SCADA entry (3.5 M/day, ≈ 100 M at 30 d) | **Missing: `og.feed_obs (source, ts DESC)`**, or rewrite it per series to use `ix_feed_obs_series_ts` |
| Engine/api SCADA `series = … AND ts >= now() − 1 h` | `ix_feed_obs_series_ts` | OK |
| Lifecycle `min(ts) WHERE ts < cutoff`, one-day export and delete batches | BRIN on the time column (0033 / `indexes.py`) | OK |

## 7. Known limits and follow-ups

- `pq_waveform_summary` and `feed_obs` stay unpartitioned and use batched deletes, which is enough at the
  node's volume. Convert them with a rename and swap in a quiet window if their size warrants it.
- A late row that arrives in a DELETE-mode day after that day was exported and deleted is exported as a new
  revision. The registry then points at the latest revision, and the older file and its manifest stay on
  disk.
- The cold tier has no pruning of its own. At 2 s telemetry it grows by the compressed raw volume per day
  (§5), so watch `status` and size `/srv/ogbackup`.
- Settle still meters from raw `og.telemetry`. Past 7 days, M&V must read `og.telemetry_1m`, which is a
  request to the SETTLE owner.
