"""ERCOT MMS QSE submission client (`MarketSubmission` backend `ercot_mms`, protocol-adapters.md S6).

Operations (EWS verbs/nouns):

| Operation                     | Verb   | Noun      | Service (SOAPAction)                    |
|-------------------------------|--------|-----------|-----------------------------------------|
| energy / three-part offer     | create | BidSet    | MarketTransactions                      |
| AS offer                      | create | BidSet    | MarketTransactions                      |
| cancel a submission           | cancel | BidSet    | MarketTransactions                      |
| DAM/RTM awards                | get    | AwardSet  | MarketInfo (Request: MarketType, TradingDate) |
| verbal dispatch instructions  | get    | VDIs      | MarketInfo                              |
| acknowledge a VDI             | change | VDIs      | MarketTransactions                      |

Safety rules:

- OFF unless `[integrations.market].enabled = true` (the factory refuses to build it otherwise);
- a submission is POSTed exactly once. Timeouts and 5xx are reported as `ERROR` receipts, never resent
  (a blind resend could double-offer); the state is resolved by `fetch_awards` / BidSet notifications;
- GETs (awards, VDIs) retry at most twice with backoff, like the public-API feeds;
- ReplyCode OK means ERCOT accepted the BidSet for processing (status SUBMITTED): validation is
  asynchronous in MMS and reported through BidSet notifications.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import httpx
from lxml import etree  # type: ignore[import-untyped]
from pydantic import BaseModel, ConfigDict, Field

from opengrid.core.timeutil import to_market_tz
from opengrid.integrations.ercot_mms.messages import (
    PAYLOAD_NS,
    EwsError,
    EwsReply,
    RequestHeader,
    as_offer_element,
    bid_set,
    build_request,
    energy_offer_element,
    parse_awards,
    parse_reply,
    parse_vdis,
    serialize,
    three_part_offer_element,
)
from opengrid.integrations.ercot_mms.signing import MessageSigner, NullSigner, X509WsSecuritySigner
from opengrid.integrations.interfaces import (
    AsOffer,
    Award,
    DispatchInstruction,
    EnergyOffer,
    SubmissionReceipt,
    ThreePartSupplyOffer,
)
from opengrid.integrations.tls import TlsSettings, build_client_ssl_context
from opengrid.platform.config import resolve_secret

logger = logging.getLogger(__name__)

__all__ = ["ErcotMmsClient", "ErcotMmsSettings"]

_SOAP_ACTION_BASE = "http://www.ercot.com/Nodal/"


class ErcotMmsSettings(BaseModel):
    """`[integrations.market.ercot_mms]`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # Production (after 2025-12-05): https://misapi.ercot.com/NodalAPI/EWS/ ; MOTE (market trials):
    # https://testmisapi.ercot.com/2007-08/Nodal/eEDS/EWS/ ; the simulator: http://127.0.0.1:<port>/ews/
    endpoint: str
    qse_code: str = Field(min_length=1)  # EWS Header/Source
    user_id: str = Field(min_length=1)  # EWS Header/UserID (the certificate's MIS user)
    tls: TlsSettings = TlsSettings()
    signing: Literal["x509", "none"] = "x509"
    signing_algorithm: Literal["sha256", "sha1"] = "sha256"
    signing_cert_file: str | None = None  # defaults to tls.cert_file
    signing_key_file: str | None = None  # defaults to tls.key_file
    timeout_s: float = Field(default=30.0, gt=0)
    get_retries: int = Field(default=2, ge=0, le=5)
    esr_resources: list[str] = []  # resources that are ESRs (no three-part offers; RTC+B curve)
    esr_curve_element: str = "EnergyBidOfferCurve"  # UNCONFIRMED spelling (messages.py)
    rrs_price_tag: str = "RRSFF"
    offer_expiration_minutes: int | None = None
    #: VDI Details/instructionType -> AS_DEPLOYMENT | AS_RECALL. The simulator's types by default; the
    #: production mapping is agreed with ERCOT operations at onboarding.
    vdi_type_map: dict[str, Literal["AS_DEPLOYMENT", "AS_RECALL"]] = {
        "DEPLOY_AS": "AS_DEPLOYMENT",
        "RECALL_AS": "AS_RECALL",
    }


class ErcotMmsClient:
    """`MarketSubmission` over ERCOT EWS."""

    def __init__(
        self,
        settings: ErcotMmsSettings,
        *,
        client: httpx.AsyncClient | None = None,
        signer: MessageSigner | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._client = client or httpx.AsyncClient(
            verify=build_client_ssl_context(settings.tls) or True, timeout=settings.timeout_s
        )
        self._signer = signer or self._default_signer(settings)

    @staticmethod
    def _default_signer(settings: ErcotMmsSettings) -> MessageSigner:
        if settings.signing == "none":
            return NullSigner()
        cert_file = settings.signing_cert_file or settings.tls.cert_file
        key_file = settings.signing_key_file or settings.tls.key_file
        if not cert_file or not key_file:
            raise ValueError(
                "x509 signing needs signing_cert_file/signing_key_file (or tls.cert_file/key_file)"
            )
        password = (
            resolve_secret(settings.tls.key_password_env).encode() if settings.tls.key_password_env else None
        )
        return X509WsSecuritySigner(
            Path(cert_file).read_bytes(),
            Path(key_file).read_bytes(),
            key_password=password,
            algorithm=settings.signing_algorithm,
        )

    @property
    def backend(self) -> str:
        return "ercot_mms"

    # -- transport -----------------------------------------------------------------------------------

    def _header(self, verb: str, noun: str) -> RequestHeader:
        return RequestHeader(
            verb=verb,
            noun=noun,
            source=self._settings.qse_code,
            user_id=self._settings.user_id,
            message_id=str(uuid.uuid4()),
            nonce=secrets.token_hex(16),
            created=self._clock(),
        )

    async def _post(self, envelope: Any, service: str) -> bytes:
        self._signer.sign(envelope)
        response = await self._client.post(
            self._settings.endpoint,
            content=serialize(envelope),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f"{_SOAP_ACTION_BASE}{service}",
            },
        )
        if response.status_code >= 500 and b"Fault" in response.content:
            return response.content  # SOAP 1.1 faults arrive as HTTP 500 with a Fault body
        response.raise_for_status()
        return response.content

    def _expiration(self) -> datetime | None:
        minutes = self._settings.offer_expiration_minutes
        if minutes is None:
            return None
        return self._clock() + timedelta(minutes=minutes)

    async def _transact(self, header: RequestHeader, payload: Any) -> SubmissionReceipt:
        """POST one market transaction exactly once and turn the reply into a receipt."""
        envelope = build_request(header, payload=payload)
        received = self._clock()
        try:
            reply = parse_reply(await self._post(envelope, "MarketTransactions"))
        except (httpx.HTTPError, EwsError) as exc:
            # Never resent automatically (module docstring).
            logger.error(
                "ERCOT EWS transaction failed", extra={"message_id": header.message_id, "error": str(exc)}
            )
            return SubmissionReceipt(
                submission_id=header.message_id, status="ERROR", errors=[str(exc)], received_at=received
            )
        return self._receipt(header.message_id, reply, received)

    async def _submit(self, trading_date: date, transaction: Any) -> SubmissionReceipt:
        return await self._transact(self._header("create", "BidSet"), bid_set(trading_date, transaction))

    @staticmethod
    def _receipt(message_id: str, reply: EwsReply, received: datetime) -> SubmissionReceipt:
        operator_ref = None
        if reply.payload is not None:
            mrid = reply.payload.find("{http://www.ercot.com/schema/2007-06/nodal/ews}mRID")
            operator_ref = mrid.text if mrid is not None else None
        status: Literal["ACCEPTED", "REJECTED", "ERROR"] = (
            "ACCEPTED" if reply.code == "OK" else ("REJECTED" if reply.code == "ERROR" else "ERROR")
        )
        return SubmissionReceipt(
            submission_id=message_id,
            status=status,
            operator_ref=operator_ref,
            errors=reply.errors,
            received_at=received,
        )

    async def _get(self, noun: str, request_fields: dict[str, str]) -> EwsReply:
        attempt = 0
        while True:
            envelope = build_request(self._header("get", noun), request_fields=request_fields)
            try:
                reply = parse_reply(await self._post(envelope, "MarketInfo"))
            except (httpx.TransportError, httpx.HTTPStatusError):
                if attempt >= self._settings.get_retries:
                    raise
                attempt += 1
                await asyncio.sleep(0.5 * 2**attempt)
                continue
            if reply.code != "OK":
                raise EwsError(f"EWS get {noun} failed: {reply.code} {reply.errors}")
            return reply

    # -- MarketSubmission ------------------------------------------------------------------------------

    def _trading_date(self, start: datetime) -> date:
        return to_market_tz(start).date()

    async def submit_energy_offer(self, offer: EnergyOffer) -> SubmissionReceipt:
        is_esr = offer.resource_id in self._settings.esr_resources
        if offer.curve_kind == "ESR_BID_OFFER" and not is_esr:
            raise ValueError(
                f"{offer.resource_id} is not configured as an ESR; cannot send a bid/offer curve"
            )
        element = energy_offer_element(
            offer, esr_curve_element=self._settings.esr_curve_element, expiration=self._expiration()
        )
        return await self._submit(self._trading_date(offer.interval_start), element)

    async def submit_as_offer(self, offer: AsOffer) -> SubmissionReceipt:
        element = as_offer_element(
            offer, rrs_price_tag=self._settings.rrs_price_tag, expiration=self._expiration()
        )
        return await self._submit(self._trading_date(offer.interval_start), element)

    async def submit_three_part_offer(self, offer: ThreePartSupplyOffer) -> SubmissionReceipt:
        if offer.resource_id in self._settings.esr_resources:
            raise ValueError(
                "ESRs do not submit three-part supply offers under RTC+B; use an ESR bid/offer curve"
            )
        element = three_part_offer_element(offer, expiration=self._expiration())
        return await self._submit(self._trading_date(offer.interval_start), element)

    async def cancel(self, submission_id: str) -> SubmissionReceipt:
        payload = etree.Element(f"{{{PAYLOAD_NS}}}BidSet", nsmap={None: PAYLOAD_NS})
        etree.SubElement(payload, f"{{{PAYLOAD_NS}}}mRID").text = submission_id
        return await self._transact(self._header("cancel", "BidSet"), payload)

    async def fetch_awards(self, trading_date: date, market: Literal["DAM", "RTM"] = "DAM") -> list[Award]:
        reply = await self._get("AwardSet", {"MarketType": market, "TradingDate": trading_date.isoformat()})
        return parse_awards(reply.payload, trading_date=trading_date, market=market)

    async def fetch_dispatch_instructions(self, since: datetime) -> list[DispatchInstruction]:
        reply = await self._get("VDIs", {"StartTime": to_market_tz(since).isoformat(timespec="seconds")})
        instructions = parse_vdis(reply.payload, type_map=dict(self._settings.vdi_type_map))
        return [i for i in instructions if i.issued_at >= since]

    async def acknowledge_vdi(self, instruction_id: str) -> SubmissionReceipt:
        """`change VDIs` acknowledging one instruction (the acknowledgement element is UNCONFIRMED)."""
        payload = etree.Element(f"{{{PAYLOAD_NS}}}VDIs", nsmap={None: PAYLOAD_NS})
        vdi = etree.SubElement(payload, f"{{{PAYLOAD_NS}}}VDI")
        etree.SubElement(vdi, f"{{{PAYLOAD_NS}}}mRID").text = instruction_id
        etree.SubElement(vdi, f"{{{PAYLOAD_NS}}}acknowledged").text = "true"
        return await self._transact(self._header("change", "VDIs"), payload)

    async def close(self) -> None:
        await self._client.aclose()
