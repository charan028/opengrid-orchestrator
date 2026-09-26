"""Tests for ogsim.customer.api_client against a fake HttpTransport (no live orchestrator
or Apache needed). Covers both api_mode=customer (the /og/api/customer/... routes) and
api_mode=operator_fallback (today's operator-only POST /og/api/opportunities)."""

from __future__ import annotations

from typing import Any

import pytest

from ogsim.customer.api_client import (
    AdmissionRejectedError,
    CustomerApiClient,
    HttpResult,
    OperatorFallbackUnsupportedError,
)
from ogsim.customer.config import API_MODE_CUSTOMER, API_MODE_OPERATOR_FALLBACK

AUTH = ("og-cust-dc", "secret")


class FakeHttpTransport:
    def __init__(self) -> None:
        self.gets: list[tuple[str, tuple[str, str]]] = []
        self.posts: list[tuple[str, dict[str, Any], tuple[str, str]]] = []
        self.get_result = HttpResult(200, {})
        self.post_result = HttpResult(200, {"ok": True})

    async def get(self, path: str, auth: tuple[str, str]) -> HttpResult:
        self.gets.append((path, auth))
        return self.get_result

    async def post(self, path: str, json: dict[str, Any], auth: tuple[str, str]) -> HttpResult:
        self.posts.append((path, json, auth))
        return self.post_result


async def test_submit_opportunity_posts_to_the_customer_path_and_carries_auth():
    transport = FakeHttpTransport()
    client = CustomerApiClient(transport, AUTH, mode=API_MODE_CUSTOMER)
    result = await client.submit_opportunity({"customer_id": "c1"})
    assert result == {"ok": True}
    path, payload, auth = transport.posts[0]
    assert path == "/og/api/customer/opportunities"
    assert payload == {"customer_id": "c1"}
    assert auth == AUTH


async def test_submit_opportunity_uses_the_operator_fallback_path_in_that_mode():
    transport = FakeHttpTransport()
    client = CustomerApiClient(transport, AUTH, mode=API_MODE_OPERATOR_FALLBACK)
    await client.submit_opportunity({"contract_id": "k1"})
    path, _, _ = transport.posts[0]
    assert path == "/og/api/opportunities"


async def test_submit_opportunity_raises_admission_rejected_on_409():
    transport = FakeHttpTransport()
    transport.post_result = HttpResult(409, {"reason_code": "NO_HEADROOM"})
    client = CustomerApiClient(transport, AUTH)
    with pytest.raises(AdmissionRejectedError) as excinfo:
        await client.submit_opportunity({})
    assert excinfo.value.status_code == 409
    assert excinfo.value.reason_code == "NO_HEADROOM"


async def test_submit_opportunity_raises_admission_rejected_on_any_4xx():
    transport = FakeHttpTransport()
    transport.post_result = HttpResult(422, {"reason_code": "MALFORMED"})
    client = CustomerApiClient(transport, AUTH)
    with pytest.raises(AdmissionRejectedError):
        await client.submit_opportunity({})


async def test_get_obligations_returns_the_obligations_list():
    transport = FakeHttpTransport()
    transport.get_result = HttpResult(200, {"obligations": [{"id": "o1"}]})
    client = CustomerApiClient(transport, AUTH)
    obligations = await client.get_obligations()
    assert obligations == [{"id": "o1"}]
    assert transport.gets[0] == ("/og/api/customer/obligations", AUTH)


async def test_get_invoices_returns_the_invoices_list():
    transport = FakeHttpTransport()
    transport.get_result = HttpResult(200, {"invoices": [{"id": "i1"}]})
    client = CustomerApiClient(transport, AUTH)
    invoices = await client.get_invoices()
    assert invoices == [{"id": "i1"}]


async def test_dispute_invoice_posts_to_the_dispute_path():
    transport = FakeHttpTransport()
    client = CustomerApiClient(transport, AUTH)
    await client.dispute_invoice("i1", "USAGE_MISMATCH")
    path, payload, _ = transport.posts[0]
    assert path == "/og/api/customer/invoices/i1/dispute"
    assert payload == {"reason_code": "USAGE_MISMATCH"}


async def test_cancel_obligation_posts_to_the_cancel_path():
    transport = FakeHttpTransport()
    client = CustomerApiClient(transport, AUTH)
    await client.cancel_obligation("o1")
    path, _, _ = transport.posts[0]
    assert path == "/og/api/customer/obligations/o1/cancel"


async def test_renominate_obligation_posts_the_new_window():
    transport = FakeHttpTransport()
    client = CustomerApiClient(transport, AUTH)
    await client.renominate_obligation("o1", "2026-09-26T00:00:00Z", "2026-09-26T01:00:00Z")
    path, payload, _ = transport.posts[0]
    assert path == "/og/api/customer/obligations/o1/renominate"
    assert payload == {"window_start": "2026-09-26T00:00:00Z", "window_end": "2026-09-26T01:00:00Z"}


@pytest.mark.parametrize(
    "action",
    ["get_obligations", "get_invoices"],
)
async def test_operator_fallback_mode_rejects_obligation_and_invoice_reads(action: str):
    transport = FakeHttpTransport()
    client = CustomerApiClient(transport, AUTH, mode=API_MODE_OPERATOR_FALLBACK)
    with pytest.raises(OperatorFallbackUnsupportedError):
        await getattr(client, action)()


async def test_operator_fallback_mode_rejects_dispute_cancel_and_renominate():
    transport = FakeHttpTransport()
    client = CustomerApiClient(transport, AUTH, mode=API_MODE_OPERATOR_FALLBACK)
    with pytest.raises(OperatorFallbackUnsupportedError):
        await client.dispute_invoice("i1", "reason")
    with pytest.raises(OperatorFallbackUnsupportedError):
        await client.cancel_obligation("o1")
    with pytest.raises(OperatorFallbackUnsupportedError):
        await client.renominate_obligation("o1", "s", "e")
