# opengrid.contracts.intake

**Purpose:** turn live market data (ERCOT `feed_obs` prices, `opengrid.forecast` P50 scenarios) and
contract terms into `OFFERED` opportunities for every `ACTIVE` contract, once per selector gate — so
`opengrid.selector.run_gate` always has this gate's candidates. Implements the intake task brief;
spec sections 02a §1–§3 (opportunities, obligations, admission).

**Interface:**
- `configure(repo, trace, market, *, forecast_scenarios=None, energy_series_key=...)`
- `run_intake_gate(gate_kind, contract_scope=None, *, now=None) -> list[Opportunity]`

Called from `opengrid.engine._engine_tick`, once per due gate trigger, immediately before
`selector.run_gate(trigger.gate_kind, trigger.contract_scope)`.

**How to test:**
```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\contracts\intake -q
```
Unit tests use `FakeMarketDataPort` and a fake `forecast_scenarios` callable (hand-computed prices) —
no database or live feed required.

**Known simplifications (documented, not hidden — BUILD.md §5a "no silent fallbacks"):**
- No per-customer contracted kW column exists on `og.contract`/`og.product_rule` for a CONTINUOUS
  product, so `energy.DEFAULT_ENERGY_OFFER_KW`/`ancillary.DEFAULT_AS_OFFER_KW`/
  `deferral.DEFAULT_DEFERRAL_OFFER_KW` stand in for it; the real product rule's min/increment/block
  still applies on top via `opengrid.core.products.round_quantity`.
- `DEFAULT_ETA_RT` (0.90) approximates round-trip efficiency; MVP-S has no per-contract efficiency
  field (only per-hub `eta_c`/`eta_d`, which is an allocator/dispatch-time concern, not intake's).
- `DIST_DEFERRAL`'s peak window (`deferral.PEAK_START_HOUR_LOCAL`/`PEAK_END_HOUR_LOCAL`) is a fixed
  constant, not a per-contract delivery calendar — `og.contract` has no calendar column yet.
