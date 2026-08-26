-- migrate:up
--
-- §2.9 grants INSERT on verifications to recovery_verifier and to no one else.
-- Migration 018 nevertheless granted it to recovery_app via
--
--   GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
--     TO recovery_app;
--
-- and never revoked it. That leaves barrier 2 of §2.9's "three independent
-- barriers" forgeable from inside the application role: recovery_app could
-- insert an agreeing verifications row which guard_recovered_amount would then
-- accept as support for a revenue write.
--
-- I8 was never breached -- the column privilege (barrier 1) refuses
-- recovery_app the revenue write regardless, and that is re-asserted by test
-- after this migration applies. This restores the independence of barrier 2.
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
