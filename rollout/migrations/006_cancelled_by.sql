-- Who asked for a job to be cancelled (NULL for jobs cancelled before this was kept).
ALTER TABLE jobs ADD COLUMN cancelled_by text REFERENCES users (email);
