-- migrate:up
--
-- §2.9 lists the verifier's grants as UPDATE (recovered_amount_minor, state,
-- active_since, updated_at) plus INSERT on verifications. That set cannot
-- satisfy §11.2 step 4, which requires the verification row, the state
-- transition and the revenue write to commit in ONE transaction AS
-- recovery_verifier.
--
-- Proven against the live database before writing this migration:
--
--   claim_case()  as recovery_verifier -> permission denied for recovery_cases
--   transition()  as recovery_verifier -> permission denied for recovery_cases
--   INSERT audit_events                -> permission denied for audit_events
--
-- The missing privileges are exactly what the Task 3 transition() and Task 4
-- claim_case() implementations touch:
--
--   active_elapsed_ms   transition() always writes it (TTL clock arithmetic)
--   worker_id           transition() clears it when entering a terminal state
--   lease_expires_at    claim_case() sets the lease
--   fencing_token       claim_case() bumps it
--   audit_events        transition() writes one row per transition (§13)
--
-- This grants those and nothing else. The revenue privilege boundary is
-- untouched: recovery_app still has no UPDATE privilege on
-- recovered_amount_minor, and both guard_recovered_amount and
-- ck_recovered_only_when_verified continue to apply to the verifier itself.
--
-- Recorded in ADR-015. SPECIFICATION.md §2.9 is deliberately NOT edited.
--
GRANT UPDATE (active_elapsed_ms, worker_id, lease_expires_at, fencing_token)
  ON recovery_cases TO recovery_verifier;

GRANT INSERT ON audit_events TO recovery_verifier;

-- migrate:down
REVOKE UPDATE (active_elapsed_ms, worker_id, lease_expires_at, fencing_token)
  ON recovery_cases FROM recovery_verifier;

REVOKE INSERT ON audit_events FROM recovery_verifier;
