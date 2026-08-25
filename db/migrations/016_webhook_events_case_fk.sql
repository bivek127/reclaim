-- migrate:up
ALTER TABLE webhook_events
  ADD CONSTRAINT webhook_events_case_id_fkey
  FOREIGN KEY (case_id) REFERENCES recovery_cases(id);

-- migrate:down
ALTER TABLE webhook_events DROP CONSTRAINT IF EXISTS webhook_events_case_id_fkey;
