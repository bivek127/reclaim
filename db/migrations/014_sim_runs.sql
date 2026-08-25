-- migrate:up
CREATE TABLE sim_runs (
  id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  seed       BIGINT      NOT NULL,
  n_per_arm  INT         NOT NULL,
  params     JSONB       NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- migrate:down
DROP TABLE IF EXISTS sim_runs;
