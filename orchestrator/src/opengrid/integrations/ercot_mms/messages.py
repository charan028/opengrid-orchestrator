"""ERCOT MMS External Web Services (EWS) message shapes (protocol-adapters.md S6.2).

Sources (public ERCOT developer portal, developer.ercot.com/applications/ews): the EWS message
structure (Appendix D annotated SOAP message, Appendix E examples), Market Transaction messages
(Three-Part Supply Offer, Ancillary Service Offer, COP), Market Information messages (AwardSet) and
Verbal Dispatch Instructions. Element names below follow those pages. Where the public pages do not
settle a spelling it is marked UNCONFIRMED and is a config value, never a silent guess:

- the ESR Energy Bid/Offer Curve element introduced with RTC+B (`esr_curve_element`, default
  `EnergyBidOfferCurve`) -- its XSD ships in ERCOT's EIS v2.0 zip, which needs checking at onboarding;
- the RRS sub-type an ESR offers (`rrs_price_tag`, default `RRSFF`);
- the row element of a Reg-Down (`asType` REGDN) price curve (sent as `OnLineReserves`).

Envelope: SOAP 1.1, document/literal. `RequestMessage` (namespace
`http://www.ercot.com/schema/2007-06/nodal/ews/message`) holds `Header` (Verb, Noun, ReplayDetection
{Nonce, Created}, Revision, Source = QSE code, UserID, MessageID, Comment), `Request` and `Payload`.
Market-transaction payloads are a `BidSet` in `http://www.ercot.com/schema/2007-06/nodal/ews`.
Replies are `ResponseMessage` with `Reply/ReplyCode` = OK | ERROR | FATAL.

Times are sent in Central Prevailing Time with an explicit UTC offset (`to_market_tz`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from xml.etree.ElementTree import Element

from defusedxml.ElementTree import fromstring
from lxml import etree  # type: ignore[import-untyped]

from opengrid.core.timeutil import to_market_tz
from opengrid.integrations.interfaces import (
    AsOffer,
    AsService,
    Award,
    DispatchInstruction,
    EnergyOffer,
    InstructionBatch,
    MalformedInstruction,
    OfferCurvePoint,
    ThreePartSupplyOffer,
)

__all__ = [
    "MSG_NS",
    "PAYLOAD_NS",
    "SOAP_NS",
    "EwsError",
    "EwsReply",
    "RequestHeader",
    "as_offer_element",
    "build_request",
    "energy_offer_element",
    "parse_awards",
    "parse_reply",
    "parse_vdi_batch",
    "parse_vdis",
    "three_part_offer_element",
]

SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
MSG_NS = "http://www.ercot.com/schema/2007-06/nodal/ews/message"
PAYLOAD_NS = "http://www.ercot.com/schema/2007-06/nodal/ews"
_S = f"{{{SOAP_NS}}}"
_M = f"{{{MSG_NS}}}"
_P = f"{{{PAYLOAD_NS}}}"

#: Our AS service -> (EWS asType group, price column in ASPriceCurve/OnLineReserves).
AS_OFFER_COLUMNS: dict[AsService, tuple[str, str]] = {
    "REGUP": ("REGUP-RRS-ONNS", "REGUP"),
    "RRS": ("REGUP-RRS-ONNS", "RRSFF"),
    "ECRS": ("REGUP-RRS-ONNS", "ECRS"),
    "NSPIN": ("REGUP-RRS-ONNS", "ONNS"),
    "REGDN": ("REGDN", "REGDN"),
}
#: EWS award asType -> our service.
_AWARD_AS_TYPES: dict[str, AsService] = {
    "REGUP": "REGUP",
    "REGDN": "REGDN",
    "RRS": "RRS",
    "RRSPF": "RRS",
    "RRSFF": "RRS",
    "RRSUF": "RRS",
    "ECRS": "ECRS",
    "NSPIN": "NSPIN",
    "ONNS": "NSPIN",
    "OFFNS": "NSPIN",
}


class EwsError(Exception):
    """A SOAP fault or an unparseable EWS reply."""


@dataclass(frozen=True, slots=True)
class RequestHeader:
    verb: str
    noun: str
    source: str  # QSE code
    user_id: str
    message_id: str
    nonce: str
    created: datetime
    revision: str = "1"
    comment: str | None = None


@dataclass(frozen=True, slots=True)
class EwsReply:
    code: str  # OK | ERROR | FATAL
    errors: list[str]
    payload: Element | None
    message_id: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


def _ts(dt: datetime) -> str:
    return to_market_tz(dt).isoformat(timespec="seconds")


def _sub(parent: Any, tag: str, text: str | None = None) -> Any:
    el = etree.SubElement(parent, tag)
    if text is not None:
        el.text = text
    return el


def _dec(value: Decimal) -> str:
    return format(value.normalize(), "f")


def build_request(
    header: RequestHeader, *, request_fields: dict[str, str] | None = None, payload: Any = None
) -> Any:
    """The SOAP envelope (an lxml element, so a signer can canonicalize and sign it before sending)."""
    envelope = etree.Element(f"{_S}Envelope", nsmap={"soapenv": SOAP_NS})
    etree.SubElement(envelope, f"{_S}Header")
    body = etree.SubElement(envelope, f"{_S}Body")
    message = etree.SubElement(body, f"{_M}RequestMessage", nsmap={"ns0": MSG_NS})
    hdr = _sub(message, f"{_M}Header")
    _sub(hdr, f"{_M}Verb", header.verb)
    _sub(hdr, f"{_M}Noun", header.noun)
    replay = _sub(hdr, f"{_M}ReplayDetection")
    _sub(replay, f"{_M}Nonce", header.nonce)
    _sub(replay, f"{_M}Created", _ts(header.created))
    _sub(hdr, f"{_M}Revision", header.revision)
    _sub(hdr, f"{_M}Source", header.source)
    _sub(hdr, f"{_M}UserID", header.user_id)
    _sub(hdr, f"{_M}MessageID", header.message_id)
    if header.comment:
        _sub(hdr, f"{_M}Comment", header.comment)
    if request_fields:
        request = _sub(message, f"{_M}Request")
        for key, value in request_fields.items():
            _sub(request, f"{_M}{key}", value)
    if payload is not None:
        _sub(message, f"{_M}Payload").append(payload)
    return envelope


def bid_set(trading_date: date, *transactions: Any) -> Any:
    root = etree.Element(f"{_P}BidSet", nsmap={None: PAYLOAD_NS})
    _sub(root, f"{_P}tradingDate", trading_date.isoformat())
    for tx in transactions:
        root.append(tx)
    return root


def _curve(parent: Any, points: list[OfferCurvePoint]) -> None:
    for point in points:
        data = _sub(parent, f"{_P}CurveData")
        _sub(data, f"{_P}xvalue", _dec(point.mw))
        _sub(data, f"{_P}y1value", _dec(point.price_usd_per_mwh))


def _common(el: Any, resource: str, external_ref: str | None, expiration: datetime | None) -> None:
    _sub(el, f"{_P}resource", resource)
    if external_ref:
        _sub(el, f"{_P}externalId", external_ref)
    if expiration is not None:
        _sub(el, f"{_P}expirationTime", _ts(expiration))


def three_part_offer_element(offer: ThreePartSupplyOffer, *, expiration: datetime | None = None) -> Any:
    tpo = etree.Element(f"{_P}ThreePartOffer")
    _common(tpo, offer.resource_id, offer.external_ref, expiration)
    startup = _sub(tpo, f"{_P}StartupCost")
    _sub(startup, f"{_P}startTime", _ts(offer.interval_start))
    _sub(startup, f"{_P}endTime", _ts(offer.interval_end))
    for temp in ("hot", "intermediate", "cold"):
        _sub(startup, f"{_P}{temp}", _dec(offer.startup_cost_usd))
    minimum = _sub(tpo, f"{_P}MinimumEnergy")
    _sub(minimum, f"{_P}startTime", _ts(offer.interval_start))
    _sub(minimum, f"{_P}endTime", _ts(offer.interval_end))
    _sub(minimum, f"{_P}cost", _dec(offer.min_energy_cost_usd_per_mwh))
    eoc = _sub(tpo, f"{_P}EnergyOfferCurve")
    _sub(eoc, f"{_P}startTime", _ts(offer.interval_start))
    _sub(eoc, f"{_P}endTime", _ts(offer.interval_end))
    _curve(eoc, offer.energy_curve)
    return tpo


def energy_offer_element(
    offer: EnergyOffer, *, esr_curve_element: str = "EnergyBidOfferCurve", expiration: datetime | None = None
) -> Any:
    """A generation energy offer is a ThreePartOffer carrying only its EnergyOfferCurve; an ESR curve
    (RTC+B, negative MW = charging) uses `esr_curve_element` (UNCONFIRMED spelling, module docstring)."""
    if offer.curve_kind == "ESR_BID_OFFER":
        el = etree.Element(f"{_P}{esr_curve_element}")
        _common(el, offer.resource_id, offer.external_ref, expiration)
        _sub(el, f"{_P}startTime", _ts(offer.interval_start))
        _sub(el, f"{_P}endTime", _ts(offer.interval_end))
        _curve(el, offer.curve)
        return el
    tpo = etree.Element(f"{_P}ThreePartOffer")
    _common(tpo, offer.resource_id, offer.external_ref, expiration)
    eoc = _sub(tpo, f"{_P}EnergyOfferCurve")
    _sub(eoc, f"{_P}startTime", _ts(offer.interval_start))
    _sub(eoc, f"{_P}endTime", _ts(offer.interval_end))
    _curve(eoc, offer.curve)
    return tpo


def as_offer_element(
    offer: AsOffer, *, rrs_price_tag: str = "RRSFF", expiration: datetime | None = None
) -> Any:
    group, column = AS_OFFER_COLUMNS[offer.service]
    if offer.service == "RRS":
        column = rrs_price_tag
    aso = etree.Element(f"{_P}ASOffer")
    _common(aso, offer.resource_id, offer.external_ref, expiration)
    _sub(aso, f"{_P}asType", group)
    curve = _sub(aso, f"{_P}ASPriceCurve")
    _sub(curve, f"{_P}startTime", _ts(offer.interval_start))
    _sub(curve, f"{_P}endTime", _ts(offer.interval_end))
    for block in offer.blocks:
        # OnLineReserves is confirmed for the REGUP-RRS-ONNS group; its use for REGDN is UNCONFIRMED.
        row = _sub(curve, f"{_P}OnLineReserves")
        _sub(row, f"{_P}xvalue", _dec(block.mw))
        _sub(row, f"{_P}{column}", _dec(block.price_usd_per_mwh))
        _sub(row, f"{_P}block", "VARIABLE")
    return aso


# -- replies -----------------------------------------------------------------------------------------


def _find_text(el: Element, *path: str) -> str | None:
    node: Element | None = el
    for name in path:
        if node is None:
            return None
        node = node.find(name)
    return node.text.strip() if node is not None and node.text is not None else None


def parse_reply(xml: bytes) -> EwsReply:
    """Parse a SOAP response carrying an EWS `ResponseMessage` (or raise `EwsError`)."""
    try:
        root = fromstring(xml)
    except Exception as exc:  # defusedxml: ParseError / EntitiesForbidden / DTDForbidden
        raise EwsError(f"unparseable EWS reply: {exc}") from exc
    body = root.find(f"{_S}Body")
    if body is None:
        raise EwsError("SOAP reply has no Body")
    fault = body.find(f"{_S}Fault")
    if fault is not None:
        raise EwsError(f"SOAP fault: {_find_text(fault, 'faultcode')}: {_find_text(fault, 'faultstring')}")
    message = body.find(f"{_M}ResponseMessage")
    if message is None:
        raise EwsError("SOAP reply has no ResponseMessage")
    reply = message.find(f"{_M}Reply")
    code = _find_text(reply, f"{_M}ReplyCode") if reply is not None else None
    if code is None:
        raise EwsError("EWS reply without ReplyCode")
    errors = [e.text.strip() for e in reply.iter(f"{_M}Error") if e.text] if reply is not None else []
    payload_el = message.find(f"{_M}Payload")
    payload = next(iter(payload_el), None) if payload_el is not None else None
    return EwsReply(
        code=code.upper(),
        errors=errors,
        payload=payload,
        message_id=_find_text(message, f"{_M}Header", f"{_M}MessageID"),
    )


def _decimal(text: str | None, what: str) -> Decimal:
    try:
        return Decimal(text or "")
    except InvalidOperation as exc:
        raise EwsError(f"bad {what}: {text!r}") from exc


def _dt(text: str | None, what: str) -> datetime:
    if not text:
        raise EwsError(f"missing {what}")
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise EwsError(f"bad {what}: {text!r}") from exc
    if value.tzinfo is None:
        raise EwsError(f"{what} without UTC offset: {text!r}")
    return value


def parse_awards(payload: Element | None, *, trading_date: date, market: str = "DAM") -> list[Award]:
    """`AwardSet` -> awards. `AwardedEnergyOffer` (resource, startTime, endTime, awardedMWh) and
    `AwardedAS` (resource, asType, startTime, endTime, awardedMW, mcpc). Other award kinds (PTP, CRR,
    energy-only) are not ours and are skipped."""
    if payload is None:
        return []
    if payload.tag != f"{_P}AwardSet":
        raise EwsError(f"expected AwardSet, got {payload.tag}")
    awards: list[Award] = []
    for el in payload.iter(f"{_P}AwardedEnergyOffer"):
        start = _dt(_find_text(el, f"{_P}startTime"), "startTime")
        end = _dt(_find_text(el, f"{_P}endTime"), "endTime")
        mwh = _decimal(_find_text(el, f"{_P}awardedMWh"), "awardedMWh")
        hours = Decimal(str((end - start).total_seconds() / 3600.0))
        awards.append(
            Award(
                award_id=_find_text(el, f"{_P}mRID")
                or f"E|{_find_text(el, f'{_P}resource')}|{start.isoformat()}",
                kind="ENERGY",
                market="DAM" if market == "DAM" else "RTM",
                trading_date=trading_date,
                resource_id=_find_text(el, f"{_P}resource") or "",
                interval_start=start,
                interval_end=end,
                awarded_mw=(mwh / hours) if hours > 0 else mwh,
                price_usd_per_mwh=Decimal(price) if (price := _find_text(el, f"{_P}price")) else None,
            )
        )
    for el in payload.iter(f"{_P}AwardedAS"):
        as_type = (_find_text(el, f"{_P}asType") or "").upper()
        service = _AWARD_AS_TYPES.get(as_type)
        if service is None:
            raise EwsError(f"unknown AS award type {as_type!r}")
        start = _dt(_find_text(el, f"{_P}startTime"), "startTime")
        awards.append(
            Award(
                award_id=_find_text(el, f"{_P}mRID")
                or f"A|{_find_text(el, f'{_P}resource')}|{as_type}|{start.isoformat()}",
                kind="AS",
                market="DAM" if market == "DAM" else "RTM",
                trading_date=trading_date,
                resource_id=_find_text(el, f"{_P}resource") or "",
                service=service,
                interval_start=start,
                interval_end=_dt(_find_text(el, f"{_P}endTime"), "endTime"),
                awarded_mw=_decimal(_find_text(el, f"{_P}awardedMW"), "awardedMW"),
                price_usd_per_mwh=_decimal(_find_text(el, f"{_P}mcpc"), "mcpc"),
            )
        )
    return awards


def _vdi(el: Element, type_map: dict[str, str]) -> DispatchInstruction:
    """One `VDI` element -> an instruction. Raises `EwsError` / `ValueError` when it cannot be read.
    `Details/rampMinutes` and `Details/recallOf` (D-35) are UNCONFIRMED spellings, like the ack's."""
    details = el.find(f"{_P}Details")

    def detail(name: str) -> str | None:
        return _find_text(details, f"{_P}{name}") if details is not None else None

    kind = type_map.get(detail("instructionType") or "OTHER", "VDI")
    as_type = (detail("asType") or "").upper()
    service = _AWARD_AS_TYPES.get(as_type)
    if kind != "VDI" and service is None:
        raise EwsError(f"unknown asType {as_type!r}")
    mw, ramp, start, end = detail("mw"), detail("rampMinutes"), detail("startTime"), detail("endTime")
    issued = _dt(_find_text(el, f"{_P}notificationTime"), "notificationTime")
    return DispatchInstruction.model_validate(
        {
            "instruction_id": _find_text(el, f"{_P}mRID") or _find_text(el, f"{_P}vdiRefNum") or "",
            "kind": kind,
            "resource_id": _find_text(el, f"{_P}resource") or "",
            "service": service,
            "mw": _decimal(mw, "mw") if mw else None,
            "start_at": _dt(start, "startTime") if start else issued,
            "end_at": _dt(end, "endTime") if end else None,
            "issued_at": issued,
            "text": detail("instructionText"),
            "ramp_minutes": int(_decimal(ramp, "rampMinutes")) if ramp else None,
            "recalls": detail("recallOf"),
        }
    )


def parse_vdi_batch(payload: Element | None, *, type_map: dict[str, str]) -> InstructionBatch:
    """`VDIs` -> instructions plus the ones that could not be parsed (each with its id when readable), so
    one malformed instruction is rejected on its own instead of failing the whole poll. `type_map` maps
    `Details/instructionType` to AS_DEPLOYMENT | AS_RECALL; unmapped types stay kind VDI."""
    if payload is None:
        return InstructionBatch()
    instructions: list[DispatchInstruction] = []
    malformed: list[MalformedInstruction] = []
    for el in payload.iter(f"{_P}VDI"):
        try:
            instructions.append(_vdi(el, type_map))
        except (EwsError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
            instruction_id = _find_text(el, f"{_P}mRID") or _find_text(el, f"{_P}vdiRefNum")
            malformed.append(MalformedInstruction(instruction_id=instruction_id, error=str(exc)[:300]))
    return InstructionBatch(instructions=instructions, malformed=malformed)


def parse_vdis(payload: Element | None, *, type_map: dict[str, str]) -> list[DispatchInstruction]:
    """`VDIs` -> dispatch instructions; strict: raises `EwsError` on the first malformed one."""
    batch = parse_vdi_batch(payload, type_map=type_map)
    if batch.malformed:
        raise EwsError(f"malformed VDI {batch.malformed[0].instruction_id}: {batch.malformed[0].error}")
    return list(batch.instructions)


def serialize(envelope: Any) -> bytes:
    data: bytes = etree.tostring(envelope, xml_declaration=True, encoding="UTF-8")
    return data
