-- 0023: og.pnl gains delivery_charge (09-optimizer-dispatcher-update.md D5's M1 TDSP delivery
-- charge, a fifth P&L term alongside revenue/energy_cost/degradation_cost/penalty). Additive only:
-- 0001_init.sql's og.pnl is otherwise unchanged. Renumbered twice: originally 0019, then 0022, both
-- taken concurrently by the live-path agent (0019 scope_posture, 0020 as_capacity_hold, 0021
-- degraded_mode_state, 0022 demo_as_ecrs) -- 0023 is the coordinated next free number.

ALTER TABLE og.pnl ADD COLUMN IF NOT EXISTS delivery_charge numeric(18,6) NOT NULL DEFAULT 0;
