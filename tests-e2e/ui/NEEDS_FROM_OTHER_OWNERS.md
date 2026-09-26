# U1: needs outside the UI's owned paths (found on the first live local run, 2026-09-25)

1. **`opengrid.api` has no `GET /og/api/stream/fleet`** (owner: api). `templates/fleet.html` subscribed to it
   and the badge sat on `reconnecting` forever (404). The UI now follows `/og/api/stream/health` for its
   live badge and will refresh per-row SoC/P cells if a frame carries `{"hubs": [{hub_id, soc_kwh, p_kw}]}`.
   Either add that stream, or add `hubs` to the health stream payload, and the per-row live update works
   with no UI change.
2. **Nothing sets `X-OG-Role`** (owner: deploy/api). `deploy/apache/opengrid.conf` sets only `X-Remote-User`,
   so every operator was rendered as a viewer. The UI now derives the role from `X-Remote-User` when no
   `X-OG-Role` is present, using the same zero-config rule as `opengrid.api.auth.role_for_identity`
   (identity literally `operator`). Named accounts from `[api.roles]` are not honoured by the UI; if those
   are used, either have the API mount set `X-OG-Role`, or the UI needs read access to that config.
3. **`GET /og/api/billing/invoice-lines` requires `from`/`to`** (owner: api). Consider defaulting them
   server-side too; the UI now always sends a 30-day period.
4. **`orchestrator/src/opengrid/feeds/secrets.py` is gitignored** (`.gitignore` `secrets*`) and therefore
   missing from every clone; `og-feeds` and `og-engine` crash on import. Commit it and narrow the pattern.
5. **`orchestrator/config/test.toml` lacks `[feeds.ercot].token_url`** (owner: architect), so a dev stack's
   feeds process authenticates against the real `ercotb2c.b2clogin.com` instead of the market simulator
   (`http://127.0.0.1:8090/token`). One line fixes it.
6. **`feed_status.last_value_at` is the latest data timestamp, not the receive time** (owner: feeds/
   api). For day-ahead prices and the wind/solar forecasts that is up to a week in the future, so the
   Markets freshness table showed ages like `-49509s` (the UI now shows `ahead: 6d`), and
   `opengrid.health.rules.evaluate_feed_alert` compares that same field to the staleness window, so
   `ALR-FEED-STALE` can never fire for a forecast product even if it stops arriving. Store the receive
   time (`recorded_at`) as the freshness field, and expose the coverage horizon separately if wanted.
7. **Energy columns on the Dispatch board** (owner: api, per `docs/team/NOTICES.md` 2026-09-25 item 1). The
   UI renders two fields per obligation card and shows `-` until they exist in
   `GET /og/api/dispatch/opportunities` rows and the `/og/api/stream/dispatch` payload:
   `energy_margin_kwh` (energy above reserve on the obligation's eligible hubs minus its remaining delivery,
   kWh, may be negative) and `time_to_depletion_min` (minutes until that margin reaches zero at the
   current grant, null when not discharging). Rename here and in `opengrid.ui.routes.dispatch.pipeline_view`
   together if the api picks other names.
8. Status after main @ c5eda88: item 4 (`feeds/secrets.py` gitignored) is still open; the new `.gitignore`
   lines cover only `dev/` secrets.
9. **The selector never selects anything on the local live stack** (owner: market sim + selector). Traced
   2026-09-26 on the dev stack: every OFFERED opportunity is ERCOT_AS NSPIN at `value_per_mwh = 5.37`
   (= $0.00537/kWh) while `selector/gate.py:165` falls back to `degradation_cost_per_kwh = 0.03` because
   `og.product_rule` carries no degradation cost, so each candidate's objective term
   `(value/1000 - degradation) * kW` is negative and the LP rationally takes none of them (9,106 OPTIMAL
   plans, 0 rows in `og.reservation`). The one DIST_DEFERRAL offer has `value_per_mwh = NULL`. Either the
   market sim must offer prices above ~$30/MWh for the demo, or the degradation cost must be set per
   product rule. Everything downstream is empty as a consequence: no SELECTED/COMMITTED/DELIVERING cards,
   empty ledger timeline, no K13 lock events, no grants, no invoice lines, empty profitability, `fleet_mw
   = 0` (nothing dispatched) and `net_margin_usd = null`.
10. **No code ever moves an opportunity out of OFFERED after a gate** (owner: selector/engine). Nothing
    writes `state = 'SELECTED'|'REJECTED'` or `gate_id` on `og.opportunity` (`grep` finds only the read in
    `contracts/pg_repo.py` and the pending query in `engine/pg_backend.py`), so
    `pending_admission_contract_ids()` returns the same two contracts forever and the engine re-runs the
    ADMISSION gate every 2 s: 3,901 "running gate" lines and 9,106 `og.plan` rows in ~2 h, growing
    unbounded. The gate must stamp `gate_id` (and a decision state) on the candidates it evaluated.
    **HOTFIX APPLIED IN WORKING TREE (2026-09-26):** `selector/db.py::mark_opportunity_selected` sets
    `og.opportunity.state='SELECTED'`, `gate_id=plan_id`, `decided_at` and `og.obligation.state='SELECTED'`
    after a successful `ledger.reserve()`; `gate.py` calls it. Also found while fixing: `gate.py` reserved
    against the *opportunity* id, but `og.reservation.obligation_id` is an FK to `og.obligation`
    (ForeignKeyViolation on every reserve) -- the candidate loader now joins `og.obligation` and reserves
    against the real obligation id. `SELECTED -> COMMITTED` (commitment rows) is still nobody's, so the
    Dispatch board shows Selected but never Committed/Delivering, and the allocator/settle chain stays
    empty. Selector horizon intervals are anchored to `now`, not to quarter hours, so every gate writes
    reservations at different `interval_start`s -- the runaway rows were all distinct.
11. **Cycle latency can never render** (owner: health/engine/architect). `opengrid.health` only scrapes
    when `[health].engine_metrics_url` is set (it is not, in `og.toml` or `config/*.toml`), and no process
    starts a Prometheus HTTP server (`start_http_server` appears nowhere; `og-engine` listens on no TCP
    port), so `og_control_tick_duration_seconds` has no source. Until both exist the Health screen shows
    "No data yet" for p50/p99 by design.
12. **`fleet_mwh` is hard-coded `None`** in `api/routers/health.py:65` ("not yet tracked -- settle owns
    delivered-MWh accounting"), so the Control room's Fleet energy tile shows `--` on every stack.
13. **No hub or bank has coordinates** (owner: sims + fleet seed + api). `og.hub.lat/lon` are NULL for all 200
    seeded hubs (`integration-sims` fleet config carries none) and `GET /og/api/fleet/hubs` does not expose the
    columns anyway, so the Control room map could plot nothing. The UI now draws the spec's simple map (UI-MAP-09)
    from zone + health alone: one bubble per ERCOT load zone at a fixed centroid, sized by hub count and
    coloured by worst state, with a legend. Per-hub markers are already wired for `lat`/`lon` once they exist.
14. **Operator-created opportunities carry no value** (owner: contracts/engine/api). `POST /og/api/opportunities`
    takes no price and the intake gate admits the row with `value_per_mwh = NULL` (trace payload
    `"value_per_mwh": null`), which `selector/gate.py:163` turns into `0.0`, so the objective term is
    `(0 - degradation) * kW < 0` and the LP never selects them. Only the market simulator's own offers carry a
    value (the NSPIN MCPC). Intake should price an operator-created opportunity from the forecast/feed for its
    service type, or the create endpoint should accept a price. (Verified 2026-09-26 by setting
    `value_per_mwh = 45` directly on three demo rows in the local test DB.)
15. **Selector/ledger reservation key mismatch -- HOTFIX APPLIED IN WORKING TREE, needs the lead** (owner:
    selector). `selector/gate.py` keyed `ledger.reserve()`'s `selected_kw_by_interval` by the bare interval
    index (`"0"`, `"1"`) while `ledger.decode_interval_key` requires `bank|start|end`, so the first gate that
    ever selected anything raised `ValueError: malformed reservation interval key: '1'`, `_engine_tick` failed
    (238 times in 10 min on the dev stack) and nothing was ever reserved or committed. The index key also
    collapsed all 8 eligible banks into one entry. Fixed locally by building keys with
    `ledger.encode_interval_key(bank, horizon_start + t*15min, horizon_start + (t+1)*15min)`;
    `tests/unit/selector/test_gate_run_gate.py` updated (39 selector tests pass). Outside the U1 lane: please
    take it as a hotfix like `hotfix/feeds-nws-updatetime`.
16. **Engine cannot start while any hub is `offline`** (owner: fleet + health). `opengrid.health`
    (`health/queries.py` `_UPDATE_HUB_HEALTH_SQL`) writes `health = 'offline'` into `og.hub_state` after 30 s
    without telemetry, but `core/models/platform.py:41` `HubState.health` is `Literal["online","stale","fault"]`,
    so `fleet.load_topology()` raises a pydantic `literal_error` at engine start-up and the process exits.
    Bootstrap deadlock: engine down > hubs go offline > engine can no longer start. Seen 2026-09-26 after an
    engine restart; unblocked locally with `UPDATE og.hub_state SET health='stale' WHERE health='offline'`.
    Add `"offline"` to the literal (health/model.py already has it) or map it on read.
17. **Ledger timeline and grants were hidden by a stale UUID guard -- HOTFIX APPLIED IN WORKING TREE** (owner:
    api + architect). `api/store.py::_fetch_by_uuid_bank_id` returned `[]` for any non-UUID bank id, a guard
    from before `migrations/0004_bank_id_text.sql` made `og.reservation/og.grant.bank_id` TEXT; with
    `bank-007` holding 22 reservations the endpoint returned none. Removed the guard and widened
    `core/models/engine.py` `Reservation.bank_id`/`Grant.bank_id` to `str` (fixtures in
    `tests/unit/api/fakes.py` and `tests/unit/core/test_models.py` updated; 347 unit tests pass).
    Remaining engine noise (owner: engine): `pending_admission_contract_ids` still re-triggers ADMISSION
    every 2 s for contracts whose OFFERED opportunities lie beyond the 24 h horizon (never loaded, so
    never stamped); ~22 gate runs per 45 s instead of ~45. Restrict the pending query to windows that
    start within the horizon.
18. **Obligation list now carries the optimizer's economics -- SMALL API CHANGE IN WORKING TREE** (owner: api +
    architect). `GET /og/api/dispatch/opportunities` rows gained optional `value_per_mwh`, `degradation_cost`
    ($/kWh) and `decided_at` (`api/store.py::list_obligations` joins `og.opportunity`/`og.contract`;
    `core/models/engine.py::Obligation` has the three optional fields). The Dispatch board uses them to print
    one decision sentence per card ("Declined so far: 5.37 $/MWh does not cover 30 $/MWh degradation",
    "Selected: 45 $/MWh clears 10 $/MWh degradation, est. margin ..."). If the stream payload
    (`/og/api/stream/dispatch`) is built from the same store call it carries them too; the UI computes the
    sentence client-side from the numbers on every stream rebuild.
19. **SELECTED -> COMMITTED is now implemented -- HOTFIX APPLIED IN WORKING TREE** (owner: selector + engine +
    contracts). `engine/__init__.py` now calls `contracts.configure(PgContractsRepo(pool), TraceStore(...))`
    (it never had; `contracts.transition_obligation` could not run in og-engine). `selector/gate.py` moves a
    reserved obligation OFFERED -> SELECTED (`R-GATE-SELECT`) -> COMMITTED (`R-COMMIT-LOCK-ENTER`) through the
    state machine, traced, and `selector/db.py::record_commitment` writes one `og.commitment` row per interval
    (kW summed across banks). Verified live 2026-09-26 11:21: COMMITTED=1, 16 commitment rows, trace row
    `COMMITMENT/COMMITMENT {R-COMMIT-LOCK-ENTER}`, 0 tick failures. Also fixed
    `allocator/__init__.py::_to_grant_row`: it masked `bank_id` and `obligation_id` behind `uuid5(...)`, so grant
    rows never matched the real bank or obligation (`og.grant.obligation_id` is an FK). Still open:
    COMMITTED -> DELIVERING at window start (who owns it is unclear from the code), and settlement after delivery.
20. **CR #19 map and funnel endpoints -- UI is built and waiting** (owner: api + fleet + market). The
    screens call each of these and fall back cleanly today, so landing them needs no UI change:
    - `GET /og/api/fleet/map` -- hubs with `lat`/`lon`, `activity` (DELIVERING, IDLE, HOME_USE, CHARGING,
      FAULT, OFFLINE), `kw`, `soc_pct`, `serving_obligations[]`, `can_serve_services[]`. Until it answers,
      hubs are scattered deterministically inside their real load zone and `activity` is derived from
      health plus the sign of `p_kw` (`static/og-map.js::activityOf`). `HOME_USE` cannot be derived --
      it needs the obligation behind the discharge, so it only appears once this endpoint ships.
    - `GET /og/api/customers/map` -- customer sites; the layer is simply empty until then.
    - `GET /og/api/grid/layers` -- zone load, utility batteries, transmission, grid connection, demand
      cells. A static real ERCOT/HIFLD export ships at `ui/static/grid-layers.json` (344 KB: 8 zones,
      90 batteries, 846 backbone segments >=200 kV) so the map is real today; swap to the endpoint when
      it lands. **Best sell destination is a documented proxy** (highest zone load) until this endpoint
      carries settlement-point prices -- the popup and legend say so.
    - `POST /og/api/fleet/commands/bulk` + `.../{id}/confirm` -- the bulk command. The UI computes its own
      risk reasons (`routes/fleet.py::bulk_risk_reasons`) and demands a second acknowledgement when a
      selected hub is serving a customer or is faulted; it ORs that with the API's
      `requires_double_confirm`. Expected confirm body: `{proposal_id, outcome, accepted[], rejected[],
      vetoed_rule_ids[], trace_id}`.
    - `GET /og/api/markets/bid-funnel?from=&to=` -- available/submitted/awarded/rejected by product plus
      rejection reasons; the panel shows an explanatory empty state until it answers.
21. **Hub coordinates -- FIXED, awaiting merge of PR #25** (owner: fleet + api). `og.hub.lat`/`lon` were
    NULL for every seeded hub, so every map position was derived in the browser. `fleet/seed.py` now
    places each hub deterministically within ~45 km of its real ERCOT load-zone centroid
    (`hub_coordinates`), and `api/store.py::list_hubs` projects the columns. Verified on the dev stack:
    200/200 hubs carry coordinates, per-zone means land on the real centroids, and both maps stop
    deriving (the "positions are derived" legend note disappears on its own). Until #25 merges, the maps
    in this PR fall back to browser-side placement, which is why that fallback stays.
