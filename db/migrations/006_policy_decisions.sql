-- migrate:up
CREATE TABLE policy_decisions (
  id                  BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id             BIGINT         NOT NULL REFERENCES recovery_cases(id),
  diagnosis_id        BIGINT         REFERENCES diagnoses(id),
  policy_version      TEXT           NOT NULL,
  lookup_miss         BOOLEAN        NOT NULL,
  conflicting_history BOOLEAN        NOT NULL,
  ambiguity_signal    BOOLEAN        NOT NULL,
  verdict             policy_verdict NOT NULL,
  selected_action     action_type,
  reason_code         TEXT           NOT NULL,
  created_at          TIMESTAMPTZ    NOT NULL DEFAULT now(),

  CONSTRAINT ck_allow_has_action CHECK ((verdict = 'ALLOW') = (selected_action IS NOT NULL)),
  CONSTRAINT ck_ambiguity_definition CHECK (ambiguity_signal = (lookup_miss AND conflicting_history)),
  CONSTRAINT ck_ambiguous_never_allows CHECK (NOT (ambiguity_signal AND verdict = 'ALLOW'))
);

-- migrate:down
DROP TABLE IF EXISTS policy_decisions;
