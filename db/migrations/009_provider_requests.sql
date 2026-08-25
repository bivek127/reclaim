-- migrate:up
CREATE TABLE provider_requests (
  id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  attempt_id              BIGINT          NOT NULL REFERENCES execution_attempts(id),
  operation               TEXT            NOT NULL,
  request_no              INT             NOT NULL,
  idempotency_key         TEXT            NOT NULL,
  request_body            JSONB           NOT NULL,
  outcome                 request_outcome NOT NULL DEFAULT 'IN_FLIGHT',
  http_status             INT,
  provider_correlation_id TEXT,
  response_body           JSONB,
  sent_at                 TIMESTAMPTZ     NOT NULL DEFAULT now(),
  completed_at            TIMESTAMPTZ,

  CONSTRAINT uq_request_sequence UNIQUE (attempt_id, request_no),
  CONSTRAINT ck_completed_shape CHECK (
      (outcome = 'IN_FLIGHT') = (completed_at IS NULL))
);

CREATE INDEX ix_requests_inflight ON provider_requests (sent_at)
  WHERE outcome = 'IN_FLIGHT';

-- migrate:down
DROP TABLE IF EXISTS provider_requests;
