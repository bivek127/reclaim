-- migrate:up
--
-- Extends request_outcome so provider_requests can record what actually
-- happened, for both execution and reconciliation reads.
--
-- Every value below is produced by real adapter code today. Nothing here is
-- speculative or reserved for a later task:
--
--   Execution (ProviderOutcome, reclaim/provider/contract.py) -- these five
--   were previously collapsed onto 'TIMEOUT' because the enum had no member
--   for them, which lost the real cause. That collapse is removed here.
--     PROVIDER_ERROR  HTTP 5xx
--     RATE_LIMITED    HTTP 429  (observed live against Razorpay test mode)
--     UNPARSEABLE     HTTP 2xx with a body that fails validation
--     AUTH_ERROR      HTTP 401/403
--     UNKNOWN         unrecognised status, or inconclusive corroboration
--
--   Reconciliation reads (FetchOutcome) -- the provider GET.
--     FOUND           the mechanism exists under our reference
--     NOT_FOUND       the provider positively reports no such mechanism
--     NO_EVIDENCE     the query failed; NOT proof of absence
--
-- Adding enum members is additive: existing rows and code paths keep their
-- meaning, and no value is ever removed or renamed.
--
ALTER TYPE request_outcome ADD VALUE IF NOT EXISTS 'PROVIDER_ERROR';
ALTER TYPE request_outcome ADD VALUE IF NOT EXISTS 'RATE_LIMITED';
ALTER TYPE request_outcome ADD VALUE IF NOT EXISTS 'UNPARSEABLE';
ALTER TYPE request_outcome ADD VALUE IF NOT EXISTS 'AUTH_ERROR';
ALTER TYPE request_outcome ADD VALUE IF NOT EXISTS 'UNKNOWN';
ALTER TYPE request_outcome ADD VALUE IF NOT EXISTS 'FOUND';
ALTER TYPE request_outcome ADD VALUE IF NOT EXISTS 'NOT_FOUND';
ALTER TYPE request_outcome ADD VALUE IF NOT EXISTS 'NO_EVIDENCE';

-- migrate:down
--
-- NOT REVERSIBLE. PostgreSQL provides no DROP VALUE for an enum type; the
-- only way to remove a label is to recreate the type and rewrite every
-- dependent column, which would rewrite provider_requests and destroy rows
-- carrying the new values.
--
-- This down section is deliberately a no-op rather than a lie. A genuine
-- rollback of this migration means restoring the database from a backup
-- taken before it was applied.
SELECT 1;
