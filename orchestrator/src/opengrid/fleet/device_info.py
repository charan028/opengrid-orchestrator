"""Battery device-info intake (OWNER DECISION 2026-09-26): each hub publishes a retained `device_info`
message on connect (`interfaces/mqtt/device_info.schema.json`, topic `<root>/hub/<hub_id>/info`). This module
is the one writer of what it reports into `og.hub`.

SAFETY (H4, 2026-09-26): a device report is NEVER allowed to change what the fleet plans and signs with.
The safety ratings (`units`, `p_kw`, `e_kwh`, `r_kwh` -- the K1 reserve floor) and the location
(`lat`, `lon`) stay exactly as seeded; before this fix a report of `reserve_floor_pct = 0`, or one naming
another hub, removed that hub's K1 reserve at the next guardian restart. So:

- the hub is the one in the MQTT TOPIC; a payload whose `hub_id` differs is rejected (traced, nothing written);
- the reported ratings and location go to the `device_*` columns only (migration 0043), never the seed columns;
- a reported rating or location that differs from the seed raises `ALR-DEVICE-RATING-MISMATCH` (once per hub,
  cleared when a later report matches) -- an operator decides whether the seed is wrong;
- every report is traced (K10), accepted or rejected.

Identity fields (serial, manufacturer, model, firmware, hardware revision, inverter model, install and
commissioning dates) are recorded as reported: they inform the operator, nothing plans with them.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import cache
from typing import Any

import jsonschema
from psycopg_pool import AsyncConnectionPool

from opengrid.health.model import AlertFinding
from opengrid.health.queries import clear_alert, fetch_open_alerts, raise_alert
from opengrid.platform.mqtt import INTERFACES_MQTT_DIR, SchemaValidationError
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

SCHEMA_FILE = "device_info.schema.json"
TRACE_STREAM = "fleet_device_info"
#: `og.trace.decision_type` has no FLEET_CHANGE value; device reports are asset-state events.
TRACE_DECISION_TYPE = "ASSET_STATE_TRANSITION"
EVENT_ACCEPTED = "DEVICE_INFO"
EVENT_REJECTED = "DEVICE_INFO_REJECTED"
ALR_DEVICE_RATING_MISMATCH = "ALR-DEVICE-RATING-MISMATCH"
_TOLERANCE = 1e-6
#: The seed columns a report is compared against (never written by this module).
_SEED_COLUMNS = ("units", "p_kw", "e_kwh", "r_kwh", "lat", "lon")


@cache
def _validator() -> jsonschema.protocols.Validator:
    """Compiled once. Switch to `platform.mqtt.validate_payload("device_info", ...)` once platform registers
    that kind."""
    with (INTERFACES_MQTT_DIR / SCHEMA_FILE).open(encoding="utf-8") as fh:
        schema = json.load(fh)
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema, format_checker=cls.FORMAT_CHECKER)


def validate_device_info(msg: dict[str, Any]) -> None:
    """Raise `SchemaValidationError` (the platform's inbound-validation error) if `msg` breaks the schema."""
    try:
        _validator().validate(msg)
    except jsonschema.ValidationError as exc:
        raise SchemaValidationError(f"device_info: {exc.message}") from exc


def hub_id_from_topic(topic: str) -> str | None:
    """The hub id in `<root>/hub/<hub_id>/info` (any root depth), or None for any other topic shape."""
    parts = topic.strip("/").split("/")
    if len(parts) >= 3 and parts[-1] == "info" and parts[-3] == "hub" and parts[-2]:
        return parts[-2]
    return None


@dataclass(frozen=True, slots=True)
class DeviceInfoResult:
    hub_id: str
    #: False: rejected (topic/payload hub mismatch or unknown hub) -- nothing written.
    accepted: bool
    #: `{column: {"seed": ..., "reported": ...}}` for every rating/location that differs (never applied).
    mismatches: dict[str, dict[str, Any]] = field(default_factory=dict)
    reason: str | None = None


def reported_ratings(msg: dict[str, Any]) -> dict[str, Any]:
    """The report's ratings and location in `og.hub` seed-column terms (`r_kwh` = reserve % of rated kWh)."""
    return {
        "units": int(msg["units"]),
        "p_kw": float(msg["rated_kw"]),
        "e_kwh": float(msg["rated_kwh"]),
        "r_kwh": float(msg["rated_kwh"]) * float(msg["reserve_floor_pct"]) / 100.0,
        "lat": float(msg["lat"]),
        "lon": float(msg["lon"]),
    }


def rating_mismatches(seed: dict[str, Any], msg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Which reported ratings/location differ from the seed row (`og.hub`). Pure."""
    mismatches: dict[str, dict[str, Any]] = {}
    for column, reported in reported_ratings(msg).items():
        seeded = seed.get(column)
        if column == "units":
            differs = seeded is None or int(seeded) != reported
        else:
            differs = seeded is None or abs(float(seeded) - float(reported)) > _TOLERANCE
        if differs:
            mismatches[column] = {"seed": seeded, "reported": reported}
    return mismatches


_SELECT_SEED_SQL = "SELECT units, p_kw, e_kwh, r_kwh, lat, lon FROM og.hub WHERE hub_id = %(hub_id)s"

#: Only identity columns and the `device_*` reported-value columns: NEVER units/p_kw/e_kwh/r_kwh/lat/lon.
_UPDATE_REPORTED_SQL = """
    UPDATE og.hub SET
        serial_number = %(serial_number)s, manufacturer = %(manufacturer)s, model = %(model)s,
        firmware_version = %(firmware_version)s, hardware_revision = %(hardware_revision)s,
        inverter_model = %(inverter_model)s, installed_at = %(installed_at)s,
        commissioned_at = %(commissioned_at)s,
        device_units = %(device_units)s, device_rated_kw = %(device_rated_kw)s,
        device_rated_kwh = %(device_rated_kwh)s, device_reserve_floor_pct = %(device_reserve_floor_pct)s,
        device_lat = %(device_lat)s, device_lon = %(device_lon)s,
        device_info_at = %(device_info_at)s
    WHERE hub_id = %(hub_id)s
"""


async def upsert_device_info(
    pool: AsyncConnectionPool, msg: dict[str, Any], *, topic: str, trace: TraceStore
) -> DeviceInfoResult:
    """Validate and record one device report received on `topic` (see the module docstring). Always traces;
    never changes a safety rating or the location."""
    validate_device_info(msg)
    topic_hub = hub_id_from_topic(topic)
    payload_hub = str(msg["hub_id"])
    if topic_hub is None or topic_hub != payload_hub:
        reason = "topic_hub_mismatch" if topic_hub is not None else "bad_topic"
        await _trace(trace, EVENT_REJECTED, topic_hub or payload_hub, msg, reason=reason, topic=topic)
        logger.error(
            "device_info rejected: payload hub does not match the topic",
            extra={"topic": topic, "payload_hub_id": payload_hub},
        )
        return DeviceInfoResult(hub_id=topic_hub or payload_hub, accepted=False, reason=reason)
    hub_id = topic_hub

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_SELECT_SEED_SQL, {"hub_id": hub_id})
        row = await cur.fetchone()
    if row is None:
        await _trace(trace, EVENT_REJECTED, hub_id, msg, reason="unknown_hub", topic=topic)
        logger.warning("device_info for an unknown hub ignored", extra={"hub_id": hub_id})
        return DeviceInfoResult(hub_id=hub_id, accepted=False, reason="unknown_hub")

    mismatches = rating_mismatches(dict(zip(_SEED_COLUMNS, row, strict=True)), msg)
    await _trace(trace, EVENT_ACCEPTED, hub_id, msg, mismatches=mismatches, topic=topic)
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _UPDATE_REPORTED_SQL,
            {
                "hub_id": hub_id,
                "serial_number": msg["serial_number"],
                "manufacturer": msg["manufacturer"],
                "model": msg["model"],
                "firmware_version": msg["firmware_version"],
                "hardware_revision": msg["hardware_revision"],
                "inverter_model": msg["inverter_model"],
                "installed_at": date.fromisoformat(str(msg["install_date"])),
                "commissioned_at": date.fromisoformat(str(msg["commissioning_date"])),
                "device_units": int(msg["units"]),
                "device_rated_kw": float(msg["rated_kw"]),
                "device_rated_kwh": float(msg["rated_kwh"]),
                "device_reserve_floor_pct": float(msg["reserve_floor_pct"]),
                "device_lat": float(msg["lat"]),
                "device_lon": float(msg["lon"]),
                "device_info_at": datetime.fromisoformat(str(msg["ts"]).replace("Z", "+00:00")),
            },
        )
        await conn.commit()
    await _raise_or_clear_mismatch(pool, hub_id, mismatches)
    return DeviceInfoResult(hub_id=hub_id, accepted=True, mismatches=mismatches)


async def _trace(
    trace: TraceStore,
    event_class: str,
    hub_id: str,
    msg: dict[str, Any],
    *,
    topic: str,
    reason: str | None = None,
    mismatches: dict[str, dict[str, Any]] | None = None,
) -> None:
    await trace.append(
        TRACE_STREAM,
        TRACE_DECISION_TYPE,
        event_class,
        {
            "hub_id": hub_id,
            "topic": topic,
            "payload_hub_id": msg.get("hub_id"),
            "serial_number": msg.get("serial_number"),
            "reported": reported_ratings(msg),
            "mismatches": mismatches or {},
            "reason": reason,
            "reported_at": msg.get("ts"),
        },
    )


async def _raise_or_clear_mismatch(
    pool: AsyncConnectionPool,
    hub_id: str,
    mismatches: dict[str, dict[str, Any]],
    *,
    now: datetime | None = None,
) -> None:
    """ALR-DEVICE-RATING-MISMATCH per hub: raised once while the report differs from the seed, cleared by
    the first report that matches again."""
    now = now or datetime.now(UTC)
    open_for_hub = [
        a
        for a in await fetch_open_alerts(pool)
        if a.rule == ALR_DEVICE_RATING_MISMATCH and a.scope_ref == hub_id
    ]
    if mismatches:
        if not open_for_hub:
            await raise_alert(
                pool,
                AlertFinding(
                    rule=ALR_DEVICE_RATING_MISMATCH,
                    severity="warning",
                    summary=(
                        f"Hub {hub_id} reports ratings/location that differ from its record "
                        f"({', '.join(sorted(mismatches))}); the record is kept"
                    ),
                    condition_key=f"{ALR_DEVICE_RATING_MISMATCH}:{hub_id}",
                    detail={"scope_kind": "HUB", "scope_ref": hub_id, "mismatches": mismatches},
                ),
                opened_at=now,
            )
    else:
        for alert in open_for_hub:
            if alert.id is not None:
                await clear_alert(pool, alert.id, cleared_at=now)
