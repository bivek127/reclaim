-- migrate:up
CREATE TABLE verifications (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id               BIGINT      NOT NULL REFERENCES recovery_cases(id),
  attempt_id            BIGINT      NOT NULL REFERENCES execution_attempts(id),
  webhook_event_id      BIGINT      REFERENCES webhook_events(id),
  webhook_status        TEXT,
  query_status          TEXT,
  query_correlation_id  TEXT,
  agrees                BOOLEAN     NOT NULL,
  verified_amount_minor BIGINT      NOT NULL DEFAULT 0,
  created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT ck_verified_amount CHECK (
      (agrees = false AND verified_amount_minor = 0)
   OR (agrees = true AND verified_amount_minor > 0))
);

-- migrate:down
DROP TABLE IF EXISTS verifications;
