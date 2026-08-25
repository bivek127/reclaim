-- migrate:up
CREATE FUNCTION guard_recovered_amount() RETURNS trigger AS $fn$
BEGIN
  IF NEW.recovered_amount_minor IS DISTINCT FROM OLD.recovered_amount_minor THEN
    IF NOT EXISTS (
      SELECT 1
        FROM verifications v
       WHERE v.case_id = NEW.id
         AND v.agrees
         AND v.verified_amount_minor = NEW.recovered_amount_minor) THEN
      RAISE EXCEPTION 'recovered_amount_minor requires a matching agreeing verification';
    END IF;
  END IF;
  RETURN NEW;
END $fn$ LANGUAGE plpgsql;

CREATE TRIGGER trg_recovered_amount
  BEFORE UPDATE ON recovery_cases
  FOR EACH ROW EXECUTE FUNCTION guard_recovered_amount();

-- migrate:down
DROP TRIGGER IF EXISTS trg_recovered_amount ON recovery_cases;
DROP FUNCTION IF EXISTS guard_recovered_amount();
