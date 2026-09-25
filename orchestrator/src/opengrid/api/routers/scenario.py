"""Scenario panel (02b S5.5, S7.1, S8): triggers a scenario-injector event on `sim` over MQTT.

`api` is the only process holding the `og_api` MQTT credentials that may publish to
`og/v1/scenario/cmd` (02b S6.1-S6.2); this router is a thin, validated bridge from an operator click to
that topic -- it does not implement any scenario logic itself (that lives in `sim`, owned by the sims
agent).
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import uuid4

from fastapi import APIRouter, Depends

from opengrid.api.auth import Identity, require_operator
from opengrid.api.deps import get_config, get_store, get_trace_store
from opengrid.api.schemas import ScenarioTriggerRequest
from opengrid.api.store import StoreProtocol
from opengrid.core.models.mqtt import ScenarioControl, ScenarioTarget
from opengrid.platform.config import Config
from opengrid.platform.mqtt import build_client, topic, validate_payload
from opengrid.trace.store import TraceStore

router = APIRouter(prefix="/og/api/scenario", tags=["scenario"])

_MQTT_API_PASSWORD_ENV = "OG_MQTT_API_PASSWORD"  # noqa: S105 -- an env-var *name*, never a secret value


@router.post("/{name}")
async def trigger_scenario(
    name: str,
    body: ScenarioTriggerRequest,
    cfg: Annotated[Config, Depends(get_config)],
    store: Annotated[StoreProtocol, Depends(get_store)],
    trace_store: Annotated[TraceStore, Depends(get_trace_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Publishes `{id, target, type, params, start, duration}` to `og/v1/scenario/cmd` (02b S3, S6.2)."""
    control = ScenarioControl(
        id=uuid4(),
        target=ScenarioTarget(kind=body.target_kind, ref=body.target_ref),
        type=name,
        params=body.params,
        start=datetime.now(UTC),
        duration_s=body.duration_s,
    )
    payload = control.model_dump(mode="json")
    validate_payload("scenario_control", payload)

    password = os.environ.get(_MQTT_API_PASSWORD_ENV, "")
    async with build_client(cfg, username="og_api", password=password, client_id="og-api-scenario") as client:
        await client.publish(topic(cfg, "scenario/cmd"), payload=_json_bytes(payload), qos=1)

    trace_ref = await trace_store.append(
        stream_id=f"operator_action:{identity.user}",
        decision_type="OPERATOR_ACTION",
        event_class="SCENARIO",
        payload={"decision_ref": str(control.id), "scenario": name, **payload},
    )
    await store.insert_operator_action(
        operator_ref=identity.user,
        action_kind="MANUAL_COMMAND",
        target_ref=f"scenario:{name}",
        tier=None,
        reason=f"scenario {name} triggered",
        trace_id=trace_ref.trace_id,
        confirmed_at=None,
    )
    return {"scenario_id": str(control.id), "type": name, "trace_id": str(trace_ref.trace_id)}


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload).encode("utf-8")
