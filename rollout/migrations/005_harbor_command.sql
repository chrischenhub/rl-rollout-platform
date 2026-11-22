-- The exact `harbor run` argv the worker launched for this run, recorded before launch.
-- It shows what actually ran (task version, overrides, timeout multiplier) even after
-- a task's platform overrides change. Credentials travel in the environment, never here.
ALTER TABLE runs ADD COLUMN harbor_command text[];
