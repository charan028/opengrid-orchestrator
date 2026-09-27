"""ogsim.fleet.device_info -- DeviceInfo message (R3, OWNER DECISION, 2026-09-26): each simulated
battery publishes its identity once, retained, when it connects (and again if any field changes --
today nothing at runtime mutates a hub's rating/serial/etc., so in practice this fires once per
process start; the function is pure and re-callable so a future change trigger can call it again
unchanged). Published on `<root>/hub/<hub_id>/info`, QoS 1, retained (`interfaces/mqtt/
device_info.schema.json`).

Every field is a pure function of the hub's own static build-time data (id, zone, ratings,
lat/lon) -- never of the simulated wall clock `now` passed to `tick()` -- so a device's identity
never drifts across a long-running process; only `ts` (the publish timestamp) varies per call.
"""

from __future__ import annotations

import re
import zlib
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from ogsim.common.config import FleetConfig
from ogsim.common.scenario import utc_timestamp
from ogsim.fleet.state import FleetState

MANUFACTURER = "Base Power"
#: A fixed reference date for "plausible dates over the last 24 months" -- deliberately NOT derived
#: from the simulated clock `now`, so a device's install/commissioning date never shifts as a
#: long-running process's simulated time advances (these are one-time historical facts, not live
#: state). 2026-09-26 is this build phase's own "today".
_REFERENCE_DATE = date(2026, 9, 26)
_INSTALL_WINDOW_DAYS = 730  # 24 months

_TRAILING_DIGITS = re.compile(r"(\d+)$")


def _stable_index(hub_id: str) -> int:
    """The hub's own numeric suffix (e.g. "hub-00042" -> 42, "sub-LZ_AEN-00" -> 0) when its id ends
    in digits; otherwise a stable (not `hash()`, which is randomized per-process by
    `PYTHONHASHSEED`) fallback derived from the id's CRC32, so every id still gets a deterministic
    index."""
    match = _TRAILING_DIGITS.search(hub_id)
    if match:
        return int(match.group(1))
    return zlib.crc32(hub_id.encode("utf-8")) % 100_000


def _install_dates(index: int) -> tuple[date, date]:
    """`(install_date, commissioning_date)`, both plausible dates in the last 24 months, spread
    deterministically across the fleet by `index` (commissioning a few days after install)."""
    install = (
        _REFERENCE_DATE
        - timedelta(days=_INSTALL_WINDOW_DAYS)
        + timedelta(days=index * 37 % _INSTALL_WINDOW_DAYS)
    )
    commissioning = install + timedelta(days=3 + index % 5)
    return install, commissioning


@dataclass(frozen=True)
class DeviceIdentity:
    """The subset of a hub's static data `build_device_info_message` needs -- kept separate from
    `FleetState`'s arrays so this module has one clean per-device entry point regardless of whether
    the caller is iterating home hubs (from `FleetState`, vectorized) or a single substation asset."""

    hub_id: str
    zone: str
    e_kwh: float
    p_kw: float
    lat: float
    lon: float
    asset_class: str  # "HOME_BESS" | "SUBSTATION_BESS"
    units: int  # 1 or 2 (always 1 for a substation asset)


def initial_firmware_version(hub_id: str) -> str:
    """The firmware_version a hub's device_info reports before any `FirmwareManager` update -- the
    same deterministic-by-index formula `build_device_info_message` always used. `ogsim.fleet.runtime.
    FleetEngine` seeds `FirmwareManager`'s per-hub `versions` from this (via `initial_firmware_
    versions` below), so a hub's initially-published device_info and its FirmwareManager-tracked
    version always agree -- otherwise the very first firmware command's `from_version` bookkeeping
    would disagree with what was already published."""
    return f"1.{_stable_index(hub_id) % 20}.0"


def initial_hardware_revision(hub_id: str) -> str:
    """The hardware_revision a hub's device_info reports -- fixed for the hub's lifetime (firmware
    updates change `version`, never the physical hardware revision). Same formula/consistency
    rationale as `initial_firmware_version`."""
    return f"Rev{chr(ord('A') + _stable_index(hub_id) % 4)}"


def initial_firmware_versions(state: FleetState) -> dict[str, str]:
    """`{hub_id: firmware_version}` for every hub in `state` -- seeds `FirmwareManager(versions=...)`."""
    return {hub_id: initial_firmware_version(hub_id) for hub_id in state.hub_ids}


def initial_hardware_revisions(state: FleetState) -> dict[str, str]:
    """`{hub_id: hardware_revision}` for every hub in `state` -- seeds `FirmwareManager
    (hardware_revisions=...)`."""
    return {hub_id: initial_hardware_revision(hub_id) for hub_id in state.hub_ids}


def build_device_info_message(
    identity: DeviceIdentity,
    reserve_frac_default: float,
    now: float,
    *,
    firmware_version: str | None = None,
    hardware_revision: str | None = None,
) -> dict[str, Any]:
    """Builds one `device_info.schema.json`-conformant message. Pure (no I/O); the caller publishes
    it RETAINED, QoS 1, on `<root>/hub/<hub_id>/info`.

    `firmware_version`/`hardware_revision` default to the same deterministic-by-index values this
    function always computed (`initial_firmware_version`/`initial_hardware_revision`); a caller that
    has a live `ogsim.fleet.firmware.FirmwareManager` (the runtime does, after R3.1) passes its
    current `FirmwareManager.device_info_fields(hub_id)` values instead, so a republish after an
    applied firmware update reports the version the hub is ACTUALLY running, not its build-time one.
    """
    index = _stable_index(identity.hub_id)
    install_date, commissioning_date = _install_dates(index)
    if identity.asset_class == "SUBSTATION_BESS":
        model = "Base Power Substation BESS"
        inverter_model = "Base Power Substation PCS"
    elif identity.units == 2:
        model = "Base Power Home Battery (Dual Unit)"
        inverter_model = "Base Power Inverter Gen2 (Dual)"
    else:
        model = "Base Power Home Battery"
        inverter_model = "Base Power Inverter Gen2"
    return {
        "hub_id": identity.hub_id,
        "serial_number": f"BP-{identity.zone}-{index:05d}",
        "manufacturer": MANUFACTURER,
        "model": model,
        "firmware_version": firmware_version
        if firmware_version is not None
        else initial_firmware_version(identity.hub_id),
        "hardware_revision": hardware_revision
        if hardware_revision is not None
        else initial_hardware_revision(identity.hub_id),
        "install_date": install_date.isoformat(),
        "commissioning_date": commissioning_date.isoformat(),
        "asset_class": identity.asset_class,
        "units": identity.units,
        "rated_kw": round(identity.p_kw, 3),
        "rated_kwh": round(identity.e_kwh, 3),
        "reserve_floor_pct": round(reserve_frac_default * 100.0, 3),
        "lat": round(identity.lat, 6),
        "lon": round(identity.lon, 6),
        "inverter_model": inverter_model,
        "ts": utc_timestamp(now),
    }


def fleet_device_identities(state: FleetState, config: FleetConfig) -> list[DeviceIdentity]:
    """One `DeviceIdentity` per hub in `state`, home hubs and substation assets alike -- a hub is a
    substation asset iff its id matches an entry in `config.substation_assets` (the same `bank-
    <asset_id>` id scheme `ogsim.fleet.state._substation_segment` builds, but matched here by hub_id,
    which for a substation asset IS its `asset_id` verbatim)."""
    substation_ids = {a.asset_id for a in config.substation_assets if a.enabled}
    identities = []
    for i, hub_id in enumerate(state.hub_ids):
        is_substation = hub_id in substation_ids
        units = 1 if is_substation else (2 if state.e_kwh[i] == config.e_kwh_dual_unit else 1)
        identities.append(
            DeviceIdentity(
                hub_id=hub_id,
                zone=state.zones[i],
                e_kwh=float(state.e_kwh[i]),
                p_kw=float(state.p_kw_limit[i]),
                lat=float(state.lat_deg[i]),
                lon=float(state.lon_deg[i]),
                asset_class="SUBSTATION_BESS" if is_substation else "HOME_BESS",
                units=units,
            )
        )
    return identities


def device_identity_for_hub(state: FleetState, config: FleetConfig, hub_id: str) -> DeviceIdentity | None:
    """One hub's `DeviceIdentity`, or `None` if it isn't in `state` -- used to republish a single
    hub's device_info (e.g. after a firmware update) without rebuilding the whole fleet's identities."""
    idx = state.hub_index.get(hub_id)
    if idx is None:
        return None
    return next((i for i in fleet_device_identities(state, config) if i.hub_id == hub_id), None)


def build_device_info_messages(
    state: FleetState, config: FleetConfig, now: float, *, firmware: Any = None
) -> list[tuple[str, dict[str, Any]]]:
    """`(topic_suffix, message)` pairs for every hub in `state` -- the whole fleet's DeviceInfo,
    built once (at connect) and re-callable unchanged if a future change-trigger needs to republish.

    `firmware`, if given, is an `ogsim.fleet.firmware.FirmwareManager` (duck-typed here, not imported,
    to keep this module's own dependency surface unchanged) -- its `device_info_fields(hub_id)` wins
    over the deterministic-by-index default for `firmware_version`/`hardware_revision`, so a fleet
    that has already applied updates republishes the version each hub is ACTUALLY running."""
    messages = []
    for identity in fleet_device_identities(state, config):
        fw_version = hw_revision = None
        if firmware is not None:
            fields = firmware.device_info_fields(identity.hub_id)
            fw_version, hw_revision = fields.get("firmware_version"), fields.get("hardware_revision")
        messages.append(
            (
                f"hub/{identity.hub_id}/info",
                build_device_info_message(
                    identity,
                    config.reserve_frac_default,
                    now,
                    firmware_version=fw_version,
                    hardware_revision=hw_revision,
                ),
            )
        )
    return messages
