"""`[grid_link]` configuration (grid-link.md S7). Disabled by default, and enabled per utility.

    [grid_link]
    enabled = false                       # master switch; nothing listens while false
    mqtt_username = "og_gridlink"         # publishes L2 instructions on <root>/scada/instruction/#
    mqtt_password_env = "OG_MQTT_GRIDLINK_PASSWORD"

    [[grid_link.utilities]]
    utility_id = "AUSTIN_ENERGY"      # one of opengrid.core.models.market.UTILITY_IDS
    enabled = false
    protocol = "dnp3"
    listen_host = "0.0.0.0"
    listen_port = 20001
    allowed_peers = ["10.20.30.0/28"]     # peer allow-list (IP or CIDR); required
    allowed_peer_cns = ["aen-ems-1"]      # TLS client-certificate CN allow-list; required with TLS
    banks = ["bank-040", "bank-041"]      # the utility's banks, in point order
    heartbeat_timeout_s = 30
    max_setpoint_kw = 26000
    [[grid_link.utilities.l2_targets]]    # L2 LIMIT/BLOCK targets, in point order
    name = "LZ_AEN"
    zone = "LZ_AEN"
    [grid_link.utilities.tls]
    enabled = true
    cert_file = "/etc/opengrid/certs/og-gridlink.pem"
    key_file = "/etc/opengrid/certs/og-gridlink.key"
    client_ca_file = "/etc/opengrid/certs/aen-ems-ca.pem"

Security rules enforced at load (fail closed): a utility may listen on a non-loopback address only with
TLS on and a non-empty CN allow-list; every utility needs a non-empty peer allow-list.
"""

from __future__ import annotations

import ipaddress
import os
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opengrid.core.models.market import UTILITY_IDS
from opengrid.integrations.tls import ServerTlsSettings

__all__ = [
    "MAX_BANKS",
    "MAX_TARGETS",
    "OVERRIDE_ENV",
    "Dnp3LinkSettings",
    "GridLinkSettings",
    "L2TargetSettings",
    "UtilityLinkSettings",
    "grid_link_table",
    "load_grid_link_settings",
]

#: Point-list limits (interfaces/grid_link/opengrid-gridlink-v1.json `limits`).
MAX_TARGETS = 64
MAX_BANKS = 200
#: D-29: a TOLLING call is capped at 90 minutes by its product rule; the link never asks for more.
TOLLING_MAX_DURATION_MIN = 90


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class L2TargetSettings(_Model):
    """One L2 LIMIT/BLOCK target: exactly one bank or one zone (expanded to its banks at runtime)."""

    name: str = Field(min_length=1, max_length=64)
    bank_id: str | None = None
    zone: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> L2TargetSettings:
        if (self.bank_id is None) == (self.zone is None):
            raise ValueError(f"L2 target {self.name!r}: set exactly one of bank_id or zone")
        return self


class Dnp3LinkSettings(_Model):
    """DNP3 outstation parameters for one utility association."""

    outstation_address: int = Field(default=10, ge=0, le=65519)
    master_address: int = Field(default=1, ge=0, le=65519)
    select_timeout_s: float = Field(default=10.0, gt=0)
    control_mode: Literal["sbo_or_direct", "sbo_only"] = "sbo_or_direct"
    max_fragment_size: int = Field(default=2048, ge=249, le=65535)
    max_associations: int = Field(default=2, ge=1, le=8)  # primary + backup control centre


class UtilityLinkSettings(_Model):
    """One utility's grid-control link."""

    utility_id: str = Field(min_length=1, max_length=32)
    enabled: bool = False
    protocol: Literal["dnp3"] = "dnp3"
    listen_host: str = "127.0.0.1"
    listen_port: int = Field(ge=0, le=65535)  # 0 = an OS-assigned port (tests)
    allowed_peers: list[str] = Field(min_length=1)
    allowed_peer_cns: list[str] = []
    banks: list[str] = Field(min_length=1, max_length=MAX_BANKS)
    l2_targets: list[L2TargetSettings] = Field(default=[], max_length=MAX_TARGETS)
    accept_toll_calls: bool = True
    accept_l2: bool = True
    heartbeat_timeout_s: float = Field(default=30.0, gt=0)
    max_setpoint_kw: float = Field(gt=0)
    max_duration_min: int = Field(default=TOLLING_MAX_DURATION_MIN, ge=1, le=TOLLING_MAX_DURATION_MIN)
    status_refresh_s: float = Field(default=2.0, gt=0)
    core_timeout_s: float = Field(default=5.0, gt=0)
    issued_by: str = "UTILITY_GRID_LINK"
    tls: ServerTlsSettings = ServerTlsSettings()
    dnp3: Dnp3LinkSettings = Dnp3LinkSettings()

    @field_validator("allowed_peers")
    @classmethod
    def _valid_networks(cls, value: list[str]) -> list[str]:
        for item in value:
            ipaddress.ip_network(item, strict=False)  # raises ValueError on a malformed entry
        return value

    @model_validator(mode="after")
    def _secure_listener(self) -> UtilityLinkSettings:
        loopback = ipaddress.ip_address(self.listen_host).is_loopback if _is_ip(self.listen_host) else False
        if not loopback and not self.tls.enabled:
            raise ValueError(f"grid link {self.utility_id}: a non-loopback listener requires TLS")
        if self.tls.enabled and not self.allowed_peer_cns:
            raise ValueError(f"grid link {self.utility_id}: TLS requires a non-empty allowed_peer_cns")
        names = [t.name for t in self.l2_targets]
        if len(names) != len(set(names)):
            raise ValueError(f"grid link {self.utility_id}: duplicate L2 target name")
        if len(self.banks) != len(set(self.banks)):
            raise ValueError(f"grid link {self.utility_id}: duplicate bank id")
        return self

    def peer_allowed(self, host: str) -> bool:
        """True when `host` (an IP address string) is inside one of `allowed_peers`."""
        if not _is_ip(host):
            return False
        address = ipaddress.ip_address(host)
        return any(address in ipaddress.ip_network(net, strict=False) for net in self.allowed_peers)


def _is_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
    except ValueError:
        return False
    return True


class GridLinkSettings(_Model):
    """`[grid_link]`."""

    enabled: bool = False
    mqtt_username: str = "og_gridlink"
    mqtt_password_env: str = "OG_MQTT_GRIDLINK_PASSWORD"  # noqa: S105 -- an env-var NAME, not a secret
    utilities: list[UtilityLinkSettings] = []

    @model_validator(mode="after")
    def _unique(self) -> GridLinkSettings:
        ids = [u.utility_id for u in self.utilities]
        ports = [(u.listen_host, u.listen_port) for u in self.utilities]
        if len(ids) != len(set(ids)):
            raise ValueError("[grid_link]: duplicate utility_id")
        if len(ports) != len(set(ports)):
            raise ValueError("[grid_link]: two utilities on the same listen address")
        return self

    def active_utilities(self) -> list[UtilityLinkSettings]:
        """The utilities that actually listen: the master switch and the utility's own switch on, and a
        utility id the orchestrator knows (`UTILITY_IDS`). An unknown id is skipped here, not rejected at
        load, so one entry awaiting another lane's id never takes the other utilities down."""
        if not self.enabled:
            return []
        return [u for u in self.utilities if u.enabled and utility_known(u.utility_id)]

    def unknown_utilities(self) -> list[str]:
        """Enabled entries whose utility id is not in `UTILITY_IDS` (logged by the runner)."""
        return [u.utility_id for u in self.utilities if u.enabled and not utility_known(u.utility_id)]


def utility_known(utility_id: str) -> bool:
    """True when `utility_id` is one of `opengrid.core.models.market.UTILITY_IDS` (the ids the core call
    function resolves tolling obligations by)."""
    return utility_id in UTILITY_IDS


#: Env var naming a host-local TOML file whose `[grid_link]` table REPLACES the release config's (so enabling
#: a utility on a host survives deploys; written by deploy/scripts/grid_link_enable_loopback.sh).
OVERRIDE_ENV = "OG_GRID_LINK_CONFIG"


def grid_link_table(release_table: Mapping[str, Any] | None) -> Mapping[str, Any] | None:
    """The `[grid_link]` table to use: the override file's when `OG_GRID_LINK_CONFIG` is set, else the
    release config's. A set-but-unreadable override raises (`OSError` / `ValueError`): the caller disables
    the link rather than silently falling back to another configuration."""
    path = os.environ.get(OVERRIDE_ENV, "").strip()
    if not path:
        return release_table
    with Path(path).open("rb") as handle:
        data = tomllib.load(handle)
    table = data.get("grid_link")
    if not isinstance(table, dict):
        raise ValueError(f"{OVERRIDE_ENV}={path} has no [grid_link] table")
    return table


def load_grid_link_settings(raw: Mapping[str, Any] | None) -> GridLinkSettings:
    """Validate `cfg.get("grid_link")`; `None` gives the defaults (disabled)."""
    return GridLinkSettings.model_validate(dict(raw or {}))
