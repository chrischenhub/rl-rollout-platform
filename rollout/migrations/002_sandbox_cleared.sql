-- When this run's sandboxes were confirmed deleted on Daytona. Until then a run that
-- started counts against MAX_RUNNING, and no later try of the same task attempt
-- (a retry, automatic or manual) can start.
ALTER TABLE runs ADD COLUMN sandbox_cleared_at timestamptz;

-- Runs that finished before this column existed were already swept by the janitor.
UPDATE runs SET sandbox_cleared_at = coalesce(finished_at, now())
WHERE status IN ('completed', 'error', 'timed_out', 'cancelled');

CREATE INDEX runs_sandbox_pending ON runs (finished_at)
    WHERE started_at IS NOT NULL AND sandbox_cleared_at IS NULL;
