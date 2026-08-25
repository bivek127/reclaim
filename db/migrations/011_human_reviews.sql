-- migrate:up
CREATE TABLE human_reviews (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  case_id           BIGINT        NOT NULL REFERENCES recovery_cases(id),
  status            review_status NOT NULL DEFAULT 'PENDING',
  reviewer_ref      TEXT,
  selected_action   action_type,
  review_expires_at TIMESTAMPTZ     NOT NULL,
  created_at        TIMESTAMPTZ     NOT NULL DEFAULT now(),
  decided_at        TIMESTAMPTZ,

  CONSTRAINT ck_decided_shape CHECK (
      (status IN ('APPROVED', 'REJECTED')) = (decided_at IS NOT NULL AND reviewer_ref IS NOT NULL)),
  CONSTRAINT ck_approve_has_action CHECK (status <> 'APPROVED' OR selected_action IS NOT NULL)
);

CREATE UNIQUE INDEX uq_case_one_open_review
  ON human_reviews (case_id)
  WHERE status = 'PENDING';

-- migrate:down
DROP TABLE IF EXISTS human_reviews;
