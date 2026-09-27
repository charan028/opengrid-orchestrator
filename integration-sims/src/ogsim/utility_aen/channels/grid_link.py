"""ogsim.utility_aen.channels.grid_link -- the Austin Energy EMS delivering toll calls over the utility
grid-control link: DNP3 (IEEE 1815) over mutual TLS to OpenGrid's outstation, point list
"opengrid-gridlink-v1" (grid-link.md S4). ogsim's own DNP3 master; no opengrid import.

Settings (`channels.grid_link` in utility_aen.yaml, passed through untouched):

    host, port, master_address (1), outstation_address (10), heartbeat_s (5), sbo (true),
    tls {ca_file, cert_file, key_file, server_hostname} (optional), timeout_s (5), status_wait_s (5),
    utility_id / env_code (added by the runtime; informational)

Mapping onto the link:

- `issue_call`: stage |kW|, duration and the EMS call id on AO 0-2, then CALL_EXECUTE (CROB 0, select-before-
  operate unless `sbo: false`), then poll until CALL_STATE reports this call id. The EMS call id is
  `call_ref` itself when it is a positive 31-bit integer, else a stable CRC-32 of it. A call starts at
  receipt: the link has no scheduled start, so the runtime issues a call when it wants discharge.
- A positive `kw` (a charge call) is sent as-is, so the outstation's range check refuses it
  (R-CALL-CHARGE-REFUSED): the link carries discharge only.
- `cancel`: CALL_ID then CALL_CANCEL. Shortening to a later `end_at` is not a link operation: refused
  locally with R-GL-SHORTEN-UNSUPPORTED.
- `status`: one integrity poll. The link reports only the utility's latest call; any other call reads
  UNKNOWN.
- `granted_kw` (the base type's field name): CALL_DELIVERED_KW, the call's MEASURED delivery (D-38), when the
  outstation marks it good, signed (- discharge); None while unmeasured or stale (COMM_LOST).

The EMS heartbeat (CROB 2) runs as a background task from the first use until `aclose()`: without it the
link refuses new calls (fail safe on the OpenGrid side).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import zlib
from datetime import UTC, datetime
from typing import Any

from ogsim.protocols.dnp3_master import Dnp3Master, Dnp3MasterError, PointValues, client_ssl_context
from ogsim.protocols.gridlink_points import (
    AI,
    AO,
    BO,
    CALL_REASONS,
    CALL_STATES,
    CROB_LATCH_ON,
    CROB_PULSE_ON,
    FLAG_ONLINE,
    STATUS,
)
from ogsim.utility_aen.channels.base import CallResult, CallSpec, ChannelError, ChannelSettings

logger = logging.getLogger(__name__)

__all__ = ["NAME", "GridLinkChannel", "build", "ems_call_id"]

NAME = "grid_link"
_MAX_CALL_ID = 2**31 - 1
_POLL_INTERVAL_S = 0.25
_STATE_NAMES = {
    "IDLE": "UNKNOWN",
    "ACCEPTED": "ACCEPTED",
    "ACTIVE": "ACTIVE",
    "ENDED": "COMPLETED",
    "REJECTED": "REFUSED",
}
_STATUS_NAMES = {code: name for name, code in STATUS.items()}
#: Control status on a staged value -> the reason the orchestrator would give for the same call.
_STAGE_REASONS = {
    ("CALL_SETPOINT_KW", "OUT_OF_RANGE"): "R-CALL-CHARGE-REFUSED",
    ("CALL_DURATION_MIN", "OUT_OF_RANGE"): "R-CALL-DURATION-CAP",
}


def ems_call_id(call_ref: str) -> int:
    """The link's numeric call id for `call_ref` (module docstring). Deterministic across restarts."""
    if call_ref.isdigit() and 0 < int(call_ref) <= _MAX_CALL_ID:
        return int(call_ref)
    return (zlib.crc32(call_ref.encode()) & _MAX_CALL_ID) or 1


class GridLinkChannel:
    """`Channel` over the grid link (module docstring)."""

    name = NAME

    def __init__(self, settings: ChannelSettings, master: Dnp3Master | None = None) -> None:
        self.settings = dict(settings)
        self._master = master or _master_from(self.settings)
        self._sbo = bool(self.settings.get("sbo", True))
        self._heartbeat_s = float(self.settings.get("heartbeat_s", 5.0))
        self._status_wait_s = float(self.settings.get("status_wait_s", 5.0))
        self._heartbeat: asyncio.Task[None] | None = None

    # -- Channel -------------------------------------------------------------------------------------------

    async def issue_call(self, spec: CallSpec) -> CallResult:
        call_id = ems_call_id(spec.call_ref)
        await self._ready()
        staged = (
            ("CALL_SETPOINT_KW", round(-spec.kw)),
            ("CALL_DURATION_MIN", spec.duration_min),
            ("CALL_ID", call_id),
        )
        for role, value in staged:
            status = await self._guard(self._master.analog_output(AO[role], int(value)))
            if status != STATUS["SUCCESS"]:
                return _refused(spec.call_ref, _STAGE_REASONS.get((role, _status_name(status))), role, status)
        status = await self._guard(self._master.crob(BO["CALL_EXECUTE"], CROB_LATCH_ON, sbo=self._sbo))
        if status != STATUS["SUCCESS"]:
            reason = "R-GL-LINK-DOWN" if _status_name(status) == "AUTOMATION_INHIBIT" else None
            return _refused(spec.call_ref, reason, "CALL_EXECUTE", status)
        return await self._await_state(spec.call_ref, call_id)

    async def cancel(self, call_ref: str, *, end_at: datetime | None = None) -> CallResult:
        if end_at is not None and end_at > datetime.now(UTC):
            return CallResult(
                call_ref,
                False,
                "REFUSED",
                "R-GL-SHORTEN-UNSUPPORTED",
                "the grid link can only end a call now",
            )
        await self._ready()
        call_id = ems_call_id(call_ref)
        await self._guard(self._master.analog_output(AO["CALL_ID"], call_id))
        status = await self._guard(self._master.crob(BO["CALL_CANCEL"], CROB_PULSE_ON, sbo=self._sbo))
        if status != STATUS["SUCCESS"]:
            return _refused(call_ref, None, "CALL_CANCEL", status)
        return await self._await_state(call_ref, call_id, until=("COMPLETED", "REFUSED"))

    async def status(self, call_ref: str) -> CallResult:
        await self._ready()
        return _result(call_ref, ems_call_id(call_ref), await self._guard(self._master.integrity_poll()))

    async def aclose(self) -> None:
        task, self._heartbeat = self._heartbeat, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await self._master.close()

    # -- internals -----------------------------------------------------------------------------------------

    async def _guard(self, operation: Any) -> Any:
        try:
            return await operation
        except Dnp3MasterError as exc:
            await self._master.close()
            raise ChannelError(f"grid link transport failed: {exc}") from exc

    async def _ready(self) -> None:
        """Connected, heartbeat sent at least once, heartbeat task running."""
        if not self._master.connected:
            await self._guard(self._master.connect())
            await self._guard(self._master.crob(BO["HEARTBEAT"], CROB_PULSE_ON))
        if self._heartbeat is None or self._heartbeat.done():
            self._heartbeat = asyncio.create_task(self._beat())

    async def _beat(self) -> None:
        while True:
            await asyncio.sleep(self._heartbeat_s)
            try:
                if self._master.connected:
                    await self._master.crob(BO["HEARTBEAT"], CROB_PULSE_ON)
            except Dnp3MasterError as exc:
                logger.warning("grid link heartbeat failed: %s", exc)
                await self._master.close()

    async def _await_state(
        self,
        call_ref: str,
        call_id: int,
        *,
        until: tuple[str, ...] = ("ACCEPTED", "ACTIVE", "COMPLETED", "REFUSED"),
    ) -> CallResult:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._status_wait_s
        result = CallResult(call_ref, False, "UNKNOWN", detail="no call state reported yet")
        while True:
            result = _result(call_ref, call_id, await self._guard(self._master.integrity_poll()))
            if result.state in until or loop.time() >= deadline:
                return result
            await asyncio.sleep(_POLL_INTERVAL_S)


def _status_name(status: int) -> str:
    return _STATUS_NAMES.get(status, f"STATUS_{status}")


def _refused(call_ref: str, reason: str | None, role: str, status: int) -> CallResult:
    detail = f"{role} refused by the outstation: {_status_name(status)}"
    return CallResult(
        call_ref, False, "REFUSED", reason or f"R-GL-{_status_name(status).replace('_', '-')}", detail
    )


def _result(call_ref: str, call_id: int, values: PointValues) -> CallResult:
    """Decode the call points for `call_id`; another call on the points reads UNKNOWN."""
    if int(values.analogs.get(AI["CALL_ID"], 0)) != call_id:
        return CallResult(call_ref, False, "UNKNOWN", detail="the link reports a different call")
    state = _STATE_NAMES.get(CALL_STATES.get(int(values.analogs.get(AI["CALL_STATE"], 0)), "IDLE"), "UNKNOWN")
    reason_number = int(values.analogs.get(AI["CALL_REASON"], 0))
    granted_flags = values.analog_flags.get(AI["CALL_DELIVERED_KW"], 0)
    granted = values.analogs.get(AI["CALL_DELIVERED_KW"]) if granted_flags & FLAG_ONLINE else None
    return CallResult(
        call_ref,
        accepted=state in ("ACCEPTED", "ACTIVE", "COMPLETED"),
        state=state,
        reason_code=CALL_REASONS.get(reason_number) if reason_number else None,
        granted_kw=-granted if granted is not None else None,
    )


def _master_from(settings: ChannelSettings) -> Dnp3Master:
    tls = dict(settings.get("tls") or {})
    context = client_ssl_context(tls["ca_file"], tls["cert_file"], tls["key_file"]) if tls else None
    return Dnp3Master(
        str(settings.get("host", "127.0.0.1")),
        int(settings.get("port", 20001)),
        master_address=int(settings.get("master_address", 1)),
        outstation_address=int(settings.get("outstation_address", 10)),
        ssl_context=context,
        server_hostname=tls.get("server_hostname"),
        timeout_s=float(settings.get("timeout_s", 5.0)),
    )


def build(settings: ChannelSettings) -> GridLinkChannel:
    """Registry factory (`CHANNEL_FACTORIES["grid_link"]`)."""
    return GridLinkChannel(settings)
