"""ogsim IEEE 2030.5 server: resource tree, event status over time, responses and admin API."""

from __future__ import annotations

from defusedxml.ElementTree import fromstring
from fastapi.testclient import TestClient

from ogsim.protocols.ieee2030_5_server import MEDIA, NS, Sep2ServerState, create_app

P = f"{{{NS}}}"


class _Clock:
    t = 1_790_000_000.0

    def __call__(self) -> float:
        return self.t


def _client() -> tuple[TestClient, Sep2ServerState, _Clock]:
    clock = _Clock()
    state = Sep2ServerState(clock=clock)
    state.add_device("abcdef0123456789abcdef0123456789abcdef01", sfdi=123456789)
    return TestClient(create_app(state)), state, clock


def test_resource_tree_links_down_to_der_controls() -> None:
    client, _, _ = _client()
    dcap = client.get("/dcap")
    assert dcap.headers["content-type"].startswith(MEDIA)
    edev_href = fromstring(dcap.content).find(f"{P}EndDeviceListLink").get("href")
    edev = fromstring(client.get(edev_href).content)
    device = edev.find(f"{P}EndDevice")
    assert device.findtext(f"{P}lFDI") == "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    fsa = fromstring(client.get(device.find(f"{P}FunctionSetAssignmentsListLink").get("href")).content)
    derp_href = fsa.find(f"{P}FunctionSetAssignments/{P}DERProgramListLink").get("href")
    program = fromstring(client.get(derp_href).content).find(f"{P}DERProgram")
    assert program.findtext(f"{P}primacy") == "1"
    controls = fromstring(client.get(program.find(f"{P}DERControlListLink").get("href")).content)
    assert controls.get("all") == "0"
    assert client.get("/edev/9/fsa").status_code == 404


def test_control_lifecycle_and_base_encoding() -> None:
    client, state, clock = _client()
    mrid = client.post(
        "/admin/devices/0/controls",
        json={"start": int(clock.t) + 10, "duration": 60, "opModMaxLimW": 5000, "opModTargetW": 1000},
    ).json()["mrid"]

    def status() -> str:
        control = fromstring(client.get("/edev/0/derp/0/derc").content).find(f"{P}DERControl")
        return control.findtext(f"{P}EventStatus/{P}currentStatus") or ""

    assert status() == "0"
    clock.t += 15
    assert status() == "1"
    control = fromstring(client.get("/edev/0/derp/0/derc").content).find(f"{P}DERControl")
    assert control.findtext(f"{P}DERControlBase/{P}opModMaxLimW") == "5000"
    assert control.findtext(f"{P}DERControlBase/{P}opModTargetW/{P}value") == "1000"
    assert control.get("replyTo") == "/rsp"
    assert client.post(f"/admin/controls/{mrid.lower()}/cancel").status_code == 200
    assert status() == "2"
    assert client.post("/admin/controls/NOPE/cancel").status_code == 404
    assert state.status(state.devices[0].controls[0]) == 2


def test_default_control_and_responses() -> None:
    client, _, _ = _client()
    assert client.put("/admin/devices/0/default", json={"opModConnect": False}).json() == {"set": True}
    dderc = fromstring(client.get("/edev/0/derp/0/dderc").content)
    assert dderc.findtext(f"{P}DERControlBase/{P}opModConnect") == "false"
    assert client.put("/admin/devices/0/default", json={}).json() == {"set": False}
    body = (
        f'<DERControlResponse xmlns="{NS}"><createdDateTime>1</createdDateTime>'
        f"<endDeviceLFDI>AA</endDeviceLFDI><status>1</status><subject>M1</subject></DERControlResponse>"
    )
    assert client.post("/rsp", content=body).status_code == 201
    assert client.get("/admin/responses").json() == [
        {"createdDateTime": "1", "endDeviceLFDI": "AA", "status": "1", "subject": "M1"}
    ]
    assert client.post("/rsp", content=f'<Other xmlns="{NS}"/>').status_code == 400
    assert client.post("/admin/devices", json={"lfdi": "ff" * 20}).json() == {"device": 1}
