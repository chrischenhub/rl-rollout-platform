CREATE TABLE environments (
    id          text PRIMARY KEY,
    name        text NOT NULL,
    description text NOT NULL DEFAULT '',
    created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE tasks (
    id             text PRIMARY KEY,             -- task.toml [task].name, e.g. pyswe/ledger-bugfix
    environment_id text NOT NULL REFERENCES environments(id),
    slug           text NOT NULL,                -- directory name; Harbor names trials after it
    description    text NOT NULL DEFAULT '',
    current_digest text NOT NULL,                -- version new jobs pin to
    overrides      jsonb NOT NULL DEFAULT '{}',  -- platform-side resources, e.g. {"cpus": 8, "memory_mb": 16384}
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (environment_id, slug)
);

-- Immutable, content-addressed task bundles. Runs pin a digest, so editing a task
-- never changes what an old job ran.
CREATE TABLE task_versions (
    task_id     text NOT NULL REFERENCES tasks(id),
    digest      text NOT NULL,
    version     text NOT NULL,
    instruction text NOT NULL,
    config      jsonb NOT NULL,
    bundle_key  text NOT NULL,
    size_bytes  bigint NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, digest)
);

CREATE TABLE users (
    email         text PRIMARY KEY,
    name          text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_login_at timestamptz
);

CREATE TABLE api_tokens (
    id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    email        text NOT NULL REFERENCES users(email),
    token_hash   text NOT NULL UNIQUE,
    label        text NOT NULL DEFAULT '',
    created_at   timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz,
    revoked_at   timestamptz
);

CREATE TABLE jobs (
    id                  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind                text NOT NULL DEFAULT 'eval' CHECK (kind IN ('eval', 'qa')),
    environment_id      text NOT NULL REFERENCES environments(id),
    agent               text NOT NULL,
    model               text,
    attempts            int  NOT NULL CHECK (attempts BETWEEN 1 AND 10),
    judge               boolean NOT NULL DEFAULT true,
    created_by          text NOT NULL REFERENCES users(email),
    idempotency_key     text,
    cancel_requested_at timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (created_by, idempotency_key)
);

-- One row per execution. A row runs at most once; a retry is a new row with
-- retry_of pointing at the row it replaces, so failure records survive.
CREATE TABLE runs (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id           uuid NOT NULL REFERENCES jobs(id),
    task_id          text NOT NULL,
    task_digest      text NOT NULL,
    agent            text NOT NULL,
    model            text,
    attempt          int  NOT NULL,
    retry_number     int  NOT NULL DEFAULT 0,
    retry_of         uuid REFERENCES runs(id),
    status           text NOT NULL DEFAULT 'queued'
                     CHECK (status IN ('queued', 'running', 'completed', 'error', 'timed_out', 'cancelled')),
    error_kind       text,
    error_message    text,
    log_tail         text,
    reward           double precision,
    rewards          jsonb,
    cost_usd         double precision,
    trial_name       text,
    artifact_prefix  text,
    worker_id        text,
    -- Fencing token: bumped on claim; every write from a worker is conditioned on it.
    lease_token      bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz,
    not_before       timestamptz NOT NULL DEFAULT now(),
    created_at       timestamptz NOT NULL DEFAULT now(),
    started_at       timestamptz,
    finished_at      timestamptz,
    updated_at       timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (task_id, task_digest) REFERENCES task_versions (task_id, digest),
    UNIQUE (job_id, task_id, agent, attempt, retry_number)
);
CREATE INDEX runs_queued_idx   ON runs (not_before) WHERE status = 'queued';
CREATE INDEX runs_running_idx  ON runs (lease_expires_at) WHERE status = 'running';
CREATE INDEX runs_job_idx      ON runs (job_id);
CREATE INDEX runs_retry_of_idx ON runs (retry_of);

CREATE TABLE run_events (
    id     bigserial PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES runs(id),
    at     timestamptz NOT NULL DEFAULT now(),
    kind   text NOT NULL,
    detail jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX run_events_run_idx ON run_events (run_id, id);
