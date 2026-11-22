-- Seconds the agent itself worked: Harbor's agent_execution phase, without sandbox
-- creation, agent install, grading or artifact sync. Task QA compares runs on this,
-- since the wall clock mostly measures Daytona.
ALTER TABLE runs ADD COLUMN agent_sec double precision;

-- A person's verdict on a Task QA flag. Flags are computed from the runs on every read
-- (see rollout/qa.py); a review attaches to one by (job, task, code) and shows while
-- that flag stands. Reviewing again replaces the verdict.
CREATE TABLE qa_flag_reviews (
    job_id      uuid NOT NULL REFERENCES jobs (id),
    task_id     text NOT NULL,
    code        text NOT NULL,
    verdict     text NOT NULL CHECK (verdict IN ('valid', 'false_positive')),
    note        text NOT NULL DEFAULT '',
    reviewed_by text NOT NULL REFERENCES users (email),
    reviewed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (job_id, task_id, code)
);
