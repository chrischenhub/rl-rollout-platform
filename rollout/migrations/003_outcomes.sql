-- Which synced file log_tail was taken from (relative to the run's artifacts), so the
-- UI can say what it's showing and link to the whole file.
ALTER TABLE runs ADD COLUMN log_source text;
UPDATE runs SET log_source = 'harbor.log' WHERE log_tail IS NOT NULL;

-- The grader's verdict in brief: failed criteria, missing deliverables, grader error.
-- Lets the job table say why a graded run failed without opening every report.
ALTER TABLE runs ADD COLUMN grade jsonb;

-- Operator knobs that change how Harbor runs this job's tasks (used to demonstrate
-- failure handling): agent_timeout_multiplier, cpus, memory_mb.
ALTER TABLE jobs ADD COLUMN options jsonb NOT NULL DEFAULT '{}';
