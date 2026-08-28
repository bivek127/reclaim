-- migrate:up
--
-- The verifier's original grants were UPDATE (recovered_amount_minor, state,
-- active_since, updated_at) plus INSERT on verifications. That set is not
-- enough: the verification row, the state transition and the revenue write
-- must all commit in ONE transaction as recovery_verifier, so revenue can
-- never be recognised without the evidence that justifies it.
--
-- Proven against the live database before writing this migration:
--
--   claim_case()  as recovery_verifier -> permission denied for recovery_cases
--   transition()  as recovery_verifier -> permission denied for recovery_cases
--   INSERT audit_events                -> permission denied for audit_events
--
-- The missing privileges are exactly what transition() and claim_case()
-- already touch:
--
--   active_elapsed_ms   transition() always writes it (TTL clock arithmetic)
--   worker_id           transition() clears it when entering a terminal state
--   lease_expires_at    claim_case() sets the lease
--   fencing_token       claim_case() bumps it
--   audit_events        transition() writes one row per transition
--
-- This grants those and nothing else. The revenue privilege boundary is
-- untouched: recovery_app still has no UPDATE privilege on
-- recovered_amount_minor, and both guard_recovered_amount and
-- ck_recovered_only_when_verified continue to apply to the verifier itself.
--
-- This widens only the columns those two functions need. The revenue column
-- itself stays restricted to the verifier role.
--
GRANT UPDATE (active_elapsed_ms, worker_id, lease_expires_at, fencing_token)
  ON recovery_cases TO recovery_verifier;

GRANT INSERT ON audit_events TO recovery_verifier;

-- migrate:down
REVOKE UPDATE (active_elapsed_ms, worker_id, lease_expires_at, fencing_token)
  ON recovery_cases FROM recovery_verifier;

REVOKE INSERT ON audit_events FROM recovery_verifier;
