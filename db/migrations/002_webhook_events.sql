-- migrate:up
-- No case_id foreign key: recovery_cases does not exist yet at this point in
-- the migration order. The column is populated once a case is resolved.
CREATE TABLE webhook_events (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  provider_event_id TEXT               NOT NULL,
  event_type        TEXT               NOT NULL,
  signature_valid   BOOLEAN            NOT NULL,
  resolution        webhook_resolution NOT NULL,
  anchor_canonical  TEXT,
  case_id           BIGINT,
  payload           JSONB              NOT NULL,
  received_at       TIMESTAMPTZ        NOT NULL DEFAULT now(),
  processed_at      TIMESTAMPTZ,

  CONSTRAINT uq_provider_event UNIQUE (provider_event_id),
  CONSTRAINT ck_resolved_has_anchor CHECK (resolution <> 'RESOLVED' OR anchor_canonical IS NOT NULL)
);

-- migrate:down
DROP TABLE IF EXISTS webhook_events;
