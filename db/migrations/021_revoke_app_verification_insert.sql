-- migrate:up
--
-- INSERT on verifications is meant for recovery_verifier and no one else.
-- Migration 018 nevertheless granted it to recovery_app via
--
--   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
--     TO recovery_app;
--
-- and never revoked it. Revenue is protected by three independent barriers:
-- the column privilege, the verification row itself, and the guard trigger.
-- That grant left the second one forgeable from inside the application role:
-- recovery_app could insert an agreeing verifications row which
-- guard_recovered_amount would then accept as support for a revenue write.
--
-- Revenue was never actually writable by recovery_app -- the column privilege
-- refuses it regardless, and that is re-asserted by test after this migration
-- applies. This restores the independence of the second barrier.
--
-- Verified before writing: no production flow needs it. The only
-- INSERT INTO verifications in reclaim/ is verification.py, which runs on a
-- recovery_verifier connection by contract.
--
-- SELECT is deliberately retained: review evidence assembly and later
-- reporting read this table.
--
REVOKE INSERT, UPDATE, DELETE ON verifications FROM recovery_app;

-- migrate:down
GRANT INSERT, UPDATE, DELETE ON verifications TO recovery_app;
