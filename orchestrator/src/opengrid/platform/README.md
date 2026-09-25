# opengrid.platform

Cross-cutting I/O adapters shared by every process (02b S1.1): config loading, the Postgres pool and
migration runner, the MQTT client factory with schema validation, the Prometheus metrics catalogue,
JSON logging, the heartbeat writer, and the common async main-loop with graceful shutdown.

Depends only on `opengrid.core` (never the reverse -- BUILD.md S5a dependency direction: `core` <-
`platform` <- modules <- entry points).

## Modules

- `config.py` -- loads `orchestrator.toml` via `OG_CONFIG`; `OG_DB`/`OG_MQTT_ROOT` env vars override
  `postgres.database`/`mqtt.topic_root` for per-workspace isolation (BUILD.md S5).
- `db.py` -- `psycopg_pool.AsyncConnectionPool` factory + a forward-only `.sql` migration runner
  (`python -m opengrid.platform.db migrate`).
- `mqtt.py` -- `aiomqtt` client factory (topic-root prefixing) and JSON-Schema validation against
  `interfaces/mqtt/*.schema.json`.
- `metrics.py` -- the `prometheus_client` metric objects from 02b S6.6, one definition per name.
- `log.py` -- JSON-lines structured logging (`configure_logging`).
- `heartbeat.py` -- `write_heartbeat(pool, process)`, called by every process on its own cadence.
- `process.py` -- `run_forever(tick, interval_s, process_name)`: signal-aware loop every process uses.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\platform -q
```
