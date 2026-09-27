"""D-35: the `ercot_mms` adapter as a `DispatchInstructionSource` -- lenient batch parsing (one malformed
instruction never hides the others), ramp/recall fields, and ACCEPT/REJECT acknowledgements, against the
ogsim MMS simulator on the wire (the sims share nothing with the orchestrator but the wire)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from defusedxml.ElementTree import fromstring

from opengrid.integrations.ercot_mms.client import ErcotMmsClient, ErcotMmsSettings
from opengrid.integrations.ercot_mms.messages import EwsError, parse_vdi_batch, parse_vdis
from opengrid.integrations.interfaces import DispatchInstructionSource

mms = pytest.importorskip("ogsim.protocols.ercot_mms")

PAY = "http://www.ercot.com/schema/2007-06/nodal/ews"
TYPES = {"DEPLOY_AS": "AS_DEPLOYMENT", "RECALL_AS": "AS_RECALL"}


def _vdi(mrid: str, details: str, notified: str = "2026-09-27T00:00:05-05:00") -> str:
    return (
        f"<VDI><mRID>{mrid}</mRID><resource>OG_ESR_1</resource><notificationTime>{notified}</notificationTime>"
        f"<Details>{details}</Details></VDI>"
    )


GOOD = (
    "<instructionType>DEPLOY_AS</instructionType><asType>ECRS</asType><mw>0.5</mw>"
    "<startTime>2026-09-27T00:00:00-05:00</startTime><endTime>2026-09-27T00:30:00-05:00</endTime>"
    "<rampMinutes>10</rampMinutes>"
)


def test_a_malformed_instruction_is_isolated_with_its_id() -> None:
    payload = fromstring(
        f'<VDIs xmlns="{PAY}">'
        + _vdi("A", GOOD)
        + _vdi("B", "<instructionType>DEPLOY_AS</instructionType><asType>ECRS</asType><mw>fifty</mw>")
        + _vdi(
            "C",
            "<instructionType>DEPLOY_AS</instructionType><asType>ECRS</asType><startTime>soon</startTime>",
        )
        + _vdi("D", "<instructionType>DEPLOY_AS</instructionType><asType>XYZ</asType>")
        + _vdi("E", GOOD.replace("00:30:00", "00:00:00"))  # ends when it starts
        + _vdi("F", "<instructionType>RECALL_AS</instructionType><asType>ECRS</asType><recallOf>A</recallOf>")
        + "</VDIs>"
    )
    batch = parse_vdi_batch(payload, type_map=TYPES)
    assert [i.instruction_id for i in batch.instructions] == ["A", "F"]
    deploy, recall = batch.instructions
    assert (deploy.service, deploy.mw, deploy.ramp_minutes) == ("ECRS", Decimal("0.5"), 10)
    assert deploy.end_at - deploy.start_at == timedelta(minutes=30)
    assert (recall.kind, recall.recalls) == ("AS_RECALL", "A")
    assert [m.instruction_id for m in batch.malformed] == ["B", "C", "D", "E"]
    with pytest.raises(EwsError):
        parse_vdis(payload, type_map=TYPES)


async def test_the_client_is_a_dispatch_instruction_source_that_acknowledges_accept_and_reject() -> None:
    state = mms.MmsSimState()
    now = datetime.now(UTC)
    good = state.dispatch.publish(
        resource="OG_ESR_1", instruction_type="DEPLOY_AS", issued_at=now, as_type="ECRS", mw=0.5,
        end=now + timedelta(minutes=30), ramp_minutes=10,
    )  # fmt: skip
    bad = state.dispatch.publish(
        resource="OG_ESR_1", instruction_type="DEPLOY_AS", issued_at=now, as_type="ECRS", mw=0.5,
        malformed_fields={"mw": "fifty"},
    )  # fmt: skip
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mms.create_app(state)), base_url="http://mms.test"
    )
    client = ErcotMmsClient(
        ErcotMmsSettings(endpoint="http://mms.test/ews/", qse_code="QOPENGRID", user_id="u", signing="none"),
        client=http,
    )
    try:
        assert isinstance(client, DispatchInstructionSource)
        batch = await client.fetch_instruction_batch(now - timedelta(minutes=15))
        assert [i.instruction_id for i in batch.instructions] == [good.mrid]
        assert [m.instruction_id for m in batch.malformed] == [bad.mrid]
        assert (await client.acknowledge_instruction(good.mrid, accepted=True, reason=None)).accepted
        rejected = await client.acknowledge_instruction(bad.mrid, accepted=False, reason="422 malformed")
        assert rejected.accepted  # the operator accepted our REJECT answer
        answers = {r["mrid"]: (r["response"], r["reason"]) for r in state.dispatch.responses()}
        assert answers == {good.mrid: ("ACCEPTED", None), bad.mrid: ("REJECTED", "422 malformed")}
        assert (await client.fetch_instruction_batch(now - timedelta(minutes=15))).instructions == []
    finally:
        await client.close()


def _mw_vdi(mrid: str, mw: str | None) -> str:
    mw_el = f"<mw>{mw}</mw>" if mw is not None else ""
    return _vdi(mrid, GOOD.replace("<mw>0.5</mw>", mw_el))


def test_an_as_deployment_without_a_positive_mw_is_malformed_never_the_full_award() -> None:
    """Review finding (r3.4.2): mw 0 parsed to Decimal 0 and reached the core as requested_kw None, which
    deploys the FULL award. ERCOT always states the MW: 0, negative or missing is a 422 malformed instruction."""
    payload = fromstring(
        f'<VDIs xmlns="{PAY}">'
        + _mw_vdi("ZERO", "0")
        + _mw_vdi("NEG", "-0.2")
        + _mw_vdi("MISSING", None)
        + _mw_vdi("PARTIAL", "0.25")
        + "</VDIs>"
    )
    batch = parse_vdi_batch(payload, type_map=TYPES)
    assert [(i.instruction_id, i.mw) for i in batch.instructions] == [("PARTIAL", Decimal("0.25"))]
    assert sorted(m.instruction_id or "" for m in batch.malformed) == ["MISSING", "NEG", "ZERO"]


async def test_the_core_gateway_never_sends_a_deployment_without_a_positive_mw() -> None:
    from uuid import uuid4

    from opengrid.contracts.as_deployment_poll import R_MALFORMED
    from opengrid.contracts.as_deployment_poll_io import CoreCallGateway
    from opengrid.integrations.interfaces import DispatchInstruction

    class _Cfg:
        def get(self, key: str, default: object = None) -> object:
            return default

    gateway = CoreCallGateway(None, None, _Cfg())  # type: ignore[arg-type]  -- refused before any I/O
    now = datetime.now(UTC)
    for mw in (None, Decimal("0")):
        instruction = DispatchInstruction.model_construct(
            instruction_id="X", kind="AS_DEPLOYMENT", resource_id="OG_ESR_1", service="ECRS", mw=mw,
            start_at=now, end_at=now + timedelta(minutes=30), issued_at=now, text=None, ramp_minutes=None,
            recalls=None,
        )  # fmt: skip
        outcome = await gateway.deploy(
            instruction, obligation_id=uuid4(), principal="ercot:ercot_mms", now=now
        )
        assert (outcome.accepted, outcome.http_status, outcome.reason_code) == (False, 422, R_MALFORMED)
