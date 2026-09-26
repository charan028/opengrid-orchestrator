"""ogsim.customer.requests -- builds the orchestrator customer-API opportunity payloads
for each service profile (PIPELINE_AC, DATA_CENTER, ERCOT_ENERGY/AS arbitrage,
DIST_DEFERRAL, PARTNER_CAPACITY), plus the malformed/oversize payload for the
`malformed_request` anomaly (R-ADMIT-REJECT, BUILD.md's customer anomaly catalogue).

The exact `/og/api/customer/opportunities` request shape is owned by the orchestrator's
API package and was still being built when this module was written; every payload goes
through `build_opportunity_payload` so the field mapping can be adjusted in one place once
that contract is final (see this package's README).
"""

from __future__ import annotations

from typing import Any

from ogsim.customer.config import CustomerSiteSpec

#: BUILD.md fleet ceiling context (2,000 hubs, up to 20 kW dual-unit, 40x600 kVA banks):
#: a request several orders of magnitude above this is unambiguously oversize, independent
#: of any one customer's contracted capacity.
OVERSIZE_REQUESTED_KW = 10_000_000.0
OVERSIZE_FIELD_LENGTH = 10_000


def build_opportunity_payload(
    site: CustomerSiteSpec,
    window_start: str,
    window_end: str,
    requested_kw: float | None = None,
    contract_id: str | None = None,
) -> dict[str, Any]:
    """Builds a well-formed opportunity/service request for `site`. Covers capacity
    requests, DATA_CENTER firm bridging, PIPELINE_AC mitigation windows, DIST_DEFERRAL
    peak windows and PARTNER_CAPACITY calls -- they differ only in `service_profile` and
    the caller-supplied window/size, not in payload shape.

    `contract_id` overrides `site.contract_id` when given -- `ogsim.customer.runtime`
    passes the id it discovered from the orchestrator's own obligations/contracts at
    startup (lead coordination: read contract ids from the API rather than hard-coding
    them where possible) rather than trusting a placeholder YAML value.
    """
    return {
        "contract_id": site.contract_id if contract_id is None else contract_id,
        "customer_id": site.customer_id,
        "service_profile": site.service_profile,
        "window_start": window_start,
        "window_end": window_end,
        "requested_kw": site.request_kw if requested_kw is None else requested_kw,
    }


def build_malformed_opportunity_payload(
    site: CustomerSiteSpec, contract_id: str | None = None
) -> dict[str, Any]:
    """Builds a malformed/oversize opportunity request the orchestrator must reject
    (BUILD.md customer anomaly catalogue: "malformed or oversize request", R-ADMIT-REJECT).
    Combines an invalid timestamp shape, a grossly oversize `requested_kw`, and an
    unexpected field."""
    return {
        "contract_id": site.contract_id if contract_id is None else contract_id,
        "customer_id": site.customer_id,
        "service_profile": site.service_profile,
        "window_start": "not-a-timestamp",
        "window_end": "not-a-timestamp",
        "requested_kw": OVERSIZE_REQUESTED_KW,
        "unexpected_field": "x" * OVERSIZE_FIELD_LENGTH,
    }


__all__ = [
    "OVERSIZE_FIELD_LENGTH",
    "OVERSIZE_REQUESTED_KW",
    "build_malformed_opportunity_payload",
    "build_opportunity_payload",
]
