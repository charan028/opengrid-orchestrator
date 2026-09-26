"""Copilot tests never reach a network: every outbound IP connection fails here, so a fake that is
accidentally bypassed shows up as a test failure instead of a real (billed) API call."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from typing import Any

import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _guard(sock: socket.socket) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise OSError("network access is blocked in copilot tests")

    def connect(sock: socket.socket, address: Any) -> None:
        _guard(sock)
        real_connect(sock, address)

    def connect_ex(sock: socket.socket, address: Any) -> int:
        _guard(sock)
        return real_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    yield
