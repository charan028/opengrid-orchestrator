# opengrid.customer_api

The customer-facing API of og-api: tailored services per customer (06-service-profiles-and-power-quality.md
S4). Mounted only when `[api.customer_api].enabled = true`.

## Identity and scoping

- Role `customer` (`opengrid.api.auth`): an `X-Remote-User` listed in `[api.roles.customer]` as
  `user = "<customer_id>"`. Like every identity, it is only believed with Apache's `X-OG-Proxy-Auth` secret.
- Every endpoint is scoped to the caller's customer_id. Another customer's object answers 404, the same
  as a missing one. Customers get 403 on every operator/viewer endpoint (`require_viewer`).

## Endpoints (`/og/api/customer/`)

| Method | Path | Notes |
|---|---|---|
| GET | `me`, `contracts` | the caller and its contracts |
| POST | `opportunities` | `{contract_id, window_start, window_end, requested_kw[, customer_id, service_profile]}` -> `contracts.admit_priced`; 409 `{reason_code}` on rejection |
| GET | `obligations[?state=]`, `obligations/{id}` | `{"obligations": [...]}` with `id`, `state`, `at_risk` |
| GET | `invoices[?from=&to=]` | `{"invoices": [...]}` (invoice lines, `id` = invoice_line_id) from the billing query |
| POST | `invoices/{id}/dispute` | `{reason?, reason_code?}` -> `og.invoice_dispute`, traced; one live dispute per line |
| POST | `obligations/{id}/cancel` | 200 cancelled (OFFERED only), 202 operator review (SELECTED/COMMITTED/DELIVERING), 409 finished |
| POST | `obligations/{id}/renominate` | `{requested_kw?, window_start?, window_end?}`; 202 queued for the next re-nomination point |
| GET | `disputes`, `requests` | the caller's own |

Operators/viewers: `GET /og/api/customer-disputes`, `GET /og/api/customer-requests`; operators record a
review with `PATCH` on either (audited in `og.operator_action` + trace). A review never changes a
commitment.

## Cancel / renominate under K13 (`rules.py`)

The specs do not define customer cancellation, so: cancel applies directly only before selection
(`OFFERED -> REJECTED` through `opengrid.contracts`). From `SELECTED` on, it becomes an operator-reviewed
request carrying the contract's penalty terms, and the obligation stays committed and delivered.
Renomination needs `renomination_allowed`, a committed/delivering obligation and an unexercised
re-nomination point ahead within the window; it is queued for that point's selector gate.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\customer_api -q
```
