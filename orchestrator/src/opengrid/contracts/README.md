# opengrid.contracts

Customers/contracts CRUD, opportunity intake and admission, the obligation state machine, product-rule
enforcement, re-nomination points, and the obligation/commitment queries `selector`/`ledger`/`settle`
read (02a S1-S2, BUILD.md S4). The only module that writes `og.contract`, `og.product_rule`,
`og.opportunity`, `og.obligation` and `og.renomination_point`.

## Interface

Fixed (orchestrator/INTERFACES.md): `admit(contract_id, window_start, window_end, requested_kw)`,
`get_contract(contract_id)`, `product_rules_for(contract_id)`, `AdmissionError`.

Additional public surface this module owns: `configure(repo, trace)` (call once at process startup
before anything else in this package is used -- see `opengrid.engine.main`), customer/contract/
product-rule CRUD (`list_contracts`, `list_customer_ids`, `create_contract`, `set_contract_status`,
`create_product_rule`), the obligation lifecycle (`transition_obligation`, `expire_unselected`),
queries (`active_obligations`), and re-nomination (`due_renomination_points`,
`exercise_renomination_point`).

## Modules

- `state_machine.py` -- pure obligation FSM (02a S2.1 table); no I/O. Encodes K13 structurally: the
  only edges out of `COMMITTED`/`DELIVERING` are `DELIVERING` and, from there, `FULFILLED` or
  `SHORTFALL` with an allowed override/infeasible reason code.
- `lifecycle.py` -- `transition_obligation`: validates via `state_machine`, persists with an
  optimistic-lock CAS on `obligation.version`, then traces (K10: no state change without a trace
  pre-image). Also `expire_unselected` (the `OFFERED -> EXPIRED` sweep).
- `admission.py` -- `admit()`: structural feasibility (`opengrid.core.products`) and eligibility,
  creates the paired `OFFERED` opportunity + obligation, or traces a reason-coded rejection with no
  row written.
- `renomination.py` -- re-nomination gate handling, scoped to one contract's obligation.
- `crud.py` -- contract/product-rule CRUD; customer directory derived from `contract.customer_id`
  (MVP-S has no separate customer table).
- `repository.py` -- the `ContractsRepo` storage protocol (pure interface, no psycopg import).
- `pg_repo.py` -- the Postgres implementation of `ContractsRepo`, built on
  `opengrid.platform.db`'s pool. Kept separate so everything else here is DB-free and unit-testable.

## How to test

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit\contracts -q
```

Unit tests use an in-memory fake `ContractsRepo` and a real `opengrid.trace.TraceStore` backed by an
in-memory `TraceBackend` fake (per that module's own docstring) -- no database required. Integration
tests against Postgres (`og_t_ctr`) live under `orchestrator/tests/integration/contracts/` and run via
`tools/remote.ps1 -Ws ctr`.
