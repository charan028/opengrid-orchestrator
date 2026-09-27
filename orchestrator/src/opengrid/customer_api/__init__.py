"""opengrid.customer_api -- the customer-facing API of og-api (06-service-profiles-and-power-quality.md S4:
the service is tailored per customer and service type; per-customer PQ envelopes).

`router` serves `/og/api/customer/...` to callers with role `customer` (`opengrid.api.auth`,
`[api.roles.customer]`), every request scoped to the caller's own customer_id. `operator_router` gives
operators/viewers the customer disputes and requests to review. `utility_router` serves a utility's own
toll calls under `/og/api/customer/v1/utility/` (D-33, role `utility`, through `opengrid.calls`). Admission, obligation state and billing
reads are reused from `opengrid.contracts` and `opengrid.api.store`; this package owns only
`og.invoice_dispute` and `og.customer_obligation_request` (`migrations/0026_customer_services.sql`).
"""

from __future__ import annotations

from opengrid.customer_api.delivery_routes import router as delivery_router
from opengrid.customer_api.operator_routes import router as operator_router
from opengrid.customer_api.routes import router
from opengrid.customer_api.utility_routes import router as utility_router

__all__ = ["delivery_router", "operator_router", "router", "utility_router"]
