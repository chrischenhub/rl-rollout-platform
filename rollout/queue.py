"""Every job and run state transition, as SQL. Postgres is both the record and the queue.

Invariants this module maintains:
- A run row executes at most once. A retry is a new row whose retry_of points at
  the row it replaces; the original keeps its failure record.
- Only the worker holding a run's current lease can write its outcome: worker
  writes are conditioned on status = 'running' AND lease_token = <token claimed>.
- A run whose lease expires is reaped (marked worker_lost, retried if under the
  cap), so a crashed worker never leaves a run stuck in 'running'.
- A run that started may have a live sandbox until sandbox_cleared_at is set, which
  happens only once Daytona has no sandbox under the run's name. Until then it counts against the
  global cap, and no other try of the same task attempt can be claimed.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from rollout.models import TERMINAL, Agent, ErrorKind, JobKind, Outcome, RunStatus
from rollout.outcomes import run_outcome, summarize
from rollout.qa import QA_MIN_RUNS

CLAIM_LOCK_ID = 7_301_002

Row = dict[str, Any]


class JobRequestError(ValueError):
    """The request names tasks or settings the platform can't run."""


async def _one(conn: AsyncConnection, sql: str, params: Any = None) -> Row | None:
    cur = await conn.execute(sql, params)
    return await cur.fetchone()


async def _all(conn: AsyncConnection, sql: str, params: Any = None) -> list[Row]:
    cur = await conn.execute(sql, params)
    return await cur.fetchall()


async def log_event(conn: AsyncConnection, run_id: UUID, kind: str, **detail: Any) -> None:
    await conn.execute(
        "INSERT INTO run_events (run_id, kind, detail) VALUES (%s, %s, %s)",
        (run_id, kind, Jsonb(json.loads(json.dumps(detail, default=str)))),
    )


# --- jobs -----------------------------------------------------------------------------


async def create_job(
    conn: AsyncConnection,
    *,
    created_by: str,
    task_ids: list[str],
    agent: str,
    model: str | None,
    attempts: int,
    judge: bool = True,
    idempotency_key: str | None = None,
    kind: str = "eval",
    options: dict | None = None,
) -> tuple[UUID, bool]:
    """Insert a job and its tasks x attempts runs in one transaction. A Task QA job
    (kind 'qa') also gets one oracle run per task, pinned to the same task version.

    Returns (job_id, created). Repeating a request with the same idempotency key
    returns the original job instead of creating a second one.
    """
    task_ids = list(dict.fromkeys(task_ids))
    if not task_ids:
        raise JobRequestError("choose at least one task")
    if not 1 <= attempts <= 10:
        raise JobRequestError("attempts must be between 1 and 10")
    if kind not in JobKind:
        raise JobRequestError(f"kind must be one of: {', '.join(JobKind)}")
    if kind == JobKind.QA:
        if agent == Agent.ORACLE:
            raise JobRequestError("Task QA compares a model with the oracle; choose a Claude model")
        if attempts < QA_MIN_RUNS:
            raise JobRequestError(f"Task QA needs at least {QA_MIN_RUNS} model runs per task to see variation")

    async with conn.transaction():
        if idempotency_key:
            existing = await _one(
                conn,
                "SELECT id FROM jobs WHERE created_by = %s AND idempotency_key = %s",
                (created_by, idempotency_key),
            )
            if existing:
                return existing["id"], False

        tasks = await _all(conn, "SELECT id, environment_id FROM tasks WHERE id = ANY(%s)", (task_ids,))
        missing = sorted(set(task_ids) - {t["id"] for t in tasks})
        if missing:
            raise JobRequestError(f"unknown task ids: {', '.join(missing)}")
        envs = {t["environment_id"] for t in tasks}
        if len(envs) != 1:
            raise JobRequestError("all tasks in a job must belong to one environment")

        job = await _one(
            conn,
            """
            INSERT INTO jobs (kind, environment_id, agent, model, attempts, judge, created_by, idempotency_key, options)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (created_by, idempotency_key) DO NOTHING
            RETURNING id
            """,
            (kind, envs.pop(), agent, model, attempts, judge, created_by, idempotency_key, Jsonb(options or {})),
        )
        if job is None:  # lost a race with an identical request
            existing = await _one(
                conn,
                "SELECT id FROM jobs WHERE created_by = %s AND idempotency_key = %s",
                (created_by, idempotency_key),
            )
            return existing["id"], False

        # One statement, so the oracle and the model runs read the same task versions.
        await conn.execute(
            """
            INSERT INTO runs (job_id, task_id, task_digest, agent, model, attempt)
            SELECT %(job)s, t.id, t.current_digest, a.agent, a.model, a.attempt
            FROM unnest(%(tasks)s::text[]) WITH ORDINALITY AS req(task_id, pos)
            JOIN tasks t ON t.id = req.task_id
            CROSS JOIN (
                SELECT %(agent)s::text AS agent, %(model)s::text AS model, n AS attempt
                FROM generate_series(1, %(attempts)s) AS n
                UNION ALL
                SELECT %(oracle)s, NULL, 1 WHERE %(qa)s
            ) AS a
            ORDER BY req.pos, a.agent, a.attempt
            """,
            {
                "job": job["id"], "tasks": task_ids, "agent": agent, "model": model, "attempts": attempts,
                "oracle": Agent.ORACLE, "qa": kind == JobKind.QA,
            },
        )
        return job["id"], True


async def cancel_job(conn: AsyncConnection, job_id: UUID, *, by: str | None = None) -> Row | None:
    """Request cancellation. Idempotent: queued runs become cancelled now; running
    runs are stopped by their worker's next heartbeat (or just before Harbor starts),
    or by the reaper if the worker is gone. A job with nothing left to stop is left
    as it is, so cancelling a finished job doesn't relabel it."""
    async with conn.transaction():
        # FOR UPDATE: waits for a run finishing right now (finish_run holds this row
        # FOR SHARE), so an automatic retry it queues is cancelled below.
        job = await _one(conn, "SELECT * FROM jobs WHERE id = %s FOR UPDATE", (job_id,))
        if job is None:
            return None
        if job["cancel_requested_at"] is None:
            active = await _one(
                conn, "SELECT count(*) AS n FROM runs WHERE job_id = %s AND status IN ('queued', 'running')",
                (job_id,),
            )
            if not active["n"]:
                return job
            job = await _one(
                conn,
                "UPDATE jobs SET cancel_requested_at = now(), cancelled_by = %s WHERE id = %s RETURNING *",
                (by, job_id),
            )
        cancelled = await _all(
            conn,
            "UPDATE runs SET status = 'cancelled', finished_at = now(), updated_at = now()"
            " WHERE job_id = %s AND status = 'queued' RETURNING id",
            (job_id,),
        )
        for r in cancelled:
            await log_event(conn, r["id"], "cancelled", before_start=True)
        return job


# --- worker side ----------------------------------------------------------------------

# This run started and its sandboxes aren't confirmed deleted: one may still be live.
SANDBOX_PENDING = "started_at IS NOT NULL AND sandbox_cleared_at IS NULL"

# Another try of r's task attempt (whatever created it: the reaper, an automatic or a
# manual retry) may still have a live sandbox, so r must not start yet.
EARLIER_TRY_PENDING = """EXISTS (
    SELECT 1 FROM runs p
    WHERE p.job_id = r.job_id AND p.task_id = r.task_id AND p.agent = r.agent
        AND p.attempt = r.attempt AND p.id <> r.id
        AND p.started_at IS NOT NULL AND p.sandbox_cleared_at IS NULL)"""


async def claim_run(
    conn: AsyncConnection, *, worker_id: str, lease_ttl_sec: int, max_running: int
) -> Row | None:
    """Atomically take the next runnable run, or None if nothing is runnable or the
    platform-wide concurrency cap is reached."""
    async with conn.transaction():
        # Serialise claims so the global cap is exact across workers. Claims are
        # short, so contention on this lock is negligible. The cap counts sandboxes
        # that may exist, not just running rows: a reaped run's sandbox holds Daytona
        # quota until it's confirmed deleted.
        await conn.execute("SELECT pg_advisory_xact_lock(%s)", (CLAIM_LOCK_ID,))
        live = await _one(conn, f"SELECT count(*) AS n FROM runs WHERE {SANDBOX_PENDING}")
        if live["n"] >= max_running:
            return None
        run = await _one(
            conn,
            f"""
            UPDATE runs SET
                status = 'running',
                worker_id = %(worker)s,
                lease_token = lease_token + 1,
                lease_expires_at = now() + make_interval(secs => %(ttl)s),
                started_at = now(),
                updated_at = now()
            WHERE id = (
                SELECT r.id FROM runs r JOIN jobs j ON j.id = r.job_id
                WHERE r.status = 'queued' AND r.not_before <= now() AND j.cancel_requested_at IS NULL
                    AND NOT {EARLIER_TRY_PENDING}
                -- Within a job, oracle runs first, so a Task QA job's grader verdicts
                -- come early. Model runs don't wait for them: they take any slot left.
                ORDER BY j.created_at, r.agent <> 'oracle', r.attempt, r.task_id, r.retry_number
                LIMIT 1
                FOR UPDATE OF r SKIP LOCKED
            )
            RETURNING *
            """,
            {"worker": worker_id, "ttl": lease_ttl_sec},
        )
        if run:
            await log_event(conn, run["id"], "claimed", worker_id=worker_id, lease_token=run["lease_token"])
        return run


async def record_command(conn: AsyncConnection, run: Row, argv: list[str]) -> None:
    """Store the Harbor command line a run is about to execute (see runs.harbor_command)."""
    await conn.execute(
        "UPDATE runs SET harbor_command = %s, updated_at = now()"
        " WHERE id = %s AND lease_token = %s AND status = 'running'",
        (argv, run["id"], run["lease_token"]),
    )


@dataclass(frozen=True)
class Heartbeat:
    owned: bool
    cancel_requested: bool = False


async def heartbeat(conn: AsyncConnection, run_id: UUID, lease_token: int, lease_ttl_sec: int) -> Heartbeat:
    """Extend the lease. owned=False means another actor (the reaper) has taken the
    run over and the caller must stop work and write nothing."""
    row = await _one(
        conn,
        """
        UPDATE runs r SET lease_expires_at = now() + make_interval(secs => %s), updated_at = now()
        FROM jobs j
        WHERE r.id = %s AND r.lease_token = %s AND r.status = 'running' AND j.id = r.job_id
        RETURNING j.cancel_requested_at IS NOT NULL AS cancel_requested
        """,
        (lease_ttl_sec, run_id, lease_token),
    )
    if row is None:
        return Heartbeat(owned=False)
    return Heartbeat(owned=True, cancel_requested=row["cancel_requested"])


async def finish_run(
    conn: AsyncConnection,
    run: Row,
    outcome: Outcome,
    *,
    max_infra_retries: int,
    retry_backoff_sec: int,
) -> bool:
    """Record a terminal outcome. False means the lease was lost and nothing was written."""
    assert outcome.status in TERMINAL
    async with conn.transaction():
        # Read the job first (FOR SHARE, so a cancel either lands before this or waits).
        # A worker only sees a cancel at its next heartbeat, so a run can fail (or be
        # stopped by a shutdown) after the cancel without having been told about it.
        job = await _one(conn, "SELECT cancel_requested_at FROM jobs WHERE id = %s FOR SHARE", (run["job_id"],))
        if job["cancel_requested_at"] is not None and outcome.retryable:
            outcome = _cancelled_instead(outcome)
        row = await _one(
            conn,
            """
            UPDATE runs SET
                status = %(status)s, reward = %(reward)s, rewards = %(rewards)s,
                error_kind = %(error_kind)s, error_message = %(error_message)s, log_tail = %(log_tail)s,
                log_source = %(log_source)s, grade = %(grade)s, agent_sec = %(agent_sec)s,
                cost_usd = %(cost)s, trial_name = %(trial)s, artifact_prefix = %(prefix)s,
                lease_expires_at = NULL, finished_at = now(), updated_at = now()
            WHERE id = %(id)s AND lease_token = %(token)s AND status = 'running'
            RETURNING *
            """,
            {
                "status": outcome.status,
                "reward": outcome.reward,
                "rewards": Jsonb(outcome.rewards) if outcome.rewards is not None else None,
                "error_kind": outcome.error_kind,
                "error_message": outcome.error_message,
                "log_tail": outcome.log_tail,
                "log_source": outcome.log_source,
                "grade": Jsonb(outcome.grade) if outcome.grade is not None else None,
                "agent_sec": outcome.agent_sec,
                "cost": outcome.cost_usd,
                "trial": outcome.trial_name,
                "prefix": outcome.artifact_prefix,
                "id": run["id"],
                "token": run["lease_token"],
            },
        )
        if row is None:
            return False
        await log_event(
            conn, row["id"], "finished", status=outcome.status, error_kind=outcome.error_kind, reward=outcome.reward
        )
        if outcome.retryable:
            await schedule_retry(conn, row, max_retries=max_infra_retries, backoff_sec=retry_backoff_sec)
        return True


def _cancelled_instead(outcome: Outcome) -> Outcome:
    """A failure that would have been retried, in a job someone has cancelled: the
    cancel is why it's final, so it ends cancelled, not failed. What went wrong stays
    in the message. Results (rewards, timeouts) and permanent errors stand as they are."""
    failure = ": ".join(filter(None, [outcome.error_kind, outcome.error_message]))
    return dataclasses.replace(
        outcome, status=RunStatus.CANCELLED, error_kind=None, retryable=False,
        error_message=f"cancelled (it also hit {failure})",
    )


async def schedule_retry(conn: AsyncConnection, run: Row, *, max_retries: int, backoff_sec: int) -> UUID | None:
    """Queue a new row that replaces `run`, with exponential backoff. No-op past the
    cap (automatic retries in a row; a manual retry starts a fresh count), for
    cancelled jobs, or if the retry already exists."""
    if run["auto_retries"] >= max_retries:
        return None
    # FOR SHARE: a cancel that committed between this read and our insert would miss the
    # new row, which would then stay queued forever. With the lock, cancel_job waits for
    # us and cancels the retry, or we wait for it and see the cancel.
    job = await _one(conn, "SELECT cancel_requested_at FROM jobs WHERE id = %s FOR SHARE", (run["job_id"],))
    if job["cancel_requested_at"] is not None:
        return None
    delay = backoff_sec * (2 ** run["auto_retries"])
    row = await _one(
        conn,
        """
        INSERT INTO runs (job_id, task_id, task_digest, agent, model, attempt, retry_number, retry_of,
                          retry_kind, auto_retries, not_before)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'auto', %s, now() + make_interval(secs => %s))
        ON CONFLICT (job_id, task_id, agent, attempt, retry_number) DO NOTHING
        RETURNING id
        """,
        (
            run["job_id"], run["task_id"], run["task_digest"], run["agent"], run["model"],
            run["attempt"], run["retry_number"] + 1, run["id"], run["auto_retries"] + 1, delay,
        ),
    )
    if row:
        await log_event(conn, run["id"], "retry_scheduled", retry_id=row["id"], delay_sec=delay)
        return row["id"]
    return None


class RetryRefused(Exception):
    """A manual retry that can't be queued; the message says why."""


async def manual_retry(conn: AsyncConnection, run_id: UUID, *, requested_by: str) -> tuple[Row, bool] | None:
    """Queue another try of a finished run's task attempt, on a person's request.

    The new row replaces the attempt's latest try (whichever run was named), so a task
    attempt stays one chain of tries. It gets a fresh automatic-retry budget, and like
    every retry it can't start until the previous try's sandbox is deleted. Returns
    (run, created); asking again while that retry is still pending returns it with
    created=False. None if the run doesn't exist.
    """
    async with conn.transaction():
        run = await _one(conn, "SELECT * FROM runs WHERE id = %s", (run_id,))
        if run is None:
            return None
        # The job row lock orders this against cancel_job and against automatic
        # retries, which lock it (FOR SHARE) before inserting theirs. Run rows aren't
        # locked here: finish_run locks its run before the job, so that would deadlock.
        job = await _one(conn, "SELECT * FROM jobs WHERE id = %s FOR UPDATE", (run["job_id"],))
        latest = await _one(
            conn,
            "SELECT * FROM runs WHERE job_id = %s AND task_id = %s AND agent = %s AND attempt = %s"
            " ORDER BY retry_number DESC LIMIT 1",
            (run["job_id"], run["task_id"], run["agent"], run["attempt"]),
        )
        if latest["status"] not in TERMINAL:
            if latest["retry_kind"] == "manual":
                return latest, False
            raise RetryRefused(f"this task attempt already has a try {latest['status']} (run {latest['id']})")
        if job["cancel_requested_at"] is not None:
            active = await _one(
                conn, "SELECT count(*) AS n FROM runs WHERE job_id = %s AND status IN ('queued', 'running')",
                (job["id"],),
            )
            if active["n"]:
                raise RetryRefused("the job is still being cancelled; retry once its runs have stopped")
            # The cancel stopped everything it was meant to stop; this is new work.
            await conn.execute(
                "UPDATE jobs SET cancel_requested_at = NULL, cancelled_by = NULL WHERE id = %s", (job["id"],)
            )
        new = await _one(
            conn,
            """
            INSERT INTO runs (job_id, task_id, task_digest, agent, model, attempt, retry_number, retry_of,
                              retry_kind, requested_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'manual', %s)
            RETURNING *
            """,
            (
                latest["job_id"], latest["task_id"], latest["task_digest"], latest["agent"], latest["model"],
                latest["attempt"], latest["retry_number"] + 1, latest["id"], requested_by,
            ),
        )
        await log_event(
            conn, latest["id"], "retry_requested", by=requested_by, retry_id=new["id"],
            reopened_job=job["cancel_requested_at"] is not None,
        )
        return new, True


async def reap_expired(
    conn: AsyncConnection, *, max_infra_retries: int, retry_backoff_sec: int, limit: int = 50
) -> list[Row]:
    """Take over runs whose worker stopped heartbeating. Returns the reaped rows so
    the caller can destroy their sandboxes."""
    async with conn.transaction():
        expired = await _all(
            conn,
            """
            SELECT * FROM runs
            WHERE status = 'running' AND lease_expires_at < now()
            ORDER BY lease_expires_at
            LIMIT %s
            FOR UPDATE SKIP LOCKED
            """,
            (limit,),
        )
        reaped = []
        for run in expired:
            # Lock the job before deciding, as finish_run does: a cancel landing right
            # now either commits first and is seen here, or waits for this reap (and
            # then cancels the retry it queues). Without it, a cancel committing between
            # the read and the update would leave the run worker_lost in a cancelled job.
            job = await _one(conn, "SELECT cancel_requested_at FROM jobs WHERE id = %s FOR SHARE", (run["job_id"],))
            if job["cancel_requested_at"] is not None:
                status, kind, msg = RunStatus.CANCELLED, None, "cancelled; worker was gone, sandbox cleaned up by reaper"
            else:
                status, kind = RunStatus.ERROR, ErrorKind.WORKER_LOST
                msg = f"worker {run['worker_id']} stopped heartbeating; lease expired at {run['lease_expires_at']}"
            row = await _one(
                conn,
                """
                UPDATE runs SET status = %s, error_kind = %s, error_message = %s,
                    lease_expires_at = NULL, finished_at = now(), updated_at = now()
                WHERE id = %s AND lease_token = %s
                RETURNING *
                """,
                (status, kind, msg, run["id"], run["lease_token"]),
            )
            await log_event(conn, row["id"], "reaped", worker_id=run["worker_id"], status=status)
            if status == RunStatus.ERROR:
                await schedule_retry(conn, row, max_retries=max_infra_retries, backoff_sec=retry_backoff_sec)
            reaped.append(row)
        return reaped


async def runs_awaiting_cleanup(conn: AsyncConnection, limit: int = 100) -> list[UUID]:
    """Finished runs whose sandboxes aren't confirmed deleted yet, oldest first."""
    rows = await _all(
        conn,
        f"SELECT id FROM runs WHERE {SANDBOX_PENDING} AND status NOT IN ('queued', 'running')"
        " ORDER BY finished_at LIMIT %s",
        (limit,),
    )
    return [r["id"] for r in rows]


async def mark_sandbox_cleared(conn: AsyncConnection, run_id: UUID) -> bool:
    """Record that Daytona has no sandbox for this finished run. Only call this once
    its Harbor process can no longer create one: it exited, or its lease expired."""
    async with conn.transaction():
        row = await _one(
            conn,
            f"UPDATE runs SET sandbox_cleared_at = now(), updated_at = now()"
            f" WHERE id = %s AND {SANDBOX_PENDING} AND status NOT IN ('queued', 'running') RETURNING id",
            (run_id,),
        )
        if row:
            await log_event(conn, run_id, "sandbox_cleared")
        return row is not None


async def statuses_for_runs(conn: AsyncConnection, run_ids: list[UUID]) -> dict[UUID, str]:
    rows = await _all(conn, "SELECT id, status FROM runs WHERE id = ANY(%s)", (run_ids,))
    return {r["id"]: r["status"] for r in rows}


async def run_context(conn: AsyncConnection, run: Row) -> Row:
    """Everything a worker needs to execute a run, besides the run row itself."""
    return await _one(
        conn,
        """
        SELECT t.slug, t.overrides, v.config, v.bundle_key, v.digest, j.judge, j.options
        FROM runs r
        JOIN jobs j ON j.id = r.job_id
        JOIN task_versions v ON v.task_id = r.task_id AND v.digest = r.task_digest
        JOIN tasks t ON t.id = r.task_id
        WHERE r.id = %s
        """,
        (run["id"],),
    )


# --- reads for the API and UI ---------------------------------------------------------

# A run is "effective" if nothing replaced it. Job summaries count effective runs so
# a retried failure isn't counted twice.
EFFECTIVE = "NOT EXISTS (SELECT 1 FROM runs c WHERE c.retry_of = r.id)"


def job_status(counts: dict[str, int], cancel_requested: bool) -> str:
    """queued | running | finished | cancelling | cancelled. "finished" says only that
    every run is terminal; the counts say how they ended."""
    total = sum(counts.values())
    active = counts.get(RunStatus.QUEUED, 0) + counts.get(RunStatus.RUNNING, 0)
    if active == 0:
        return "cancelled" if cancel_requested else "finished"
    if cancel_requested:
        return "cancelling"
    if counts.get(RunStatus.QUEUED, 0) == total:
        return "queued"
    return "running"


async def get_job(conn: AsyncConnection, job_id: UUID) -> Row | None:
    job = await _one(conn, "SELECT * FROM jobs WHERE id = %s", (job_id,))
    if job is None:
        return None
    rows = await _all(
        conn,
        f"SELECT status, count(*) AS n FROM runs r"
        f" WHERE job_id = %s AND {EFFECTIVE} GROUP BY status",
        (job_id,),
    )
    counts = {r["status"]: r["n"] for r in rows}
    effective = await _all(
        conn,
        f"SELECT agent, status, error_kind, reward FROM runs r WHERE job_id = %s AND {EFFECTIVE}",
        (job_id,),
    )
    retries = await _all(
        conn, "SELECT retry_kind, count(*) AS n FROM runs WHERE job_id = %s AND retry_of IS NOT NULL GROUP BY 1",
        (job_id,),
    )
    job["counts"] = counts
    job["total_runs"] = sum(counts.values())
    job["status"] = job_status(counts, job["cancel_requested_at"] is not None)
    job.update(summarize(effective, retries={r["retry_kind"]: r["n"] for r in retries}))
    if job["kind"] == JobKind.QA:
        # A QA job's oracle runs check the grader; its pass rate is the model's.
        model = summarize(r for r in effective if r["agent"] != Agent.ORACLE)
        job.update({k: model[k] for k in ("graded_runs", "passed_runs", "pass_rate", "mean_reward")})
    return job


async def list_jobs(conn: AsyncConnection, limit: int = 50) -> list[Row]:
    return await _all(
        conn,
        f"""
        SELECT j.*,
            count(r.id) FILTER (WHERE {EFFECTIVE}) AS total_runs,
            count(r.id) FILTER (WHERE {EFFECTIVE} AND r.status IN ('queued', 'running')) AS active_runs
        FROM jobs j LEFT JOIN runs r ON r.job_id = j.id
        GROUP BY j.id ORDER BY j.created_at DESC LIMIT %s
        """,
        (limit,),
    )


async def list_runs(conn: AsyncConnection, job_id: UUID) -> list[Row]:
    runs = await _all(
        conn,
        f"""
        SELECT r.*, {EFFECTIVE} AS effective,
            r.status = 'queued' AND {EARLIER_TRY_PENDING} AS waiting_for_cleanup,
            extract(epoch FROM coalesce(r.finished_at, now()) - r.started_at) AS duration_sec
        FROM runs r WHERE job_id = %s
        ORDER BY r.task_id, r.agent, r.attempt, r.retry_number
        """,
        (job_id,),
    )
    for r in runs:
        r["outcome"] = run_outcome(r)
    return runs


async def get_run(conn: AsyncConnection, run_id: UUID) -> Row | None:
    run = await _one(
        conn,
        f"""
        SELECT r.*, {EFFECTIVE} AS effective,
            r.status = 'queued' AND {EARLIER_TRY_PENDING} AS waiting_for_cleanup,
            extract(epoch FROM coalesce(r.finished_at, now()) - r.started_at) AS duration_sec,
            (SELECT c.id FROM runs c WHERE c.retry_of = r.id) AS retried_by
        FROM runs r WHERE id = %s
        """,
        (run_id,),
    )
    if run is not None:
        run["outcome"] = run_outcome(run)
    return run


async def job_tasks(conn: AsyncConnection, job_id: UUID) -> list[Row]:
    """Per task in the job: the version it's pinned to, that version's task.toml
    settings, and the Harbor command and synced trial config of its latest try."""
    return await _all(
        conn,
        """
        SELECT DISTINCT ON (r.task_id) r.task_id, r.task_digest AS digest, v.version, v.config,
            (SELECT c.harbor_command FROM runs c
             WHERE c.job_id = r.job_id AND c.task_id = r.task_id AND c.harbor_command IS NOT NULL
             ORDER BY c.started_at DESC LIMIT 1) AS harbor_command,
            (SELECT c.id FROM runs c
             WHERE c.job_id = r.job_id AND c.task_id = r.task_id AND c.artifact_prefix IS NOT NULL
             ORDER BY c.started_at DESC LIMIT 1) AS synced_run_id
        FROM runs r JOIN task_versions v ON v.task_id = r.task_id AND v.digest = r.task_digest
        WHERE r.job_id = %s
        ORDER BY r.task_id, r.created_at
        """,
        (job_id,),
    )


async def flag_reviews(conn: AsyncConnection, job_id: UUID) -> list[Row]:
    return await _all(conn, "SELECT * FROM qa_flag_reviews WHERE job_id = %s", (job_id,))


async def review_flag(
    conn: AsyncConnection, job_id: UUID, task_id: str, code: str, *, verdict: str, note: str, by: str
) -> Row:
    """Record a person's verdict on a Task QA flag, replacing any earlier one."""
    return await _one(
        conn,
        """
        INSERT INTO qa_flag_reviews (job_id, task_id, code, verdict, note, reviewed_by)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (job_id, task_id, code) DO UPDATE SET
            verdict = excluded.verdict, note = excluded.note,
            reviewed_by = excluded.reviewed_by, reviewed_at = now()
        RETURNING *
        """,
        (job_id, task_id, code, verdict, note, by),
    )


async def run_events(conn: AsyncConnection, run_id: UUID) -> list[Row]:
    return await _all(conn, "SELECT at, kind, detail FROM run_events WHERE run_id = %s ORDER BY id", (run_id,))


async def list_environments(conn: AsyncConnection) -> list[Row]:
    return await _all(
        conn,
        "SELECT e.*, (SELECT count(*) FROM tasks t WHERE t.environment_id = e.id) AS task_count"
        " FROM environments e ORDER BY e.id",
    )


async def get_environment(conn: AsyncConnection, env_id: str) -> Row | None:
    return await _one(conn, "SELECT * FROM environments WHERE id = %s", (env_id,))


async def list_tasks(conn: AsyncConnection, env_id: str) -> list[Row]:
    return await _all(
        conn,
        """
        SELECT t.id, t.slug, t.description, t.current_digest AS digest, t.overrides, v.version, v.config,
            v.instruction
        FROM tasks t JOIN task_versions v ON v.task_id = t.id AND v.digest = t.current_digest
        WHERE t.environment_id = %s ORDER BY t.slug
        """,
        (env_id,),
    )


async def get_task(conn: AsyncConnection, task_id: str) -> Row | None:
    return await _one(
        conn,
        """
        SELECT t.id, t.environment_id, t.slug, t.description, t.current_digest AS digest, t.overrides,
            v.version, v.instruction, v.config, v.size_bytes
        FROM tasks t JOIN task_versions v ON v.task_id = t.id AND v.digest = t.current_digest
        WHERE t.id = %s
        """,
        (task_id,),
    )
