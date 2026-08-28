-- migrate:up
--
-- A run's seed is meant to make every reported number reproducible.
--
-- That held for outcome rows but not for reported metrics. compute_metrics()
-- decided EXPIRED_UNRESOLVED exclusion from the LIVE recovery_cases.state,
-- while sim_outcomes persisted no state. A real case whose story continued
-- after the run therefore silently rewrote the run's reported numbers:
--
--     same seed + same sim_outcomes + case state changes
--       -> identical outcome fingerprint
--       -> DIFFERENT control_n / excluded_from_lift / lift
--
-- Freezing the state at selection time is also the correct semantics on its own
-- terms: an experiment's recorded result should reflect what was known when it
-- ran, not be revised by later real-world events.
--
-- Reuses the existing `case_state` enum from migration 001 rather than
-- introducing a new type -- the same reuse audit_events.prev_state/new_state
-- already makes. One additive column; recovery_cases is untouched.
--
-- NOT NULL is safe without a backfill: no persisted simulation data exists
-- anywhere (no reclaim_dev database; reclaim_test is truncated per test).
--
--
ALTER TABLE sim_outcomes
  ADD COLUMN case_state_at_run case_state NOT NULL;

-- migrate:down
ALTER TABLE sim_outcomes
  DROP COLUMN case_state_at_run;
