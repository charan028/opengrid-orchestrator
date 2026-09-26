# opengrid.core

Pure functions, no I/O. Single implementation of every shared formula (BUILD.md S1, 02b S12): hub/bank
physics, envelope/limit checks, product-rule rounding, Ed25519 signing, trace hashing, time utilities,
and the pydantic wire/row models. Every other `opengrid` package imports from here instead of
re-deriving these formulas; `orchestrator/tools/dupcheck.py` enforces it.

## Modules

- `physics.py` -- SoC step, P/E/kVA capability, ramp limiting.
- `limits.py` -- reserve/P/kVA/ramp/feeder/fleet-ramp/one-buyer/commitment-lock checks (K1, K2, K4, K13).
- `products.py` -- `min_qty`/`increment`/`block` quantity rounding and variable-kind derivation.
- `crypto.py` -- RFC 8785 JCS canonicalization, Ed25519 sign/verify, SHA-256 helpers.
- `tracehash.py` -- per-stream SHA-256 hash-chain construction and verification.
- `timeutil.py` -- 15-min interval alignment, UTC/America-Chicago conversion, command freshness (K6),
  clock-quality check (K12).
- `models/` -- pydantic v2 contracts: `mqtt.py` (wire messages), `engine.py` (og.* engine tables),
  `platform.py` (fleet/health tables), `pq.py` (MVP-S+ service-profile/power-quality/asset-health
  contracts: `ServiceProfile`, `PowerQualityEnvelope`, waveform/calibration wire messages, and the
  `og.hub_inverter_pq`/`pq_waveform_*`/`calibration_attempt`/`maintenance_work_order`/`asset_event` rows).

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\core -q
```

`mypy --strict` is required for this package (BUILD.md S5a): `python -m mypy src/opengrid/core`.
