-- migrate:up
CREATE TABLE financial_obligations (
  id                BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  anchor_kind       anchor_kind NOT NULL,
  anchor_key        TEXT        NOT NULL,
  anchor_canonical  TEXT        NOT NULL,
  amount_minor      BIGINT      NOT NULL,
  currency          CHAR(3)     NOT NULL DEFAULT 'INR',
  customer_ref      TEXT        NOT NULL,
  source_event_id   TEXT        NOT NULL,
  first_seen_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_seen_at      TIMESTAMPTZ NOT NULL DEFAULT now(),

  CONSTRAINT uq_obligation_anchor UNIQUE (anchor_canonical),
  CONSTRAINT ck_amount_positive CHECK (amount_minor > 0),
  CONSTRAINT ck_currency_upper CHECK (currency = upper(currency)),
  CONSTRAINT ck_anchor_shape CHECK (
      (anchor_kind = 'ORDER' AND anchor_canonical LIKE 'order:%')
   OR (anchor_kind = 'SUBSCRIPTION_CYCLE' AND anchor_canonical LIKE 'subcycle:%:%'))
);

CREATE FUNCTION guard_obligation_immutable() RETURNS trigger AS $fn$
BEGIN
  IF NEW.amount_minor IS DISTINCT FROM OLD.amount_minor
  OR NEW.currency IS DISTINCT FROM OLD.currency
  OR NEW.customer_ref IS DISTINCT FROM OLD.customer_ref
  OR NEW.anchor_canonical IS DISTINCT FROM OLD.anchor_canonical THEN
    RAISE EXCEPTION 'financial_obligations financial fields are immutable';
  END IF;
  RETURN NEW;
END $fn$ LANGUAGE plpgsql;

CREATE TRIGGER trg_obligation_immutable
  BEFORE UPDATE ON financial_obligations
  FOR EACH ROW EXECUTE FUNCTION guard_obligation_immutable();

-- migrate:down
DROP TRIGGER IF EXISTS trg_obligation_immutable ON financial_obligations;
DROP FUNCTION IF EXISTS guard_obligation_immutable();
DROP TABLE IF EXISTS financial_obligations;
