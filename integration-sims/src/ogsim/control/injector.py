"""Routes an anomaly injection/cancel to the right simulator (market's admin
HTTP API, or MQTT for fleet/SCADA), validates it against the catalogue, and
appends every injection to the JSONL log.

Every injection carries a `source`: "manual" (UI/CLI/REST), "scenario" (the
YAML scenario runner), or "random" (the autonomous random-mode engine) -
they all share one log and one active-anomaly registry.

MQTT-bound (scada/fleet) messages are built to
`interfaces/mqtt/scenario_control.schema.json`: `{id, target: {kind, ref},
type, params, start, duration_s}`, with `type` one of that schema's
SCADA_*/FLEET_* enum values and `start` an ISO-8601 date-time string -
distinct from this module's own lowercase catalogue ids and epoch-seconds
`InjectionRecord.start`. Market-owned anomalies never go over MQTT; they are
posted to ogsim.market's own admin HTTP API instead, so the schema doesn't
apply to them.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal

from ogsim.control import catalogue, market_client, mqtt_pub
from ogsim.control.log import AnomalyLog

InjectionSource = Literal["manual", "scenario", "random"]

CANCEL_DURATION_S = 0


class UnknownAnomalyTypeError(ValueError):
    """Raised when an anomaly `type` is not in `ogsim.control.catalogue`."""


@dataclass
class InjectionRecord:
    id: str
    type: str
    target: str
    params: dict[str, Any]
    start: float
    duration: float
    owner: str
    source: InjectionSource = "manual"

    @property
    def end(self) -> float:
        return self.start + self.duration

    def is_active_at(self, now: float) -> bool:
        return self.start <= now < self.end

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "target": self.target,
            "params": self.params,
            "start": self.start,
            "end": self.end,
            "duration": self.duration,
            "owner": self.owner,
            "source": self.source,
        }


def _scenario_control_message(record: InjectionRecord, duration_s: int | None) -> dict[str, Any]:
    """Builds the `<root>/scenario/cmd` payload for a scada/fleet record,
    conforming to interfaces/mqtt/scenario_control.schema.json."""
    entry = catalogue.BY_ID[record.type]
    return {
        "id": record.id,
        "target": {"kind": entry.wire_target_kind, "ref": record.target},
        "type": entry.wire_type,
        "params": record.params,
        "start": datetime.fromtimestamp(record.start, tz=UTC).isoformat(),
        "duration_s": duration_s,
    }


class Injector:
    """Shared entry point for manual (UI/CLI/REST), scenario, and random-mode
    anomaly injections. Keeps the active-anomaly registry the UI and the
    random engine's max-concurrency cap both read from."""

    def __init__(self, log: AnomalyLog | None = None):
        self.log = log or AnomalyLog()
        self._active: dict[str, InjectionRecord] = {}

    def active(
        self, source: InjectionSource | None = None, now: float | None = None
    ) -> list[InjectionRecord]:
        now = time.time() if now is None else now
        records: Iterable[InjectionRecord] = self._active.values()
        if source is not None:
            records = (r for r in records if r.source == source)
        return [r for r in records if r.is_active_at(now)]

    async def inject(
        self,
        type_: str,
        target: str,
        params: dict[str, Any] | None = None,
        duration: float = 60.0,
        start: float | None = None,
        anomaly_id: str | None = None,
        source: InjectionSource = "manual",
    ) -> InjectionRecord:
        owner = catalogue.owner_of(type_)
        if owner is None:
            raise UnknownAnomalyTypeError(f"unknown anomaly type '{type_}'")
        record = InjectionRecord(
            id=anomaly_id or str(uuid.uuid4()),
            type=type_,
            target=target,
            params=params or {},
            start=start if start is not None else time.time(),
            duration=duration,
            owner=owner,
            source=source,
        )
        await self._dispatch(record)
        self._active[record.id] = record
        self.log.append({"action": "inject", **record.to_dict()})
        return record

    async def _dispatch(self, record: InjectionRecord) -> None:
        if record.owner == "market":
            await market_client.inject(
                {
                    "id": record.id,
                    "type": record.type,
                    "target": record.target,
                    "params": record.params,
                    "start": record.start,
                    "duration": record.duration,
                }
            )
        else:
            await mqtt_pub.publish_scenario_cmd(_scenario_control_message(record, int(record.duration)))

    async def cancel(self, anomaly_id: str) -> bool:
        record = self._active.pop(anomaly_id, None)
        found = record is not None
        if record is not None:
            if record.owner == "market":
                with contextlib.suppress(Exception):
                    await market_client.cancel(anomaly_id)
            else:
                # No cancel verb in the wire schema: republish the same
                # command with duration_s=0, which the sim treats as an
                # already-expired window and stops applying immediately.
                await mqtt_pub.publish_scenario_cmd(_scenario_control_message(record, CANCEL_DURATION_S))
        self.log.append({"action": "cancel", "id": anomaly_id, "found": found})
        return found

    def injection_log(self) -> list[dict[str, Any]]:
        return self.log.read_all()
