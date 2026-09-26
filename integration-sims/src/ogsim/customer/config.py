"""ogsim.customer.config -- YAML+env configuration for ogsim.customer.

Reuses `ogsim.common.config`'s env-over-YAML precedence and production-topic-root/
production-MQTT-credential refusal (`resolve_topic_root`, `resolve_mqtt_credentials`,
`is_production`, `WorkspaceConfigError`) exactly as `ogsim.fleet`/`ogsim.scada` do --
this module never re-implements that logic, only builds `CustomerConfig` on top of it
(BUILD.md "no duplication"). Defined in `ogsim.customer`, not `ogsim.common`, since
`ogsim/common/` is owned by a different agent lane; only its already-public helpers are
imported here.

One MQTT identity for every customer operator in this process: `og_sim_customer`
(`OG_MQTT_CUSTOMER_PASSWORD`), ACL-scoped to publish `site/#` and `corridor/#` only.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ogsim.common.config import (
    MqttSettings,
    load_yaml_file,
    resolve_mqtt_credentials,
    resolve_topic_root,
)

#: `integration-sims/` of this install (src/ogsim/customer/config.py -> parents[3]).
INSTALL_DIR = Path(__file__).resolve().parents[3]
DEFAULT_CUSTOMER_CONFIG_PATH = INSTALL_DIR / "config" / "customer.yaml"

#: BUILD.md "Performance": negligible load on a shared host -- about 1 request/min per
#: customer by default.
DEFAULT_REQUEST_INTERVAL_S: float = 60.0
DEFAULT_METER_INTERVAL_S: float = 2.0
DEFAULT_POLL_INTERVAL_S: float = 60.0

#: `[api.roles]` role a real orchestrator deployment must define for these callers
#: (see this package's README / the agent's final report); `operator_fallback` targets
#: today's operator-only `/og/api/opportunities` endpoint instead, for use before the
#: customer role/routes exist.
API_MODE_CUSTOMER = "customer"
API_MODE_OPERATOR_FALLBACK = "operator_fallback"

#: The Apache-fronted base URL the sim calls THROUGH -- never the orchestrator's loopback
#: port directly (lead coordination: Apache turns the HTTP Basic identity below into the
#: trusted `X-Remote-User` the orchestrator's API reads; a caller-set header would be
#: spoofable). `OGSIM_CUSTOMER_API_BASE` in `/etc/opengrid/customer_sim.env`.
API_BASE_URL_ENV_VAR = "OGSIM_CUSTOMER_API_BASE"

#: service_profile -> Apache basic-auth account group (`og-cust-<group>`, owner-provisioned
#: dev accounts). Credentials for group G live in env vars
#: `OGSIM_CUSTOMER_<G>_USER`/`OGSIM_CUSTOMER_<G>_PASSWORD`.
_DEFAULT_AUTH_GROUP_BY_SERVICE_PROFILE: dict[str, str] = {
    "DATA_CENTER": "DC",
    "PIPELINE_AC": "PIPE",
    "ERCOT_ENERGY": "ERCOT",
    "ERCOT_AS": "ERCOT",
    "DIST_DEFERRAL": "DIST",
    "PARTNER_CAPACITY": "PARTNER",
    # docs/orchestrator/07-delivery/14-additional-services.md: og-cust-pjm / -mobile / -largeld.
    "PJM_CAPACITY": "PJM",
    "MOBILE_STORAGE": "MOBILE",
    "LARGE_LOAD": "LARGELD",
}

#: Profiles whose customer publishes a site meter at its point of common coupling
#: (`customer_site_meter.schema.json`): the DATA_CENTER closed loop, and the MOBILE_STORAGE /
#: LARGE_LOAD deployments' site meters (14-additional-services.md). PJM_CAPACITY is driven by the
#: simulated ISO instruction, not a customer meter.
SITE_METER_PROFILES: frozenset[str] = frozenset({"DATA_CENTER", "MOBILE_STORAGE", "LARGE_LOAD"})


class CustomerCredentialsError(RuntimeError):
    """Raised when a customer operator's Apache basic-auth credentials are not set in the
    environment. Never includes the (missing) password value."""


def default_auth_group(service_profile: str) -> str:
    """The Apache basic-auth account group for `service_profile`, or "" if unmapped (an
    unmapped profile must set `CustomerSiteSpec.auth_group` explicitly)."""
    return _DEFAULT_AUTH_GROUP_BY_SERVICE_PROFILE.get(service_profile, "")


def resolve_customer_credentials(auth_group: str) -> tuple[str, str]:
    """Reads the HTTP Basic (username, password) for `auth_group` from
    `OGSIM_CUSTOMER_<GROUP>_USER`/`OGSIM_CUSTOMER_<GROUP>_PASSWORD`. Never logs the
    password; raises `CustomerCredentialsError` (naming only the missing variable, never a
    value) if either is unset."""
    group = auth_group.strip().upper()
    user_var, password_var = f"OGSIM_CUSTOMER_{group}_USER", f"OGSIM_CUSTOMER_{group}_PASSWORD"
    username, password = os.environ.get(user_var, ""), os.environ.get(password_var, "")
    if not username or not password:
        raise CustomerCredentialsError(f"{user_var}/{password_var} must both be set for auth group {group!r}")
    return username, password


def customer_mqtt_settings_from_env(raw: dict[str, Any]) -> MqttSettings:
    username, password = resolve_mqtt_credentials("og_sim_customer", "OG_MQTT_CUSTOMER_PASSWORD")
    return MqttSettings(
        host=str(os.environ.get("OG_MQTT_HOST") or raw.get("host", "127.0.0.1")),
        port=int(os.environ.get("OG_MQTT_PORT") or raw.get("port", 1883)),
        username=username,
        password=password,
        topic_root=resolve_topic_root(raw),
    )


@dataclass(frozen=True)
class CustomerSiteSpec:
    """One simulated operator: a customer/service pair, with the site or corridor
    identity its closed-loop signals publish under (06-service-profiles-and-power-quality.md).

    `service_profile` is one of PIPELINE_AC, DATA_CENTER, ERCOT_ENERGY, ERCOT_AS,
    DIST_DEFERRAL, PARTNER_CAPACITY. `site_id` is used by DATA_CENTER (site meter);
    `corridor_id`/`line_id` by PIPELINE_AC (corridor current). Fleet-aware sizing:
    `baseline_kw`/`request_kw` should respect the 2,000-hub / 39.2 kWh-11 kW-per-unit /
    40x600 kVA-bank fleet BUILD.md sizes this simulator's counterpart requests against.
    """

    customer_id: str
    service_profile: str
    contract_id: str = ""
    site_id: str = ""
    corridor_id: str = ""
    line_id: str = ""
    baseline_kw: float = 0.0
    request_kw: float = 100.0
    limit_a: float = 15.0
    request_interval_s: float = DEFAULT_REQUEST_INTERVAL_S
    meter_interval_s: float = DEFAULT_METER_INTERVAL_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    #: Apache basic-auth account group ("DC"/"PIPE"/"ERCOT"/"DIST"/"PARTNER"); "" (default)
    #: infers it from `service_profile` via `default_auth_group`.
    auth_group: str = ""

    def resolved_auth_group(self) -> str:
        return self.auth_group or default_auth_group(self.service_profile)


@dataclass(frozen=True)
class CustomerConfig:
    mqtt: MqttSettings
    #: "" means unset: the Apache-fronted URL must come from `API_BASE_URL_ENV_VAR` (or an
    #: explicit YAML `api.base_url`, dev/test only) -- never a silent loopback default. The
    #: orchestrator's loopback port now 401s without the Apache proxy secret, so a caller
    #: must refuse to start the API part rather than hit it directly.
    api_base_url: str = ""
    api_mode: str = API_MODE_CUSTOMER
    api_timeout_s: float = 10.0
    seed: int = 0
    sites: tuple[CustomerSiteSpec, ...] = field(default_factory=tuple)


def _parse_site(raw: dict[str, Any]) -> CustomerSiteSpec:
    defaults = CustomerSiteSpec(
        customer_id=str(raw["customer_id"]), service_profile=str(raw["service_profile"])
    )
    return CustomerSiteSpec(
        customer_id=defaults.customer_id,
        service_profile=defaults.service_profile,
        contract_id=str(raw.get("contract_id", defaults.contract_id)),
        site_id=str(raw.get("site_id", defaults.site_id)),
        corridor_id=str(raw.get("corridor_id", defaults.corridor_id)),
        line_id=str(raw.get("line_id", defaults.line_id)),
        baseline_kw=float(raw.get("baseline_kw", defaults.baseline_kw)),
        request_kw=float(raw.get("request_kw", defaults.request_kw)),
        limit_a=float(raw.get("limit_a", defaults.limit_a)),
        request_interval_s=float(raw.get("request_interval_s", defaults.request_interval_s)),
        meter_interval_s=float(raw.get("meter_interval_s", defaults.meter_interval_s)),
        poll_interval_s=float(raw.get("poll_interval_s", defaults.poll_interval_s)),
        auth_group=str(raw.get("auth_group", defaults.auth_group)),
    )


def load_customer_config(path: str | None = None) -> CustomerConfig:
    path = path or os.environ.get("OGSIM_CUSTOMER_CONFIG") or str(DEFAULT_CUSTOMER_CONFIG_PATH)
    raw = load_yaml_file(path)
    api_raw = raw.get("api", {}) if isinstance(raw.get("api", {}), dict) else {}
    sites_raw = raw.get("sites", []) if isinstance(raw.get("sites", []), list) else []
    return CustomerConfig(
        mqtt=customer_mqtt_settings_from_env(raw.get("mqtt", {})),
        api_base_url=str(os.environ.get(API_BASE_URL_ENV_VAR) or api_raw.get("base_url", "")),
        api_mode=str(os.environ.get("OGSIM_CUSTOMER_API_MODE") or api_raw.get("mode", API_MODE_CUSTOMER)),
        api_timeout_s=float(api_raw.get("timeout_s", 10.0)),
        seed=int(raw.get("seed", 0)),
        # `enabled: false` keeps an operator in the file but out of the process (e.g. until its
        # Apache account exists): no API calls, no MQTT signals, no credential lookup.
        sites=tuple(_parse_site(s) for s in sites_raw if bool(s.get("enabled", True))),
    )


__all__ = [
    "API_BASE_URL_ENV_VAR",
    "API_MODE_CUSTOMER",
    "API_MODE_OPERATOR_FALLBACK",
    "DEFAULT_CUSTOMER_CONFIG_PATH",
    "DEFAULT_METER_INTERVAL_S",
    "DEFAULT_POLL_INTERVAL_S",
    "DEFAULT_REQUEST_INTERVAL_S",
    "SITE_METER_PROFILES",
    "CustomerConfig",
    "CustomerCredentialsError",
    "CustomerSiteSpec",
    "customer_mqtt_settings_from_env",
    "default_auth_group",
    "load_customer_config",
    "resolve_customer_credentials",
]
