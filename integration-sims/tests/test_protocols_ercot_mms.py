"""ogsim ERCOT MMS/EWS simulator: SOAP handling, validation, clearing, VDIs and signature enforcement."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from defusedxml.ElementTree import fromstring
from fastapi.testclient import TestClient

from ogsim.protocols.ercot_mms import MSG, PAY, SOAP, MmsSimState, _verify_signature, create_app

M = f"{{{MSG}}}"
P = f"{{{PAY}}}"
_counter = 0


def _envelope(verb: str, noun: str, payload: str = "", request: str = "", source: str = "QOPENGRID") -> str:
    global _counter
    _counter += 1
    return (
        f'<soapenv:Envelope xmlns:soapenv="{SOAP}"><soapenv:Header/><soapenv:Body>'
        f'<m:RequestMessage xmlns:m="{MSG}"><m:Header><m:Verb>{verb}</m:Verb><m:Noun>{noun}</m:Noun>'
        f"<m:ReplayDetection><m:Nonce>n{_counter}</m:Nonce><m:Created>2026-09-26T10:00:00-05:00</m:Created>"
        f"</m:ReplayDetection><m:Revision>1</m:Revision><m:Source>{source}</m:Source><m:UserID>u</m:UserID>"
        f"<m:MessageID>id{_counter}</m:MessageID></m:Header>"
        + (f"<m:Request>{request}</m:Request>" if request else "")
        + (f"<m:Payload>{payload}</m:Payload>" if payload else "")
        + "</m:RequestMessage></soapenv:Body></soapenv:Envelope>"
    )


def _post(client: TestClient, body: str) -> Any:
    response = client.post("/ews/", content=body)
    return fromstring(response.content).find(f"{{{SOAP}}}Body/{M}ResponseMessage")


TIMES = "<startTime>2026-09-27T17:00:00-05:00</startTime><endTime>2026-09-27T18:00:00-05:00</endTime>"


def _bidset(inner: str) -> str:
    return f'<BidSet xmlns="{PAY}"><tradingDate>2026-09-27</tradingDate>{inner}</BidSet>'


def test_offers_clear_into_awards() -> None:
    state = MmsSimState(spp=Decimal("40"))
    client = TestClient(create_app(state))
    tpo = (
        f"<ThreePartOffer><resource>OG_GEN_1</resource><EnergyOfferCurve>{TIMES}"
        "<CurveData><xvalue>5</xvalue><y1value>30</y1value></CurveData>"
        "<CurveData><xvalue>9</xvalue><y1value>45</y1value></CurveData></EnergyOfferCurve></ThreePartOffer>"
    )
    aso = (
        f"<ASOffer><resource>OG_ESR_1</resource><asType>REGUP-RRS-ONNS</asType><ASPriceCurve>{TIMES}"
        "<OnLineReserves><xvalue>4</xvalue><REGUP>8</REGUP></OnLineReserves>"
        "<OnLineReserves><xvalue>6</xvalue><REGUP>15</REGUP></OnLineReserves></ASPriceCurve></ASOffer>"
    )
    reply = _post(client, _envelope("create", "BidSet", _bidset(tpo + aso)))
    assert reply.findtext(f"{M}Reply/{M}ReplyCode") == "OK"
    assert len(reply.findall(f"{M}Payload/{P}BidSet/{P}mRID")) == 2
    awards = _post(
        client,
        _envelope(
            "get",
            "AwardSet",
            request="<m:MarketType>DAM</m:MarketType><m:TradingDate>2026-09-27</m:TradingDate>",
        ),
    ).find(f"{M}Payload/{P}AwardSet")
    energy = awards.find(f"{P}AwardedEnergyOffer")
    assert energy.findtext(f"{P}awardedMWh") == "5.0" and energy.findtext(f"{P}price") == "40"
    as_award = awards.find(f"{P}AwardedAS")
    assert as_award.findtext(f"{P}asType") == "REGUP" and as_award.findtext(f"{P}awardedMW") == "4"
    assert [s["kind"] for s in client.get("/admin/submissions").json()] == ["ENERGY", "AS"]


def test_synchronous_validation() -> None:
    client = TestClient(create_app(MmsSimState()))
    unknown = _post(
        client,
        _envelope("create", "BidSet", _bidset("<ThreePartOffer><resource>X</resource></ThreePartOffer>")),
    )
    assert unknown.findtext(f"{M}Reply/{M}ReplyCode") == "ERROR"
    assert "unknown resource" in unknown.findtext(f"{M}Reply/{M}Error")
    bad_curve = (
        f"<ThreePartOffer><resource>OG_GEN_1</resource><EnergyOfferCurve>{TIMES}"
        "<CurveData><xvalue>5</xvalue><y1value>30</y1value></CurveData>"
        "<CurveData><xvalue>4</xvalue><y1value>45</y1value></CurveData></EnergyOfferCurve></ThreePartOffer>"
    )
    assert "monotonically" in _post(client, _envelope("create", "BidSet", _bidset(bad_curve))).findtext(
        f"{M}Reply/{M}Error"
    )
    assert (
        _post(client, _envelope("create", "BidSet", source="QOTHER")).findtext(f"{M}Reply/{M}ReplyCode")
        == "ERROR"
    )
    assert _post(client, _envelope("create", "BidSet")).findtext(f"{M}Reply/{M}Error") == "empty BidSet"
    assert client.post("/ews/", content="not xml").status_code == 500


def test_vdis_and_prices_admin() -> None:
    client = TestClient(create_app(MmsSimState()))
    assert (
        client.put("/admin/prices", json={"spp": 55, "mcpc": {"ecrs": 12}}).json()["mcpc"]["ECRS"] == "12.0"
    )
    mrid = client.post("/admin/vdis", json={"resource": "OG_ESR_1", "asType": "ECRS", "mw": 2}).json()["mrid"]
    vdis = _post(client, _envelope("get", "VDIs")).find(f"{M}Payload/{P}VDIs")
    vdi = vdis.find(f"{P}VDI")
    assert vdi.findtext(f"{P}Details/{P}instructionType") == "DEPLOY_AS"
    ack = f'<VDIs xmlns="{PAY}"><VDI><mRID>{mrid}</mRID></VDI></VDIs>'
    assert _post(client, _envelope("change", "VDIs", ack)).findtext(f"{M}Reply/{M}ReplyCode") == "OK"
    assert _post(client, _envelope("get", "VDIs")).find(f"{M}Payload/{P}VDIs/{P}VDI") is None


def test_signature_required() -> None:
    client = TestClient(create_app(MmsSimState(require_signature=True)))
    response = client.post("/ews/", content=_envelope("get", "VDIs"))
    assert response.status_code == 500 and b"missing wsse:Security" in response.content
    assert _verify_signature(_envelope("get", "VDIs").encode()) == "missing wsse:Security header"
