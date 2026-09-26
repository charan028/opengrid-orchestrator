# OpenGrid Orchestrator API documentation

The REST and SSE API served by `og-api` (`opengrid.api`, port 8080 on loopback, reached through Apache at
`https://base.tocy-net.net/og/api/`).

| Document | Read it for |
|---|---|
| [reference.md](reference.md) | Every `/og/api/*` endpoint: role, parameters, body fields, response codes. Generated. |
| [openapi.json](openapi.json) | The raw OpenAPI 3 document. Generated. |
| [auth-and-actions.md](auth-and-actions.md) | Identity (proxy secret, named operators), roles, CSRF, the two-step and two-person flows, SSE streams, error codes |
| [health.md](health.md) | The health payload, degraded modes, and the guardian's K7 escalation alerts |
| [scenarios.md](scenarios.md) | The operator scenario route: request shape, anomaly types, and how it differs from the simulator control plane |
| [integration-howto.md](integration-howto.md) | Switching between live ERCOT/EIA/NWS and the market simulator, the ERCOT token flow |

## Regenerating the reference

The reference is built from the real FastAPI app, so it cannot drift from the code. No database or broker is needed.
From the repository root, with the orchestrator installed:

```
python docs/api/generate_reference.py
```

Roles in the reference come from each route's `require_operator` / `require_viewer` dependency, not from prose.
The wire contracts for the devices (MQTT) and the market simulator live in `interfaces/`, not here.
