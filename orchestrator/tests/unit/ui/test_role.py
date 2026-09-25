"""Tests for `opengrid.ui.role` (BUILD.md code-review round item 5): an `X-OG-Role` header present but
not one of the known role names is logged (structured, no secrets) and defaults to viewer, rather than
silently defaulting with no trace of the bad value."""

from __future__ import annotations

import logging

import pytest
from starlette.datastructures import Headers
from starlette.requests import Request

from opengrid.ui.role import is_operator, role_of


def _request_with_role_header(value: str | None) -> Request:
    headers = Headers({"x-og-role": value} if value is not None else {})
    scope = {"type": "http", "headers": headers.raw, "method": "GET", "path": "/"}
    return Request(scope)


def test_role_of_defaults_to_viewer_with_no_header() -> None:
    assert role_of(_request_with_role_header(None)) == "viewer"


def test_role_of_recognizes_operator() -> None:
    assert role_of(_request_with_role_header("operator")) == "operator"
    assert is_operator(_request_with_role_header("operator")) is True


def test_role_of_defaults_to_viewer_and_logs_a_warning_for_an_unknown_role(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="opengrid.ui.role"):
        role = role_of(_request_with_role_header("admin"))

    assert role == "viewer"
    assert len(caplog.records) == 1
    assert "admin" in caplog.records[0].getMessage()
    assert caplog.records[0].levelno == logging.WARNING


def test_role_of_does_not_log_for_a_known_role(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="opengrid.ui.role"):
        role_of(_request_with_role_header("viewer"))

    assert caplog.records == []
