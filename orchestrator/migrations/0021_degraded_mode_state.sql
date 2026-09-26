-- 0021: persists opengrid.health's current degraded-mode set (02b S6.5) so opengrid.api / the UI can
-- read it. Additive only. Renumbered per the lead's migration-numbering coordination: 0019 = scope_posture
-- (safety), 0020 = live-path Non-Spin (as_capacity_hold) -- this table is 0021.
--
-- Bug: opengrid.health.evaluate_once() already computes degraded_modes (NO_NEW_COMMITMENTS,
-- HOLD_LOCAL_AUTONOMY, HOLD, DIST_DEFERRAL_OPEN_LOOP) every cycle inside og-settle, but only ever
-- returned it in an in-process HealthSnapshot -- nothing persisted it. GET /og/api/health builds a
-- completely separate api.store.HealthSnapshot straight from og.heartbeat/og.feed_status/og.hub_state/
-- og.alert, with no degraded-mode field at all, so the API response and the UI's degraded-mode banners
-- could never show a real value.
--
-- og-settle (opengrid.health.evaluate_once, via health.queries.write_degraded_modes) is the sole writer,
-- upserting the currently-active mode set every ~5s cycle: a row exists for exactly as long as that mode
-- is active, `since` records when it most recently became active. opengrid.api reads this table
-- read-only for GET /og/api/health.

CREATE TABLE IF NOT EXISTS og.degraded_mode_state (
    mode  text PRIMARY KEY,
    since timestamptz NOT NULL DEFAULT now()
);
