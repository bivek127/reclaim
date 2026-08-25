-- migrate:up
CREATE TABLE audit_events (
  id                      BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  occurred_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
  event_type              TEXT        NOT NULL,
  obligation_id           BIGINT,
  case_id                 BIGINT,
  action_id               BIGINT,
  attempt_id              BIGINT,
  provider_request_id     BIGINT,
  worker_id               TEXT,
  fencing_token           BIGINT,
  prev_state              case_state,
  new_state               case_state,
  reason_code             TEXT,
  model                   TEXT,
  model_version           TEXT,
  policy_version          TEXT,
  reviewer_ref            TEXT,
  provider_correlation_id TEXT,
  detail                  JSONB       NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX ix_audit_case_time ON audit_events (case_id, occurred_at);

CREATE FUNCTION audit_append_only() RETURNS trigger AS $fn$
BEGIN
  RAISE EXCEPTION 'audit_events is append-only';
END $fn$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_no_mutate
  BEFORE UPDATE OR DELETE ON audit_events
  FOR EACH ROW EXECUTE FUNCTION audit_append_only();

-- migrate:down
DROP TRIGGER IF EXISTS trg_audit_no_mutate ON audit_events;
DROP FUNCTION IF EXISTS audit_append_only();
DROP TABLE IF EXISTS audit_events;
