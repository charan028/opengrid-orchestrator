"""D-35: ERCOT AS deployment dispatch instructions -- the instruction book (latency, duplicates, out-of-order,
acknowledgements) and ogsim.market's /mms/ews/ endpoint driven by the ercot_as_* scenario anomalies."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from defusedxml.ElementTree import fromstring
from fastapi.testclient import TestClient

from ogsim.control import catalogue
from ogsim.control.scenarios import load_scenario
from ogsim.market.app import create_app
from ogsim.market.as_dispatch import AS_DISPATCH_TYPES
from ogsim.market.config import MarketConfig
from ogsim.protocols.ercot_as_dispatch import AsDispatchBook, DeliveryProfile
from ogsim.protocols.ercot_mms import MSG, PAY, SOAP

M = f"{{{MSG}}}"
P = f"{{{PAY}}}"
T0 = datetime(2026, 9, 27, 5, 0, tzinfo=UTC)
SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"
_nonce = 0


class _Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


def _envelope(verb: str, noun: str, payload: str = "") -> str:
    global _nonce
    _nonce += 1
    return (
        f'<soapenv:Envelope xmlns:soapenv="{SOAP}"><soapenv:Body><m:RequestMessage xmlns:m="{MSG}"><m:Header>'
        f"<m:Verb>{verb}</m:Verb><m:Noun>{noun}</m:Noun><m:ReplayDetection><m:Nonce>as{_nonce}</m:Nonce>"
        f"</m:ReplayDetection><m:Source>QOPENGRID</m:Source><m:MessageID>m{_nonce}</m:MessageID></m:Header>"
        + (f"<m:Payload>{payload}</m:Payload>" if payload else "")
        + "</m:RequestMessage></soapenv:Body></soapenv:Envelope>"
    )


def _book(**profile: Any) -> AsDispatchBook:
    return AsDispatchBook(qse_code="Q", profile=DeliveryProfile(**profile))


def _deploy(book: AsDispatchBook, at: datetime, mw: float = 1.0) -> str:
    return book.publish(
        resource="R",
        instruction_type="DEPLOY_AS",
        issued_at=at,
        as_type="ECRS",
        mw=mw,
        end=at + timedelta(minutes=30),
    ).mrid


# -- the instruction book ----------------------------------------------------------------------------------


def test_an_instruction_becomes_visible_after_its_latency() -> None:
    book = _book(latency_s=(2.0, 2.0))
    mrid = _deploy(book, T0)
    assert book.pending(T0 + timedelta(seconds=1)) == []
    assert [i.mrid for i in book.pending(T0 + timedelta(seconds=2))] == [mrid]


def test_an_acknowledged_instruction_is_not_delivered_again_and_the_answer_is_kept() -> None:
    book = _book()
    mrid = _deploy(book, T0)
    assert book.acknowledge(mrid, accepted=False, reason="R-X exceeds award")
    assert book.pending(T0) == []
    (row,) = book.responses()
    assert (row["response"], row["reason"]) == ("REJECTED", "R-X exceeds award")
    assert book.acknowledge("nope", accepted=True, reason=None) is False


def test_a_duplicate_is_delivered_exactly_once_more_after_the_acknowledgement() -> None:
    book = _book(duplicate_probability=1.0)
    mrid = _deploy(book, T0)
    book.acknowledge(mrid, accepted=True, reason=None)
    assert [i.mrid for i in book.pending(T0)] == [mrid]
    assert book.pending(T0) == []
    book.acknowledge(mrid, accepted=True, reason=None)  # a second ack schedules no further duplicate
    assert book.pending(T0) == []


def test_a_batch_can_arrive_out_of_order() -> None:
    book = _book(reorder_probability=1.0)
    first = _deploy(book, T0)
    second = _deploy(book, T0 + timedelta(seconds=1))
    assert [i.mrid for i in book.pending(T0 + timedelta(seconds=5))] == [second, first]


def test_malformed_fields_are_rendered_verbatim() -> None:
    book = _book()
    instruction = book.publish(
        resource="R", instruction_type="DEPLOY_AS", issued_at=T0, mw=1.0, malformed_fields={"mw": "fifty"}
    )
    assert instruction.fields()["mw"] == "fifty"


# -- ogsim.market /mms ---------------------------------------------------------------------------------------


@pytest.fixture
def market(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, _Clock]]:
    monkeypatch.setenv("OGSIM_AS_LATENCY_S", "0,0")
    monkeypatch.setenv("OGSIM_AS_DUPLICATE_PROBABILITY", "0")
    monkeypatch.setenv("OGSIM_AS_REORDER_PROBABILITY", "0")
    monkeypatch.setenv("OGSIM_AS_AWARDS", "OG_ESR_1:ECRS:0.5,OG_ESR_1:RRS:0.4")
    clock = _Clock()
    app = create_app(MarketConfig(data_mode="synthetic", seed=3), clock=clock)
    with TestClient(app) as client:
        yield client, clock


def _inject(client: TestClient, type_: str, target: str = "OG_ESR_1", **params: Any) -> list[str]:
    body = {"id": f"t-{type_}", "type": type_, "target": target, "params": params, "duration": 60}
    resp = client.post("/admin/anomalies", json=body)
    assert resp.status_code == 200, resp.text
    return list(resp.json()["instructions"])


def _vdis(client: TestClient) -> list[Any]:
    reply = fromstring(client.post("/mms/ews/", content=_envelope("get", "VDIs")).content)
    return reply.findall(f"{{{SOAP}}}Body/{M}ResponseMessage/{M}Payload/{P}VDIs/{P}VDI")


def _ack(client: TestClient, mrid: str, response: str = "ACCEPT", reason: str = "") -> str:
    payload = (
        f'<VDIs xmlns="{PAY}"><VDI><mRID>{mrid}</mRID><acknowledged>true</acknowledged>'
        f"<response>{response}</response><reason>{reason}</reason></VDI></VDIs>"
    )
    reply = fromstring(client.post("/mms/ews/", content=_envelope("change", "VDIs", payload)).content)
    return str(reply.findtext(f"{{{SOAP}}}Body/{M}ResponseMessage/{M}Reply/{M}ReplyCode"))


def test_a_normal_ecrs_deployment_carries_resource_product_mw_start_ramp_and_end(market) -> None:  # type: ignore[no-untyped-def]
    client, _clock = market
    (mrid,) = _inject(client, "ercot_as_deploy", service="ECRS", duration_min=30, ramp_min=10)
    (vdi,) = _vdis(client)
    details = {child.tag.split("}")[1]: child.text for child in vdi.find(f"{P}Details")}
    assert vdi.findtext(f"{P}mRID") == mrid and vdi.findtext(f"{P}resource") == "OG_ESR_1"
    assert (details["instructionType"], details["asType"], details["mw"]) == ("DEPLOY_AS", "ECRS", "0.5")
    assert details["rampMinutes"] == "10"
    start = datetime.fromisoformat(details["startTime"])
    assert datetime.fromisoformat(details["endTime"]) - start == timedelta(minutes=30)
    assert _ack(client, mrid) == "OK"
    assert _vdis(client) == []
    (row,) = client.get("/mms/admin/vdis").json()
    assert row["response"] == "ACCEPTED"


def test_an_rrs_event_and_its_recall_name_the_deployment(market) -> None:  # type: ignore[no-untyped-def]
    client, clock = market
    (deploy,) = _inject(client, "ercot_as_deploy", service="RRS", ramp_min=0)
    clock.now += timedelta(minutes=10)
    (recall,) = _inject(client, "ercot_as_recall", service="RRS")
    by_id = {v.findtext(f"{P}mRID"): v for v in _vdis(client)}
    assert by_id[deploy].findtext(f"{P}Details/{P}mw") == "0.4"
    assert by_id[recall].findtext(f"{P}Details/{P}instructionType") == "RECALL_AS"
    assert by_id[recall].findtext(f"{P}Details/{P}recallOf") == deploy


def test_abnormal_scenarios(market) -> None:  # type: ignore[no-untyped-def]
    client, clock = market
    (dup,) = _inject(client, "ercot_as_duplicate")
    (bad,) = _inject(client, "ercot_as_malformed")
    (unknown,) = _inject(client, "ercot_as_unknown_award", target="*")
    (exceed,) = _inject(client, "ercot_as_exceed_award", factor=3.0)
    by_id = {v.findtext(f"{P}mRID"): v for v in _vdis(client)}
    assert by_id[bad].findtext(f"{P}Details/{P}mw") == "fifty"
    assert by_id[unknown].findtext(f"{P}resource") == "OG_ESR_UNKNOWN"
    assert by_id[exceed].findtext(f"{P}Details/{P}mw") == "1.5"
    assert _ack(client, dup) == "OK"
    assert [v.findtext(f"{P}mRID") for v in _vdis(client) if v.findtext(f"{P}mRID") == dup] == [dup]
    assert _ack(client, bad, "REJECT", "422 malformed") == "OK"
    assert _ack(client, bad, "MAYBE") == "ERROR"


def test_an_out_of_order_pair_delivers_the_recall_first(market) -> None:  # type: ignore[no-untyped-def]
    client, clock = market
    deploy, recall = _inject(client, "ercot_as_out_of_order")
    assert [v.findtext(f"{P}mRID") for v in _vdis(client)] == [recall]
    clock.now += timedelta(seconds=30)
    assert {v.findtext(f"{P}mRID") for v in _vdis(client)} == {deploy, recall}


def test_every_dispatch_type_is_a_market_catalogue_entry_and_every_scenario_loads() -> None:
    assert {a.id for a in catalogue.CATALOGUE if a.id.startswith("ercot_as_")} == set(AS_DISPATCH_TYPES)
    assert all(catalogue.owner_of(t) == "market" for t in AS_DISPATCH_TYPES)
    names = [load_scenario(p).name for p in sorted(SCENARIOS.glob("ercot-as-*.yaml"))]
    assert len(names) == 7
