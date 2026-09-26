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
