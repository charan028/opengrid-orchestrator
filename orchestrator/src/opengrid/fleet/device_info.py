"""Battery device-info intake (OWNER DECISION 2026-09-26): each hub publishes a retained `device_info`
message on connect (`interfaces/mqtt/device_info.schema.json`, topic `<root>/hub/<hub_id>/info`). This module
is the one writer of what it reports into `og.hub` (migration 0036's identity columns, `device_info_at`).

Identity fields (serial, manufacturer, model, firmware, hardware revision, inverter model, install and
commissioning dates) are simply recorded. The RATINGS the fleet plans with -- unit count, rated kW/kWh,
reserve floor, location -- are changed only when the device reports something different from what is on
record (the seed), and every such change is traced first (K10) as `FLEET_CHANGE`, old -> new, so a
device can never silently re-rate itself. The engine's in-memory fleet twin keeps the ratings it loaded;
the caller (og-engine's MQTT ingest) reloads the hub when `DeviceInfoResult.rating_changes` is non-empty.

Validation uses the interface schema (compiled once). An unknown hub is reported, never created.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from functools import cache
from typing import Any

import jsonschema
from psycopg_pool import AsyncConnectionPool

from opengrid.platform.mqtt import INTERFACES_MQTT_DIR, SchemaValidationError
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

SCHEMA_FILE = "device_info.schema.json"
TRACE_STREAM = "fleet_device_info"
#: `og.trace.decision_type` has no FLEET_CHANGE value (0001/0011 CHECK); a re-rating is an asset state
#: transition, recorded under that type with `event_class = FLEET_CHANGE`.
TRACE_DECISION_TYPE = "ASSET_STATE_TRANSITION"
TRACE_EVENT_CLASS = "FLEET_CHANGE"
_TOLERANCE = 1e-6


@cache
def _validator() -> jsonschema.protocols.Validator:
    """Compiled once (as `opengrid.platform.mqtt._validator_for` does): device info arrives per connect,
    not per tick, but there is no reason to re-check the schema itself each time. Switch to
    `platform.mqtt.validate_payload("device_info", ...)` once platform registers that kind."""
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


@dataclass(frozen=True, slots=True)
class DeviceInfoResult:
    hub_id: str
    found: bool
    #: `{field: {"old": ..., "new": ...}}` for every planning rating the device changed (empty: none).
    rating_changes: dict[str, dict[str, Any]] = field(default_factory=dict)


def rating_changes(current: dict[str, Any], msg: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """The ratings the device reports that differ from `current` (an `og.hub` row): `units`, `p_kw`
    (rated_kw), `e_kwh` (rated_kwh), `r_kwh` (reserve_floor_pct of rated_kwh), `lat`, `lon`. Pure."""
    reported: dict[str, Any] = {
        "units": int(msg["units"]),
        "p_kw": float(msg["rated_kw"]),
        "e_kwh": float(msg["rated_kwh"]),
        "r_kwh": float(msg["rated_kwh"]) * float(msg["reserve_floor_pct"]) / 100.0,
        "lat": float(msg["lat"]),
        "lon": float(msg["lon"]),
    }
    changes: dict[str, dict[str, Any]] = {}
    for column, new in reported.items():
        old = current.get(column)
        if old is None:
            differs = True
        elif isinstance(new, int) and not isinstance(new, bool) and column == "units":
            differs = int(old) != new
        else:
            differs = abs(float(old) - float(new)) > _TOLERANCE
        if differs:
            changes[column] = {"old": old, "new": new}
    return changes


_SELECT_HUB_SQL = """
    SELECT units, p_kw, e_kwh, r_kwh, lat, lon FROM og.hub WHERE hub_id = %(hub_id)s FOR UPDATE
"""

_UPDATE_IDENTITY_SQL = """
    UPDATE og.hub SET
        serial_number = %(serial_number)s, manufacturer = %(manufacturer)s, model = %(model)s,
        firmware_version = %(firmware_version)s, hardware_revision = %(hardware_revision)s,
        inverter_model = %(inverter_model)s, installed_at = %(installed_at)s,
        commissioned_at = %(commissioned_at)s, device_info_at = %(device_info_at)s
    WHERE hub_id = %(hub_id)s
"""

#: Only these columns may be re-rated, each by its own fixed statement (never a caller-built column list).
_RATING_UPDATE_SQL = {
    column: f"UPDATE og.hub SET {column} = %(value)s WHERE hub_id = %(hub_id)s"  # noqa: S608 -- fixed names
    for column in ("units", "p_kw", "e_kwh", "r_kwh", "lat", "lon")
}


async def upsert_device_info(
    pool: AsyncConnectionPool, msg: dict[str, Any], *, trace: TraceStore | None = None
) -> DeviceInfoResult:
    """Validate `msg` and record it on its hub (see the module docstring). Returns what changed; a hub
    that is not in `og.hub` is logged and returned with `found=False` (nothing is written)."""
    validate_device_info(msg)
    hub_id = str(msg["hub_id"])
    async with pool.connection() as conn:
        async with conn.cursor() as cur:
            await cur.execute(_SELECT_HUB_SQL, {"hub_id": hub_id})
            row = await cur.fetchone()
            if row is None:
                await conn.rollback()
                logger.warning("device_info for an unknown hub ignored", extra={"hub_id": hub_id})
                return DeviceInfoResult(hub_id=hub_id, found=False)
            current = dict(zip(("units", "p_kw", "e_kwh", "r_kwh", "lat", "lon"), row, strict=True))
            changes = rating_changes(current, msg)
            if changes and trace is not None:
                await trace.append(
                    TRACE_STREAM,
                    TRACE_DECISION_TYPE,
                    TRACE_EVENT_CLASS,
                    {
                        "hub_id": hub_id,
                        "serial_number": msg["serial_number"],
                        "changes": {k: {"old": v["old"], "new": v["new"]} for k, v in changes.items()},
                        "reported_at": msg["ts"],
                    },
                )
            for column, change in changes.items():
                await cur.execute(_RATING_UPDATE_SQL[column], {"value": change["new"], "hub_id": hub_id})
            await cur.execute(
                _UPDATE_IDENTITY_SQL,
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
                    "device_info_at": datetime.fromisoformat(str(msg["ts"]).replace("Z", "+00:00")),
                },
            )
        await conn.commit()
    if changes:
        logger.info("hub re-rated from its device info", extra={"hub_id": hub_id, "changes": list(changes)})
    return DeviceInfoResult(hub_id=hub_id, found=True, rating_changes=changes)
