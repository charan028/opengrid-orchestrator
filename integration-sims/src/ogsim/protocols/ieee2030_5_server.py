"""Minimal IEEE 2030.5 (SEP 2.0) server simulator for DER control (protocol-adapters.md S4).

Serves, as `application/sep+xml` in namespace `urn:ieee:std:2030.5:ns`:

    GET  /dcap                                  DeviceCapability
    GET  /edev                                  EndDeviceList
    GET  /edev/{d}/fsa                          FunctionSetAssignmentsList (one FSA per device)
    GET  /edev/{d}/derp                         DERProgramList (one program per device, primacy 1)
    GET  /edev/{d}/derp/0/derc                  DERControlList
    GET  /edev/{d}/derp/0/dderc                 DefaultDERControl
    POST /rsp                                   DERControlResponse sink (replyTo of every control)

and a JSON admin API the tests and the scenario panel drive:

    POST /admin/devices                         {"lfdi": "...", "sfdi": 123}
    POST /admin/devices/{d}/controls            {"start": unix, "duration": s, "opModMaxLimW": 5000, ...}
    POST /admin/controls/{mrid}/cancel
    PUT  /admin/devices/{d}/default             {"opModMaxLimW": 8000} (or {} to clear)
    GET  /admin/responses

Real servers enforce TLS 1.2 with ECDHE-ECDSA-AES128-CCM8 and client certificates; the simulator is
plain HTTP for in-process tests. Event status follows the clock: scheduled (0) before `start`, active (1)
inside the interval, cancelled (2) after an admin cancel.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from xml.sax.saxutils import escape

from defusedxml.ElementTree import fromstring
from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

__all__ = ["Sep2ServerState", "create_app"]

NS = "urn:ieee:std:2030.5:ns"
MEDIA = "application/sep+xml"


class ControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    start: int | None = None
    duration: int = Field(default=3600, gt=0)
    description: str = "utility DER control"
    op_mod_connect: bool | None = Field(default=None, alias="opModConnect")
    op_mod_energize: bool | None = Field(default=None, alias="opModEnergize")
    op_mod_max_lim_w: int | None = Field(default=None, alias="opModMaxLimW", ge=0, le=10000)
    op_mod_fixed_w: int | None = Field(default=None, alias="opModFixedW", ge=-10000, le=10000)
    op_mod_target_w: int | None = Field(default=None, alias="opModTargetW")  # watts, multiplier 0


class DeviceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lfdi: str
    sfdi: int | None = None


@dataclass
class _Control:
    mrid: str
    body: ControlBody
    created: int
    cancelled: bool = False


@dataclass
class _Device:
    lfdi: str
    sfdi: int | None
    controls: list[_Control] = field(default_factory=list)
    default: ControlBody | None = None


@dataclass
class Sep2ServerState:
    clock: Callable[[], float] = time.time
    devices: list[_Device] = field(default_factory=list)
    responses: list[dict[str, Any]] = field(default_factory=list)

    def add_device(self, lfdi: str, sfdi: int | None = None) -> int:
        self.devices.append(_Device(lfdi=lfdi.upper(), sfdi=sfdi))
        return len(self.devices) - 1

    def add_control(self, device: int, body: ControlBody) -> str:
        mrid = uuid.uuid4().hex.upper()
        now = int(self.clock())
        if body.start is None:
            body = body.model_copy(update={"start": now})
        self.devices[device].controls.append(_Control(mrid=mrid, body=body, created=now))
        return mrid

    def cancel(self, mrid: str) -> bool:
        for device in self.devices:
            for control in device.controls:
                if control.mrid == mrid:
                    control.cancelled = True
                    return True
        return False

    def status(self, control: _Control) -> int:
        if control.cancelled:
            return 2
        now = self.clock()
        start = control.body.start or 0
        return 1 if start <= now < start + control.body.duration else 0


def _base_xml(body: ControlBody) -> str:
    parts: list[str] = []
    if body.op_mod_connect is not None:
        parts.append(f"<opModConnect>{str(body.op_mod_connect).lower()}</opModConnect>")
    if body.op_mod_energize is not None:
        parts.append(f"<opModEnergize>{str(body.op_mod_energize).lower()}</opModEnergize>")
    if body.op_mod_fixed_w is not None:
        parts.append(f"<opModFixedW>{body.op_mod_fixed_w}</opModFixedW>")
    if body.op_mod_max_lim_w is not None:
        parts.append(f"<opModMaxLimW>{body.op_mod_max_lim_w}</opModMaxLimW>")
    if body.op_mod_target_w is not None:
        parts.append(
            f"<opModTargetW><multiplier>0</multiplier><value>{body.op_mod_target_w}</value></opModTargetW>"
        )
    return f"<DERControlBase>{''.join(parts)}</DERControlBase>"


def _xml(body: str) -> Response:
    return Response(content='<?xml version="1.0" encoding="UTF-8"?>' + body, media_type=MEDIA)


def create_app(state: Sep2ServerState | None = None) -> FastAPI:
    app = FastAPI(title="ogsim IEEE 2030.5 server")
    app.state.sep2 = state or Sep2ServerState()

    def _state(request: Request) -> Sep2ServerState:
        sep2: Sep2ServerState = request.app.state.sep2
        return sep2

    def _device(request: Request, d: int) -> _Device:
        devices = _state(request).devices
        if not 0 <= d < len(devices):
            raise HTTPException(status_code=404, detail="no such EndDevice")
        return devices[d]

    @app.get("/dcap")
    async def dcap() -> Response:
        return _xml(
            f'<DeviceCapability xmlns="{NS}" href="/dcap" pollRate="900">'
            f'<EndDeviceListLink href="/edev" all="{len(app.state.sep2.devices)}"/></DeviceCapability>'
        )

    @app.get("/edev")
    async def edev(request: Request) -> Response:
        devices = _state(request).devices
        items = "".join(
            f'<EndDevice href="/edev/{i}">'
            + (f"<sFDI>{d.sfdi}</sFDI>" if d.sfdi is not None else "")
            + f"<lFDI>{escape(d.lfdi)}</lFDI>"
            f'<FunctionSetAssignmentsListLink href="/edev/{i}/fsa" all="1"/></EndDevice>'
            for i, d in enumerate(devices)
        )
        return _xml(
            f'<EndDeviceList xmlns="{NS}" href="/edev" all="{len(devices)}" results="{len(devices)}">'
            f"{items}</EndDeviceList>"
        )

    @app.get("/edev/{d}/fsa")
    async def fsa(request: Request, d: int) -> Response:
        _device(request, d)
        return _xml(
            f'<FunctionSetAssignmentsList xmlns="{NS}" href="/edev/{d}/fsa" all="1" results="1">'
            f'<FunctionSetAssignments href="/edev/{d}/fsa/0"><mRID>{d:032X}</mRID>'
            f"<description>DER control</description>"
            f'<DERProgramListLink href="/edev/{d}/derp" all="1"/></FunctionSetAssignments>'
            f"</FunctionSetAssignmentsList>"
        )

    @app.get("/edev/{d}/derp")
    async def derp(request: Request, d: int) -> Response:
        _device(request, d)
        return _xml(
            f'<DERProgramList xmlns="{NS}" href="/edev/{d}/derp" all="1" results="1">'
            f'<DERProgram href="/edev/{d}/derp/0"><mRID>{(d + 1):032X}</mRID>'
            f"<description>Utility DER program</description>"
            f'<DefaultDERControlLink href="/edev/{d}/derp/0/dderc"/>'
            f'<DERControlListLink href="/edev/{d}/derp/0/derc" all="1"/>'
            f"<primacy>1</primacy></DERProgram></DERProgramList>"
        )

    @app.get("/edev/{d}/derp/0/derc")
    async def derc(request: Request, d: int) -> Response:
        sep2 = _state(request)
        device = _device(request, d)
        items = "".join(
            f'<DERControl href="/edev/{d}/derp/0/derc/{c.mrid}" replyTo="/rsp" responseRequired="03">'
            f"<mRID>{c.mrid}</mRID><description>{escape(c.body.description)}</description>"
            f"<creationTime>{c.created}</creationTime>"
            f"<EventStatus><currentStatus>{sep2.status(c)}</currentStatus>"
            f"<dateTime>{int(sep2.clock())}</dateTime><potentiallySuperseded>false</potentiallySuperseded>"
            f"</EventStatus><interval><duration>{c.body.duration}</duration><start>{c.body.start}</start>"
            f"</interval>{_base_xml(c.body)}</DERControl>"
            for c in device.controls
        )
        count = len(device.controls)
        return _xml(
            f'<DERControlList xmlns="{NS}" href="/edev/{d}/derp/0/derc" all="{count}" results="{count}" '
            f'subscribable="0">{items}</DERControlList>'
        )

    @app.get("/edev/{d}/derp/0/dderc")
    async def dderc(request: Request, d: int) -> Response:
        device = _device(request, d)
        base = _base_xml(device.default) if device.default is not None else "<DERControlBase/>"
        return _xml(
            f'<DefaultDERControl xmlns="{NS}" href="/edev/{d}/derp/0/dderc"><mRID>{(d + 100):032X}</mRID>'
            f"<description>default</description>{base}</DefaultDERControl>"
        )

    @app.post("/rsp", status_code=201)
    async def rsp(request: Request) -> Response:
        root = fromstring(await request.body())
        if root.tag != f"{{{NS}}}DERControlResponse":
            raise HTTPException(status_code=400, detail="expected DERControlResponse")
        record = {child.tag.split("}")[-1]: (child.text or "") for child in root}
        _state(request).responses.append(record)
        return Response(status_code=201)

    # -- admin (JSON) ---------------------------------------------------------------------------------

    @app.post("/admin/devices")
    async def admin_add_device(request: Request, body: DeviceBody) -> dict[str, int]:
        return {"device": _state(request).add_device(body.lfdi, body.sfdi)}

    @app.post("/admin/devices/{d}/controls")
    async def admin_add_control(request: Request, d: int, body: ControlBody) -> dict[str, str]:
        _device(request, d)
        return {"mrid": _state(request).add_control(d, body)}

    @app.post("/admin/controls/{mrid}/cancel")
    async def admin_cancel(request: Request, mrid: str) -> dict[str, bool]:
        if not _state(request).cancel(mrid.upper()):
            raise HTTPException(status_code=404, detail="no such control")
        return {"cancelled": True}

    @app.put("/admin/devices/{d}/default")
    async def admin_default(request: Request, d: int, body: ControlBody) -> dict[str, bool]:
        device = _device(request, d)
        has_any = any(
            getattr(body, name) is not None for name in ControlBody.model_fields if name.startswith("op_mod")
        )
        device.default = body if has_any else None
        return {"set": has_any}

    @app.get("/admin/responses")
    async def admin_responses(request: Request) -> list[dict[str, Any]]:
        return _state(request).responses

    return app
