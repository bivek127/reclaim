-- migrate:up
CREATE TABLE diagnoses (
  id                 BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id            BIGINT           NOT NULL REFERENCES recovery_cases(id),
  source             diagnosis_source NOT NULL,
  model              TEXT,
  model_version      TEXT,
  prompt_version     TEXT             NOT NULL,
  cause              TEXT             NOT NULL,
  recommended_action action_type,
  reasoning          TEXT,
  confidence         NUMERIC(4, 3),
  raw_response       JSONB,
  llm_retry_count    INT              NOT NULL DEFAULT 0,
  created_at         TIMESTAMPTZ      NOT NULL DEFAULT now(),

  CONSTRAINT ck_reasoning_len CHECK (reasoning IS NULL OR length(reasoning) <= 800),
  CONSTRAINT ck_fallback_has_no_model CHECK (
      source <> 'DETERMINISTIC_FALLBACK' OR model IS NULL)
);

-- migrate:down
DROP TABLE IF EXISTS diagnoses;
