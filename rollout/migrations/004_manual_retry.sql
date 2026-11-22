-- Who created a retry: the platform after an infrastructure failure, or a person.
ALTER TABLE runs ADD COLUMN retry_kind text CHECK (retry_kind IN ('auto', 'manual'));
-- Automatic retries in a row that led to this run. MAX_INFRA_RETRIES caps this, not
-- retry_number, so a manual retry starts with a fresh automatic budget.
ALTER TABLE runs ADD COLUMN auto_retries int NOT NULL DEFAULT 0;
-- The person who asked for a manual retry.
ALTER TABLE runs ADD COLUMN requested_by text REFERENCES users (email);

-- Every retry so far was automatic.
UPDATE runs SET retry_kind = 'auto', auto_retries = retry_number WHERE retry_of IS NOT NULL;
ALTER TABLE runs ADD CONSTRAINT runs_retry_kind_set CHECK ((retry_of IS NULL) = (retry_kind IS NULL));
