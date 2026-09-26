"""`opengrid.platform.watchdog`: no-op without `NOTIFY_SOCKET` (dev/test default), sends the right
datagram payload when a socket is configured, and derives the ping interval from `WATCHDOG_USEC`."""

from __future__ import annotations

import socket

import pytest

from opengrid.platform import watchdog


def test_notify_functions_are_silent_noops_without_notify_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    watchdog.notify_ready()
    watchdog.notify_watchdog()
    watchdog.notify_stopping()  # must not raise


def test_watchdog_interval_is_none_without_watchdog_usec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert watchdog.watchdog_interval_s() is None


def test_watchdog_interval_is_half_of_watchdog_usec_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "10000000")  # 10s
    assert watchdog.watchdog_interval_s() == pytest.approx(5.0)


def test_watchdog_interval_honors_a_wider_margin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "10000000")
    assert watchdog.watchdog_interval_s(margin=4) == pytest.approx(2.5)


def test_watchdog_interval_is_none_for_malformed_watchdog_usec(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WATCHDOG_USEC", "not-a-number")
    assert watchdog.watchdog_interval_s() is None


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX is POSIX-only")
def test_notify_watchdog_sends_the_expected_payload(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.settimeout(2.0)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
    try:
        watchdog.notify_watchdog()
        data, _addr = server.recvfrom(4096)
        assert data == b"WATCHDOG=1"
    finally:
        server.close()


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX is POSIX-only")
def test_notify_ready_sends_the_expected_payload(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    sock_path = str(tmp_path / "notify.sock")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(sock_path)
    server.settimeout(2.0)
    monkeypatch.setenv("NOTIFY_SOCKET", sock_path)
    try:
        watchdog.notify_ready()
        data, _addr = server.recvfrom(4096)
        assert data == b"READY=1"
    finally:
        server.close()
