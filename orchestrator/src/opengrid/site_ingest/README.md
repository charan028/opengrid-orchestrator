# opengrid.site_ingest

Customer-side closed-loop signals for tailored services (06-service-profiles-and-power-quality.md S4.a
PIPELINE_AC, S4.b DATA_CENTER, S1.3 `feedback_signal_ref`).

## Purpose

- Validates `<root>/site/<customer_id>/<site_id>/meter` (`interfaces/mqtt/customer_site_meter.schema.json`)
  and `<root>/corridor/<customer_id>/<corridor_id>/current`
  (`interfaces/mqtt/pipeline_corridor_current.schema.json`) payloads. The payload's customer and source ids
  must match the topic's, and a timestamp more than 5 s ahead of the receiver's clock is refused.
- Keeps the newest reading per (customer, source) in memory. Freshness is measured from the reading's `ts`.
- Buffers readings and writes them in one batched, asynchronously committed statement per table on the
  caller's flush timer (`og.customer_site_meter_reading`, `og.corridor_current_reading`, migration 0026).
  Nothing is written per message.
- Resolves `feedback_signal_ref` (`site_meter:<site_id>:<field>`, `corridor:<corridor_id>:<field>`) for
  the obligation's customer to a `FeedbackValue`: `usable` means GOOD and no older than the profile's
  freshness gate.

## Interface

```python
configure(backend, *, buffer_max=10_000, max_future_skew_s=5.0) -> None
ingest_site_meter(payload, *, topic, root, now=None) -> bool          # raises SiteIngestRejectedError
ingest_corridor_current(payload, *, topic, root, now=None) -> bool    # raises SiteIngestRejectedError
async flush_readings() -> int
resolve_feedback_signal(ref, *, customer_id, max_age_s, now=None) -> FeedbackValue | None
latest_site_meter(customer_id, site_id) / latest_corridor_current(customer_id, corridor_id)
counters() -> dict[str, int]
```

## Engine wiring (for the live-path agent; `engine/__init__.py` is not edited here)

Behind `[site_ingest].enabled` (default false). In `main()`, after `pq_mod.configure(...)`:

```python
import opengrid.site_ingest as site_ingest_mod
from opengrid.site_ingest.pg_backend import PgSiteIngestBackend

site_ingest_on = bool(cfg.get("site_ingest.enabled", False))
if site_ingest_on:
    site_ingest_mod.configure(
        PgSiteIngestBackend(pool),
        buffer_max=int(cfg.get("site_ingest.buffer_max", site_ingest_mod.DEFAULT_BUFFER_MAX)),
    )
```

Next to the other periodic tasks (and cancel it in the `finally`):

```python
site_flush_task = (
    asyncio.create_task(
        run_periodic(
            "site-ingest-flush",
            float(cfg.get("site_ingest.flush_interval_s", site_ingest_mod.DEFAULT_FLUSH_INTERVAL_S)),
            site_ingest_mod.flush_readings,
        )
    )
    if site_ingest_on
    else None
)
```

In `_mqtt_ingest_loop` (pass `site_ingest_on` in): subscribe, and add these branches **before** the
generic ones (neither topic overlaps `tel/`, `ack/` or `scada/`):

```python
site_topic = topic(cfg, "site/+/+/meter")
corridor_topic = topic(cfg, "corridor/+/+/current")
if site_ingest_on:
    await client.subscribe(site_topic)
    await client.subscribe(corridor_topic)
...
elif site_ingest_on and message.topic.matches(site_topic):
    site_ingest.ingest_site_meter(payload, topic=msg_topic, root=cfg.mqtt_topic_root)
elif site_ingest_on and message.topic.matches(corridor_topic):
    site_ingest.ingest_corridor_current(payload, topic=msg_topic, root=cfg.mqtt_topic_root)
```

and catch `site_ingest.SiteIngestRejectedError` next to `SchemaValidationError` (log and drop). Once the
architect adds the kinds `"customer_site_meter"` / `"pipeline_corridor_current"` to
`opengrid.platform.mqtt._SCHEMA_BY_KIND`, also call `validate_payload(kind, payload)` first.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\site_ingest -q
```
