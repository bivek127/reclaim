-- migrate:up
CREATE TABLE execution_attempts (
  id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  action_id          BIGINT        NOT NULL REFERENCES recovery_actions(id),
  case_id            BIGINT        NOT NULL REFERENCES recovery_cases(id),
  attempt_no         INT           NOT NULL,
  idempotency_key    TEXT          NOT NULL,
  provider_reference TEXT,
  state              attempt_state NOT NULL DEFAULT 'PREPARED',
  amount_minor       BIGINT        NOT NULL,
  currency           CHAR(3)       NOT NULL,
  created_at         TIMESTAMPTZ   NOT NULL DEFAULT now(),
  settled_at         TIMESTAMPTZ,

  CONSTRAINT uq_attempt_key UNIQUE (idempotency_key),
  CONSTRAINT uq_attempt_reference UNIQUE (provider_reference),
  CONSTRAINT uq_attempt_sequence UNIQUE (action_id, attempt_no),
  CONSTRAINT ck_attempt_amount CHECK (amount_minor > 0)
);

CREATE UNIQUE INDEX uq_action_one_open_attempt
  ON execution_attempts (action_id)
  WHERE state IN ('PREPARED', 'IN_FLIGHT', 'UNKNOWN');

CREATE FUNCTION guard_attempt_amount() RETURNS trigger AS $fn$
DECLARE ob RECORD;
BEGIN
  SELECT o.amount_minor, o.currency INTO ob
    FROM recovery_cases c
    JOIN financial_obligations o ON o.id = c.obligation_id
   WHERE c.id = NEW.case_id;
  IF NEW.amount_minor <> ob.amount_minor OR NEW.currency <> ob.currency THEN
    RAISE EXCEPTION 'execution_attempt amount/currency must match the obligation';
  END IF;
  RETURN NEW;
END $fn$ LANGUAGE plpgsql;

CREATE TRIGGER trg_attempt_amount
  BEFORE INSERT OR UPDATE ON execution_attempts
  FOR EACH ROW EXECUTE FUNCTION guard_attempt_amount();

-- migrate:down
DROP TRIGGER IF EXISTS trg_attempt_amount ON execution_attempts;
DROP FUNCTION IF EXISTS guard_attempt_amount();
DROP TABLE IF EXISTS execution_attempts;
