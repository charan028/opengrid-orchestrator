"""ERCOT MMS / EWS market-submission simulator (protocol-adapters.md S6.6).

One SOAP 1.1 endpoint, `POST /ews/`, accepting EWS `RequestMessage` envelopes:

    create BidSet (ThreePartOffer | ASOffer | <ESR curve element>)   -> ReplyCode OK + BidSet mRID
    cancel BidSet (mRID)                                             -> OK / ERROR
    get    AwardSet (Request: MarketType, TradingDate)               -> AwardedEnergyOffer / AwardedAS
    get    VDIs                                                      -> unacknowledged VDI list
    change VDIs (VDI/mRID)                                           -> acknowledges

Validation mirrors what MMS rejects synchronously: unknown QSE (Header/Source) or resource, a reused
ReplayDetection nonce, non-monotonic curves, an empty BidSet, and -- with `require_signature` -- a missing
or invalid WS-Security X.509 signature (verified with exclusive C14N, like ERCOT's gateway).

Clearing (deterministic, for tests and demos): per trading date, every offer interval clears against an
hourly settlement point price (`spp`, default flat, admin-settable) and per-service MCPCs. An energy curve
is awarded the largest cumulative MW whose price is <= SPP; an ESR bid/offer curve's charging side
(negative MW) is awarded when its bid price is >= SPP; an AS curve is awarded the largest cumulative
MW whose price is <= that service's MCPC.

Admin (JSON): `PUT /admin/prices`, `POST /admin/vdis`, `GET /admin/submissions`.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from xml.sax.saxutils import escape

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from defusedxml.ElementTree import fromstring
from fastapi import FastAPI, Request, Response
from lxml import etree
from pydantic import BaseModel, ConfigDict

__all__ = ["MmsSimState", "create_app"]

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
MSG = "http://www.ercot.com/schema/2007-06/nodal/ews/message"
PAY = "http://www.ercot.com/schema/2007-06/nodal/ews"
WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
WSU = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"
DS = "http://www.w3.org/2000/09/xmldsig#"
_HASHES = {"sha256": hashes.SHA256, "sha1": hashes.SHA1}
_AS_COLUMNS = ("REGUP", "RRSPF", "RRSFF", "RRSUF", "ECRS", "ONNS", "REGDN")
_COLUMN_SERVICE = {"RRSPF": "RRS", "RRSFF": "RRS", "RRSUF": "RRS", "ONNS": "NSPIN"}


class PricesBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spp: float | None = None
    mcpc: dict[str, float] | None = None


class VdiBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resource: str
    instructionType: str = "DEPLOY_AS"  # noqa: N815 -- ERCOT element spelling
    asType: str | None = None  # noqa: N815
    mw: float | None = None
    startTime: str | None = None  # noqa: N815
    endTime: str | None = None  # noqa: N815
    text: str = ""


@dataclass
class _Offer:
    mrid: str
    kind: str  # ENERGY | ESR | AS
    resource: str
    trading_date: str
    start: str
    end: str
    curve: list[tuple[Decimal, Decimal]]
    service: str | None = None
    cancelled: bool = False


@dataclass
class MmsSimState:
    qse_code: str = "QOPENGRID"
    resources: set[str] = field(default_factory=lambda: {"OG_ESR_1", "OG_ESR_2", "OG_GEN_1"})
    require_signature: bool = False
    spp: Decimal = Decimal("35")
    mcpc: dict[str, Decimal] = field(
        default_factory=lambda: {s: Decimal("10") for s in ("REGUP", "REGDN", "RRS", "ECRS", "NSPIN")}
    )
    esr_curve_element: str = "EnergyBidOfferCurve"
    offers: list[_Offer] = field(default_factory=list)
    nonces: set[str] = field(default_factory=set)
    vdis: dict[str, dict[str, Any]] = field(default_factory=dict)
    seq: int = 0


def _text(el: Any, path: str) -> str | None:
    node = el.find(path)
    return node.text.strip() if node is not None and node.text else None


def _reply(
    verb_noun: tuple[str, str], message_id: str | None, code: str, errors: list[str], payload: str = ""
) -> Response:
    errs = "".join(f"<m:Error>{escape(e)}</m:Error>" for e in errors)
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="{SOAP}"><soapenv:Body>'
        f'<m:ResponseMessage xmlns:m="{MSG}"><m:Header><m:Verb>reply</m:Verb><m:Noun>{verb_noun[1]}</m:Noun>'
        f"<m:MessageID>{escape(message_id or '')}</m:MessageID></m:Header>"
        f"<m:Reply><m:ReplyCode>{code}</m:ReplyCode>{errs}</m:Reply>"
        + (f"<m:Payload>{payload}</m:Payload>" if payload else "")
        + "</m:ResponseMessage></soapenv:Body></soapenv:Envelope>"
    )
    return Response(content=body, media_type="text/xml")


def _fault(reason: str) -> Response:
    body = (
        f'<?xml version="1.0" encoding="UTF-8"?><soapenv:Envelope xmlns:soapenv="{SOAP}"><soapenv:Body>'
        f"<soapenv:Fault><faultcode>soapenv:Client</faultcode><faultstring>{escape(reason)}</faultstring>"
        f"</soapenv:Fault></soapenv:Body></soapenv:Envelope>"
    )
    return Response(content=body, media_type="text/xml", status_code=500)


def _verify_signature(raw: bytes) -> str | None:
    """None if the WS-Security signature verifies; otherwise the reason."""
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
    doc = etree.fromstring(raw, parser)
    security = doc.find(f"{{{SOAP}}}Header/{{{WSSE}}}Security")
    if security is None:
        return "missing wsse:Security header"
    token = security.find(f"{{{WSSE}}}BinarySecurityToken")
    signed_info = security.find(f"{{{DS}}}Signature/{{{DS}}}SignedInfo")
    value = security.find(f"{{{DS}}}Signature/{{{DS}}}SignatureValue")
    if token is None or signed_info is None or value is None or not token.text or not value.text:
        return "incomplete WS-Security signature"
    method = signed_info.find(f"{{{DS}}}SignatureMethod").get("Algorithm", "")
    algo = "sha1" if method.endswith("rsa-sha1") else "sha256"
    by_id = {el.get(f"{{{WSU}}}Id"): el for el in doc.iter() if el.get(f"{{{WSU}}}Id")}
    body = doc.find(f"{{{SOAP}}}Body")
    covered = set()
    for ref in signed_info.findall(f"{{{DS}}}Reference"):
        target = by_id.get(ref.get("URI", "").lstrip("#"))
        if target is None:
            return "signature reference to unknown element"
        c14n = etree.tostring(target, method="c14n", exclusive=True, with_comments=False)
        digest = base64.b64encode(hashlib.new(algo, c14n).digest()).decode()
        if digest != (ref.findtext(f"{{{DS}}}DigestValue") or "").strip():
            return "digest mismatch (message altered after signing)"
        covered.add(target)
    if body not in covered:
        return "SOAP Body is not covered by the signature"
    public_key = x509.load_der_x509_certificate(base64.b64decode(token.text)).public_key()
    if not isinstance(public_key, rsa.RSAPublicKey):
        return "signing certificate is not RSA"
    try:
        public_key.verify(
            base64.b64decode(value.text),
            etree.tostring(signed_info, method="c14n", exclusive=True, with_comments=False),
            padding.PKCS1v15(),
            _HASHES[algo](),
        )
    except Exception:
        return "signature value does not verify"
    return None


def _curve(el: Any) -> list[tuple[Decimal, Decimal]]:
    points = []
    for data in el.iter(f"{{{PAY}}}CurveData"):
        points.append(
            (
                Decimal(_text(data, f"{{{PAY}}}xvalue") or "0"),
                Decimal(_text(data, f"{{{PAY}}}y1value") or "0"),
            )
        )
    return points


def _monotonic(points: list[tuple[Decimal, Decimal]]) -> bool:
    return all(b[0] > a[0] and b[1] >= a[1] for a, b in zip(points, points[1:], strict=False))


def create_app(state: MmsSimState | None = None) -> FastAPI:
    app = FastAPI(title="ogsim ERCOT MMS/EWS simulator")
    sim = state or MmsSimState()
    app.state.mms = sim

    def new_mrid(trading_date: str, code: str) -> str:
        sim.seq += 1
        return f"{sim.qse_code}.{trading_date.replace('-', '')}.{code}.{sim.seq}"

    @app.post("/ews/")
    async def ews(request: Request) -> Response:
        raw = await request.body()
        try:
            root = fromstring(raw)
        except Exception:
            return _fault("malformed XML")
        message = root.find(f"{{{SOAP}}}Body/{{{MSG}}}RequestMessage")
        if message is None:
            return _fault("no RequestMessage")
        verb = (_text(message, f"{{{MSG}}}Header/{{{MSG}}}Verb") or "").lower()
        noun = _text(message, f"{{{MSG}}}Header/{{{MSG}}}Noun") or ""
        message_id = _text(message, f"{{{MSG}}}Header/{{{MSG}}}MessageID")
        source = _text(message, f"{{{MSG}}}Header/{{{MSG}}}Source")
        nonce = _text(message, f"{{{MSG}}}Header/{{{MSG}}}ReplayDetection/{{{MSG}}}Nonce")
        vn = (verb, noun)
        if sim.require_signature:
            reason = _verify_signature(raw)
            if reason is not None:
                return _fault(f"authentication failed: {reason}")
        if source != sim.qse_code:
            return _reply(vn, message_id, "ERROR", [f"unknown QSE {source!r}"])
        if not nonce or nonce in sim.nonces:
            return _reply(vn, message_id, "ERROR", ["replay detected: nonce missing or reused"])
        sim.nonces.add(nonce)
        payload = message.find(f"{{{MSG}}}Payload")
        if verb == "create" and noun == "BidSet":
            return _create(vn, message_id, payload)
        if verb == "cancel" and noun == "BidSet":
            mrid = _text(payload, f"{{{PAY}}}BidSet/{{{PAY}}}mRID") if payload is not None else None
            for offer in sim.offers:
                if offer.mrid == mrid and not offer.cancelled:
                    offer.cancelled = True
                    return _reply(vn, message_id, "OK", [])
            return _reply(vn, message_id, "ERROR", [f"no active submission {mrid!r}"])
        if verb == "get" and noun == "AwardSet":
            trading_date = _text(message, f"{{{MSG}}}Request/{{{MSG}}}TradingDate") or ""
            return _reply(vn, message_id, "OK", [], _awards(trading_date))
        if verb == "get" and noun == "VDIs":
            return _reply(vn, message_id, "OK", [], _vdis())
        if verb == "change" and noun == "VDIs":
            mrid = (
                _text(payload, f"{{{PAY}}}VDIs/{{{PAY}}}VDI/{{{PAY}}}mRID") if payload is not None else None
            )
            if mrid in sim.vdis:
                sim.vdis[mrid]["acknowledged"] = True
                return _reply(vn, message_id, "OK", [])
            return _reply(vn, message_id, "ERROR", [f"no VDI {mrid!r}"])
        return _reply(vn, message_id, "ERROR", [f"unsupported {verb} {noun}"])

    def _create(vn: tuple[str, str], message_id: str | None, payload: Any) -> Response:
        bid_set = payload.find(f"{{{PAY}}}BidSet") if payload is not None else None
        if bid_set is None:
            return _reply(vn, message_id, "ERROR", ["empty BidSet"])
        trading_date = _text(bid_set, f"{{{PAY}}}tradingDate") or ""
        created: list[_Offer] = []
        errors: list[str] = []
        for tx in list(bid_set):
            tag = tx.tag.split("}")[-1]
            if tag == "tradingDate":
                continue
            resource = _text(tx, f"{{{PAY}}}resource") or ""
            if resource not in sim.resources:
                errors.append(f"{tag}: unknown resource {resource!r}")
                continue
            if tag in ("ThreePartOffer", sim.esr_curve_element):
                eoc = tx.find(f"{{{PAY}}}EnergyOfferCurve") if tag == "ThreePartOffer" else tx
                if eoc is None:
                    errors.append(f"{tag}: no EnergyOfferCurve")
                    continue
                points = _curve(eoc)
                if not points or not _monotonic(points):
                    errors.append(f"{tag}: curve must be monotonically increasing")
                    continue
                kind = "ESR" if tag == sim.esr_curve_element else "ENERGY"
                start = _text(eoc, f"{{{PAY}}}startTime") or ""
                end = _text(eoc, f"{{{PAY}}}endTime") or ""
                created.append(
                    _Offer(new_mrid(trading_date, "EOC"), kind, resource, trading_date, start, end, points)
                )
            elif tag == "ASOffer":
                curve = tx.find(f"{{{PAY}}}ASPriceCurve")
                if curve is None:
                    errors.append("ASOffer: no ASPriceCurve")
                    continue
                for column in _AS_COLUMNS:
                    points = [
                        (
                            Decimal(_text(row, f"{{{PAY}}}xvalue") or "0"),
                            Decimal(_text(row, f"{{{PAY}}}{column}") or "0"),
                        )
                        for row in curve
                        if row.find(f"{{{PAY}}}{column}") is not None
                    ]
                    if points:
                        created.append(_Offer(new_mrid(trading_date, "ASO"), "AS", resource, trading_date,
                                              _text(curve, f"{{{PAY}}}startTime") or "",
                                              _text(curve, f"{{{PAY}}}endTime") or "", points,
                                              service=_COLUMN_SERVICE.get(column, column)))  # fmt: skip
            else:
                errors.append(f"unsupported transaction {tag}")
        if errors:
            return _reply(vn, message_id, "ERROR", errors)
        sim.offers.extend(created)
        payload_xml = (
            f'<BidSet xmlns="{PAY}">'
            + "".join(f"<mRID>{escape(o.mrid)}</mRID><status>SUBMITTED</status>" for o in created)
            + "</BidSet>"
        )
        return _reply(vn, message_id, "OK", [], payload_xml)

    def _awards(trading_date: str) -> str:
        parts: list[str] = []
        for offer in sim.offers:
            if offer.cancelled or offer.trading_date != trading_date:
                continue
            hours = Decimal(
                str(
                    (datetime.fromisoformat(offer.end) - datetime.fromisoformat(offer.start)).total_seconds()
                    / 3600
                )
            )
            if offer.kind in ("ENERGY", "ESR"):
                sell = [mw for mw, price in offer.curve if mw > 0 and price <= sim.spp]
                buy = [mw for mw, price in offer.curve if mw < 0 and price >= sim.spp]
                mw = max(sell) if sell else (min(buy) if buy else Decimal("0"))
                if mw == 0:
                    continue
                parts.append(
                    f"<AwardedEnergyOffer><mRID>{escape(offer.mrid)}</mRID><qse>{sim.qse_code}</qse>"
                    f"<resource>{escape(offer.resource)}</resource><startTime>{offer.start}</startTime>"
                    f"<endTime>{offer.end}</endTime><awardedMWh>{mw * hours}</awardedMWh>"
                    f"<price>{sim.spp}</price></AwardedEnergyOffer>"
                )
            else:
                mcpc = sim.mcpc.get(offer.service or "", Decimal("0"))
                cleared = [mw for mw, price in offer.curve if price <= mcpc]
                if not cleared:
                    continue
                parts.append(
                    f"<AwardedAS><mRID>{escape(offer.mrid)}</mRID><qse>{sim.qse_code}</qse>"
                    f"<resource>{escape(offer.resource)}</resource><asType>{offer.service}</asType>"
                    f"<startTime>{offer.start}</startTime><endTime>{offer.end}</endTime>"
                    f"<awardedMW>{max(cleared)}</awardedMW><mcpc>{mcpc}</mcpc></AwardedAS>"
                )
        return f'<AwardSet xmlns="{PAY}">{"".join(parts)}</AwardSet>'

    def _vdis() -> str:
        items = []
        for mrid, vdi in sim.vdis.items():
            if vdi.get("acknowledged"):
                continue
            details = f"<instructionType>{escape(vdi['instructionType'])}</instructionType>"
            for key in ("asType", "mw", "startTime", "endTime"):
                if vdi.get(key) is not None:
                    details += f"<{key}>{escape(str(vdi[key]))}</{key}>"
            details += f"<instructionText>{escape(vdi.get('text', ''))}</instructionText>"
            details += "<ercotOperatorName>SIM OPERATOR</ercotOperatorName>"
            items.append(
                f"<VDI><mRID>{escape(mrid)}</mRID><resource>{escape(vdi['resource'])}</resource>"
                f"<vdiRefNum>{escape(mrid)}</vdiRefNum><notificationTime>{vdi['notificationTime']}</notificationTime>"
                f"<Details>{details}</Details></VDI>"
            )
        return f'<VDIs xmlns="{PAY}">{"".join(items)}</VDIs>'

    # -- admin --------------------------------------------------------------------------------------

    @app.put("/admin/prices")
    async def admin_prices(body: PricesBody) -> dict[str, Any]:
        if body.spp is not None:
            sim.spp = Decimal(str(body.spp))
        for service, price in (body.mcpc or {}).items():
            sim.mcpc[service.upper()] = Decimal(str(price))
        return {"spp": str(sim.spp), "mcpc": {k: str(v) for k, v in sim.mcpc.items()}}

    @app.post("/admin/vdis")
    async def admin_vdi(body: VdiBody) -> dict[str, str]:
        mrid = f"{sim.qse_code}.VDI.{uuid.uuid4().hex[:12]}"
        record = body.model_dump()
        record["notificationTime"] = body.startTime or datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        sim.vdis[mrid] = record
        return {"mrid": mrid}

    @app.get("/admin/submissions")
    async def admin_submissions() -> list[dict[str, Any]]:
        return [
            {"mrid": o.mrid, "kind": o.kind, "resource": o.resource, "service": o.service,
             "trading_date": o.trading_date, "cancelled": o.cancelled}
            for o in sim.offers
        ]  # fmt: skip

    return app
