-- migrate:up
CREATE TABLE circuit_breaker (
  id                   INT PRIMARY KEY DEFAULT 1,
  state                breaker_state NOT NULL DEFAULT 'CLOSED',
  consecutive_failures INT           NOT NULL DEFAULT 0,
  opened_at            TIMESTAMPTZ,
  reset_after          TIMESTAMPTZ,
  trip_cause           JSONB,
  CONSTRAINT ck_singleton CHECK (id = 1),
  CONSTRAINT ck_open_shape CHECK ((state = 'OPEN') = (opened_at IS NOT NULL))
);

INSERT INTO circuit_breaker (id) VALUES (1);

-- migrate:down
DROP TABLE IF EXISTS circuit_breaker;
