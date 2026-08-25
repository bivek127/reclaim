-- migrate:up
CREATE TABLE recovery_cases (
  id                     BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  obligation_id          BIGINT      NOT NULL REFERENCES financial_obligations(id),
  state                  case_state  NOT NULL DEFAULT 'NEW',

  worker_id              TEXT,
  lease_expires_at       TIMESTAMPTZ NOT NULL DEFAULT '-infinity',
  fencing_token          BIGINT      NOT NULL DEFAULT 0,

  attempt_count          INT         NOT NULL DEFAULT 0,
  max_attempts           INT         NOT NULL DEFAULT 2,

  ttl_budget_ms          BIGINT      NOT NULL,
  active_elapsed_ms      BIGINT      NOT NULL DEFAULT 0,
  active_since           TIMESTAMPTZ,

  recovered_amount_minor BIGINT      NOT NULL DEFAULT 0,

  created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_case_per_obligation UNIQUE (obligation_id),
  CONSTRAINT ck_attempt_budget CHECK (attempt_count >= 0 AND attempt_count <= max_attempts),
  CONSTRAINT ck_fencing_nonneg CHECK (fencing_token >= 0),
  CONSTRAINT ck_recovered_nonneg CHECK (recovered_amount_minor >= 0),
  CONSTRAINT ck_recovered_only_when_verified CHECK (
      recovered_amount_minor = 0 OR state = 'VERIFIED_RECOVERED'),
  CONSTRAINT ck_ttl_clock CHECK (
      CASE WHEN state IN ('HALTED', 'VERIFIED_RECOVERED', 'VERIFIED_FAILED', 'EXPIRED_UNRESOLVED')
           THEN active_since IS NULL
           ELSE active_since IS NOT NULL
      END),
  CONSTRAINT ck_terminal_unleased CHECK (
      state NOT IN ('VERIFIED_RECOVERED', 'VERIFIED_FAILED', 'EXPIRED_UNRESOLVED')
      OR worker_id IS NULL)
);

CREATE INDEX ix_cases_claimable ON recovery_cases (state, lease_expires_at)
  WHERE state NOT IN ('VERIFIED_RECOVERED', 'VERIFIED_FAILED', 'EXPIRED_UNRESOLVED', 'HALTED');
CREATE INDEX ix_cases_ttl ON recovery_cases (active_since)
  WHERE active_since IS NOT NULL;

-- migrate:down
DROP TABLE IF EXISTS recovery_cases;
