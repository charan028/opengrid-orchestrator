"""ERCOT MMS/EWS submission client against the ogsim MMS simulator, fully in-process (ASGI transport),
including WS-Security X.509 signing verified by the simulator."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from opengrid.integrations.ercot_mms.client import ErcotMmsClient, ErcotMmsSettings
from opengrid.integrations.ercot_mms.intake import (
    AwardContractMap,
    apply_awards,
    awards_to_intake,
    deployments_for,
)
from opengrid.integrations.ercot_mms.messages import EwsError, parse_reply
from opengrid.integrations.ercot_mms.signing import X509WsSecuritySigner
from opengrid.integrations.interfaces import AsOffer, EnergyOffer, OfferCurvePoint, ThreePartSupplyOffer

mms = pytest.importorskip("ogsim.protocols.ercot_mms")

# 2026-09-27 hour ending 18 (17:00-18:00 CDT)
START = datetime(2026, 9, 27, 22, 0, tzinfo=UTC)
END = START + timedelta(hours=1)
TRADING_DATE = date(2026, 9, 27)


def _pem_pair() -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "QOPENGRID-API")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


def _client(state: Any, *, signed: bool = False, **overrides: Any) -> ErcotMmsClient:
    app = mms.create_app(state)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mms.test")
    settings = ErcotMmsSettings(
        endpoint="http://mms.test/ews/",
        qse_code="QOPENGRID",
        user_id="og-api",
        signing="none",
        esr_resources=["OG_ESR_1", "OG_ESR_2"],
        **overrides,
    )
    signer = X509WsSecuritySigner(*_pem_pair()) if signed else None
    return ErcotMmsClient(settings, client=http, signer=signer)


def _esr_offer(resource: str = "OG_ESR_1") -> EnergyOffer:
    return EnergyOffer(
        resource_id=resource,
        market="DAM",
        curve_kind="ESR_BID_OFFER",
        interval_start=START,
        interval_end=END,
        curve=[
            OfferCurvePoint(mw=Decimal("-5"), price_usd_per_mwh=Decimal("20")),
            OfferCurvePoint(mw=Decimal("2"), price_usd_per_mwh=Decimal("30")),
            OfferCurvePoint(mw=Decimal("8"), price_usd_per_mwh=Decimal("60")),
        ],
    )


async def test_signed_submission_to_award_to_intake() -> None:
    state = mms.MmsSimState(require_signature=True)
    client = _client(state, signed=True)
    try:
        receipt = await client.submit_energy_offer(_esr_offer())
        assert receipt.accepted, receipt.errors
        assert receipt.operator_ref is not None and receipt.operator_ref.startswith("QOPENGRID.20260927.")
        as_receipt = await client.submit_as_offer(
            AsOffer(
                resource_id="OG_ESR_2",
                market="DAM",
                service="ECRS",
                interval_start=START,
                interval_end=END,
                blocks=[
                    OfferCurvePoint(mw=Decimal("3"), price_usd_per_mwh=Decimal("5")),
                    OfferCurvePoint(mw=Decimal("6"), price_usd_per_mwh=Decimal("12")),
                ],
            )
        )
        assert as_receipt.accepted, as_receipt.errors
        awards = await client.fetch_awards(TRADING_DATE)
    finally:
        await client.close()
    energy = next(a for a in awards if a.kind == "ENERGY")
    assert (energy.resource_id, energy.awarded_mw, energy.price_usd_per_mwh) == (
        "OG_ESR_1",
        Decimal("2"),
        Decimal("35"),
    )
    assert energy.interval_start == START and energy.interval_end == END
    ecrs = next(a for a in awards if a.kind == "AS")
    assert (ecrs.service, ecrs.awarded_mw, ecrs.price_usd_per_mwh) == ("ECRS", Decimal("3"), Decimal("10"))

    contract_energy, contract_ecrs = uuid4(), uuid4()
    mapping = AwardContractMap(
        energy={"OG_ESR_1": contract_energy}, ancillary={"OG_ESR_2:ECRS": contract_ecrs}
    )
    admitted: list[tuple[Any, ...]] = []

    async def fake_admit(contract_id: Any, start: Any, end: Any, kw: Decimal, *, value_per_mwh: Any) -> str:
        admitted.append((contract_id, start, end, kw, value_per_mwh))
        return "opportunity"

    results = await apply_awards(awards, mapping, fake_admit)
    assert [r for _, r in results] == ["opportunity", "opportunity"]
    assert (contract_energy, START, END, Decimal("2000.0"), Decimal("35")) in admitted
    assert (contract_ecrs, START, END, Decimal("3000.0"), Decimal("10")) in admitted


async def test_unsigned_request_is_rejected_when_signature_required() -> None:
    client = _client(mms.MmsSimState(require_signature=True))
    try:
        receipt = await client.submit_energy_offer(_esr_offer())
    finally:
        await client.close()
    assert receipt.status == "ERROR"
    assert "authentication failed" in receipt.errors[0]


async def test_tampered_signed_body_fails_verification() -> None:
    from lxml import etree

    from opengrid.integrations.ercot_mms.messages import (
        RequestHeader,
        bid_set,
        build_request,
        energy_offer_element,
    )

    signer = X509WsSecuritySigner(*_pem_pair())
    header = RequestHeader(
        verb="create", noun="BidSet", source="QOPENGRID", user_id="u", message_id="m", nonce="n1",
        created=datetime.now(UTC),
    )  # fmt: skip
    envelope = build_request(header, payload=bid_set(TRADING_DATE, energy_offer_element(_esr_offer())))
    signer.sign(envelope)
    raw = etree.tostring(envelope)
    assert mms._verify_signature(raw) is None
    assert mms._verify_signature(raw.replace(b"<xvalue>8</xvalue>", b"<xvalue>80</xvalue>")) is not None


async def test_validation_errors_and_replay() -> None:
    state = mms.MmsSimState()
    client = _client(state)
    try:
        bad = await client.submit_three_part_offer(
            ThreePartSupplyOffer(
                resource_id="UNKNOWN_GEN",
                market="DAM",
                interval_start=START,
                interval_end=END,
                startup_cost_usd=Decimal("100"),
                min_energy_cost_usd_per_mwh=Decimal("20"),
                energy_curve=[OfferCurvePoint(mw=Decimal("10"), price_usd_per_mwh=Decimal("25"))],
            )
        )
        assert bad.status == "REJECTED" and "unknown resource" in bad.errors[0]
        good = await client.submit_three_part_offer(
            ThreePartSupplyOffer(
                resource_id="OG_GEN_1",
                market="DAM",
                interval_start=START,
                interval_end=END,
                startup_cost_usd=Decimal("100"),
                min_energy_cost_usd_per_mwh=Decimal("20"),
                energy_curve=[OfferCurvePoint(mw=Decimal("10"), price_usd_per_mwh=Decimal("25"))],
            )
        )
        assert good.accepted
        cancelled = await client.cancel(good.operator_ref or "")
        assert cancelled.accepted
        assert await client.fetch_awards(TRADING_DATE) == []
        with pytest.raises(ValueError, match="ESRs do not submit three-part"):
            await client.submit_three_part_offer(
                ThreePartSupplyOffer(
                    resource_id="OG_ESR_1",
                    market="DAM",
                    interval_start=START,
                    interval_end=END,
                    startup_cost_usd=Decimal("0"),
                    min_energy_cost_usd_per_mwh=Decimal("0"),
                    energy_curve=[OfferCurvePoint(mw=Decimal("1"), price_usd_per_mwh=Decimal("1"))],
                )
            )
    finally:
        await client.close()


async def test_replayed_request_is_refused() -> None:
    from opengrid.integrations.ercot_mms.messages import (
        RequestHeader,
        bid_set,
        build_request,
        energy_offer_element,
        serialize,
    )

    app = mms.create_app(mms.MmsSimState())
    header = RequestHeader(
        verb="create", noun="BidSet", source="QOPENGRID", user_id="u", message_id="m", nonce="same-nonce",
        created=datetime.now(UTC),
    )  # fmt: skip
    raw = serialize(build_request(header, payload=bid_set(TRADING_DATE, energy_offer_element(_esr_offer()))))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mms.test") as http:
        first = parse_reply((await http.post("/ews/", content=raw)).content)
        second = parse_reply((await http.post("/ews/", content=raw)).content)
    assert first.code == "OK"
    assert second.code == "ERROR" and "replay" in second.errors[0]


async def test_charging_award_and_price_sensitivity() -> None:
    state = mms.MmsSimState(spp=Decimal("15"))
    client = _client(state)
    try:
        assert (await client.submit_energy_offer(_esr_offer())).accepted
        awards = await client.fetch_awards(TRADING_DATE)
    finally:
        await client.close()
    assert [(a.kind, a.awarded_mw) for a in awards] == [("ENERGY", Decimal("-5"))]
    intakes, skipped = awards_to_intake(awards, AwardContractMap(energy={"OG_ESR_1": uuid4()}))
    assert intakes == [] and skipped == awards  # charging is not an obligation


async def test_vdi_as_deployment_flow() -> None:
    state = mms.MmsSimState()
    app = mms.create_app(state)
    http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mms.test")
    await http.post(
        "/admin/vdis",
        json={"resource": "OG_ESR_2", "instructionType": "DEPLOY_AS", "asType": "ECRS", "mw": 3,
              "startTime": "2026-09-27T17:05:00-05:00", "endTime": "2026-09-27T17:35:00-05:00",
              "text": "Deploy ECRS"},
    )  # fmt: skip
    await http.post("/admin/vdis", json={"resource": "OG_ESR_1", "instructionType": "COMMIT", "text": "hold"})
    client = ErcotMmsClient(
        ErcotMmsSettings(endpoint="http://mms.test/ews/", qse_code="QOPENGRID", user_id="u", signing="none"),
        client=http,
    )
    try:
        instructions = await client.fetch_dispatch_instructions(datetime(2000, 1, 1, tzinfo=UTC))
        deploy = next(i for i in instructions if i.kind == "AS_DEPLOYMENT")
        assert (deploy.service, deploy.mw) == ("ECRS", Decimal("3"))
        assert {i.kind for i in instructions} == {"AS_DEPLOYMENT", "VDI"}
        rows = deployments_for(instructions)
        assert len(rows) == 1
        assert rows[0].start_at == datetime(2026, 9, 27, 22, 5, tzinfo=UTC)
        assert rows[0].end_at - rows[0].start_at == timedelta(minutes=30)
        assert rows[0].source == "MARKET_SIM" and rows[0].obligation_id is None
        ack = await client.acknowledge_vdi(deploy.instruction_id)
        assert ack.accepted
        remaining = await client.fetch_dispatch_instructions(datetime(2000, 1, 1, tzinfo=UTC))
        assert [i.kind for i in remaining] == ["VDI"]
    finally:
        await client.close()


async def test_transport_failure_is_an_error_receipt_never_a_resend() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ConnectTimeout("down", request=request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = ErcotMmsClient(
        ErcotMmsSettings(
            endpoint="http://mms.test/ews/",
            qse_code="Q",
            user_id="u",
            signing="none",
            esr_resources=["OG_ESR_1"],
        ),
        client=http,
    )
    try:
        receipt = await client.submit_energy_offer(_esr_offer())
    finally:
        await client.close()
    assert receipt.status == "ERROR" and calls == 1


def test_offer_models_validate_curves() -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        EnergyOffer(
            resource_id="r", market="DAM", interval_start=START, interval_end=END,
            curve=[OfferCurvePoint(mw=Decimal("2"), price_usd_per_mwh=Decimal("1")),
                   OfferCurvePoint(mw=Decimal("1"), price_usd_per_mwh=Decimal("2"))],
        )  # fmt: skip
    with pytest.raises(ValueError, match="negative MW"):
        EnergyOffer(
            resource_id="r", market="DAM", interval_start=START, interval_end=END,
            curve=[OfferCurvePoint(mw=Decimal("-2"), price_usd_per_mwh=Decimal("1"))],
        )  # fmt: skip


def test_parse_reply_rejects_faults_and_entities() -> None:
    fault = (
        b'<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"><soapenv:Body>'
        b"<soapenv:Fault><faultcode>soapenv:Client</faultcode><faultstring>nope</faultstring></soapenv:Fault>"
        b"</soapenv:Body></soapenv:Envelope>"
    )
    with pytest.raises(EwsError, match="nope"):
        parse_reply(fault)
    with pytest.raises(EwsError, match="unparseable"):
        parse_reply(b'<?xml version="1.0"?><!DOCTYPE a [<!ENTITY b "c">]><a>&b;</a>')
