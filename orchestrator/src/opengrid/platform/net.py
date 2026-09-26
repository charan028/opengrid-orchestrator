"""Small network helpers shared by the processes that bind a local port (og-api, og-engine /metrics)."""

from __future__ import annotations

import ipaddress


def is_loopback_host(host: str) -> bool:
    """True for `localhost` and any loopback address (127.0.0.0/8, ::1); False otherwise."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False
