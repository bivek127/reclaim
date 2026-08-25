-- migrate:up
GRANT USAGE ON SCHEMA public TO recovery_app, recovery_verifier;

GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO recovery_app;
REVOKE UPDATE ON recovery_cases FROM recovery_app;
GRANT UPDATE (
  state,
  worker_id,
  lease_expires_at,
  fencing_token,
  attempt_count,
  active_elapsed_ms,
  active_since,
  updated_at
) ON recovery_cases TO recovery_app;

GRANT SELECT ON ALL TABLES IN SCHEMA public TO recovery_verifier;
GRANT UPDATE (recovered_amount_minor, state, active_since, updated_at)
  ON recovery_cases TO recovery_verifier;
GRANT INSERT ON verifications TO recovery_verifier;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO recovery_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO recovery_verifier;

-- migrate:down
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM recovery_app, recovery_verifier;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM recovery_app, recovery_verifier;
REVOKE USAGE ON SCHEMA public FROM recovery_app, recovery_verifier;
