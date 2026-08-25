-- migrate:up
CREATE TABLE sim_outcomes (
  id                    BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id                BIGINT      NOT NULL REFERENCES sim_runs(id),
  arm                   sim_arm     NOT NULL,
  case_id               BIGINT      NOT NULL REFERENCES recovery_cases(id),
  pre_decision_features JSONB       NOT NULL,
  action_type           action_type,
  resolved              BOOLEAN     NOT NULL,
  amount_minor          BIGINT      NOT NULL,
  CONSTRAINT ck_control_has_no_action CHECK (arm <> 'CONTROL' OR action_type IS NULL)
);

-- migrate:down
DROP TABLE IF EXISTS sim_outcomes;
