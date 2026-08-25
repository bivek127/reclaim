-- migrate:up
CREATE TABLE recovery_actions (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id             BIGINT        NOT NULL REFERENCES recovery_cases(id),
  action_type         action_type   NOT NULL,
  status              action_status NOT NULL DEFAULT 'PROPOSED',
  sequence_no         INT           NOT NULL,
  policy_decision_id  BIGINT        NOT NULL REFERENCES policy_decisions(id),
  superseded_by       BIGINT        REFERENCES recovery_actions(id),
  provider_expires_at TIMESTAMPTZ,
  action_deadline_at  TIMESTAMPTZ,
  created_at          TIMESTAMPTZ   NOT NULL DEFAULT now(),
  resolved_at         TIMESTAMPTZ,

  CONSTRAINT uq_action_sequence UNIQUE (case_id, sequence_no),
  CONSTRAINT ck_supersede_shape CHECK (
      (status = 'SUPERSEDED') = (superseded_by IS NOT NULL)),
  CONSTRAINT ck_resolved_shape CHECK (
      (status IN ('TERMINAL_SUCCESS', 'TERMINAL_FAILED', 'SUPERSEDED')) = (resolved_at IS NOT NULL)),
  CONSTRAINT ck_deadline_after_provider CHECK (
      provider_expires_at IS NULL
   OR action_deadline_at IS NULL
   OR action_deadline_at > provider_expires_at)
);

CREATE UNIQUE INDEX uq_case_one_open_action
  ON recovery_actions (case_id)
  WHERE status IN ('PROPOSED', 'LIVE', 'UNRESOLVED');

-- migrate:down
DROP TABLE IF EXISTS recovery_actions;
