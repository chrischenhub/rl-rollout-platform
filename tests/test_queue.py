"""Queue invariants, tested directly against Postgres."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import pytest

from conftest import USER, wait_for
from rollout import queue
from rollout.models import ErrorKind, Outcome, RunStatus


async def make_job(pool, tasks, attempts=2, **kw):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=tasks, agent="oracle", model=None, attempts=attempts, **kw
        )
    return job_id


async def claim(pool, worker="w1", ttl=60, max_running=100):
    async with pool.connection() as conn:
        return await queue.claim_run(conn, worker_id=worker, lease_ttl_sec=ttl, max_running=max_running)


async def finish(pool, run, outcome, max_retries=2):
    async with pool.connection() as conn:
        return await queue.finish_run(conn, run, outcome, max_infra_retries=max_retries, retry_backoff_sec=0)


async def expire_lease(pool, run_id):
    async with pool.connection() as conn:
        await conn.execute("UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = %s", (run_id,))


async def reap(pool, max_retries=2):
    async with pool.connection() as conn:
        return await queue.reap_expired(conn, max_infra_retries=max_retries, retry_backoff_sec=0)


async def clear(pool, run_id):
    """What a worker records once Daytona lists no sandbox for the run."""
    async with pool.connection() as conn:
        return await queue.mark_sandbox_cleared(conn, run_id)


def done_or_waiting_on_a_lock(pool, task):
    """A wait_for predicate: `task` has finished, or is blocked on a row lock."""

    async def check():
        if task.done():
            return True
        async with pool.connection() as conn:
            cur = await conn.execute(
                "SELECT count(*) AS n FROM pg_stat_activity"
                " WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            return (await cur.fetchone())["n"] > 0

    return check


async def test_create_job_expands_tasks_times_attempts(pool, tasks):
    job_id = await make_job(pool, tasks, attempts=3)
    async with pool.connection() as conn:
        runs = await queue.list_runs(conn, job_id)
        job = await queue.get_job(conn, job_id)
    assert {(r["task_id"], r["attempt"]) for r in runs} == {(t, a) for t in tasks for a in (1, 2, 3)}
    assert job["status"] == "queued" and job["total_runs"] == 6


async def test_idempotency_key_returns_the_same_job(pool, tasks):
    async with pool.connection() as conn:
        first, created1 = await queue.create_job(
            conn, created_by=USER, task_ids=tasks, agent="oracle", model=None, attempts=1, idempotency_key="k1"
        )
        second, created2 = await queue.create_job(
            conn, created_by=USER, task_ids=tasks, agent="oracle", model=None, attempts=1, idempotency_key="k1"
        )
        runs = await queue.list_runs(conn, first)
    assert first == second and created1 and not created2
    assert len(runs) == len(tasks)


async def test_unknown_task_is_rejected(pool, tasks):
    with pytest.raises(queue.JobRequestError, match="unknown task"):
        await make_job(pool, ["testenv/nope"])


async def test_concurrent_claims_never_share_a_run(pool, tasks):
    await make_job(pool, tasks, attempts=5)  # 10 runs
    claims = await asyncio.gather(*(claim(pool, worker=f"w{i}") for i in range(25)))
    ids = [c["id"] for c in claims if c]
    assert len(ids) == len(set(ids)) == 10


async def test_global_cap_limits_running_runs(pool, tasks):
    await make_job(pool, tasks, attempts=3)
    got = [await claim(pool, max_running=2) for _ in range(4)]
    assert [g is not None for g in got] == [True, True, False, False]


async def test_reaper_takes_over_and_fences_the_old_worker(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    run = await claim(pool)
    await expire_lease(pool, run["id"])

    reaped = await reap(pool)
    assert [r["id"] for r in reaped] == [run["id"]]
    assert reaped[0]["status"] == RunStatus.ERROR and reaped[0]["error_kind"] == ErrorKind.WORKER_LOST

    # The old worker comes back: it can neither heartbeat nor write a result.
    async with pool.connection() as conn:
        hb = await queue.heartbeat(conn, run["id"], run["lease_token"], 60)
    assert not hb.owned
    assert not await finish(pool, run, Outcome(RunStatus.COMPLETED, reward=1.0))

    async with pool.connection() as conn:
        runs = await queue.list_runs(conn, job_id)
        job = await queue.get_job(conn, job_id)
    retry = next(r for r in runs if r["retry_of"] == run["id"])
    assert retry["status"] == RunStatus.QUEUED and retry["retry_number"] == 1 and retry["effective"]
    assert job["total_runs"] == 1  # the replaced run is not double counted


async def test_infra_errors_retry_up_to_the_cap(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    sandbox_error = Outcome(RunStatus.ERROR, error_kind=ErrorKind.SANDBOX, retryable=True)
    for _ in range(3):
        run = await claim(pool)
        assert run is not None
        assert await finish(pool, run, sandbox_error, max_retries=2)
        await clear(pool, run["id"])
    assert await claim(pool) is None
    async with pool.connection() as conn:
        runs = await queue.list_runs(conn, job_id)
        job = await queue.get_job(conn, job_id)
    assert [r["retry_number"] for r in runs] == [0, 1, 2]
    assert job["status"] == "finished" and job["counts"] == {"error": 1}  # not "completed": nothing was graded


async def test_task_failure_is_a_result_not_retried(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    run = await claim(pool)
    assert await finish(pool, run, Outcome(RunStatus.COMPLETED, reward=0.0))
    async with pool.connection() as conn:
        runs = await queue.list_runs(conn, job_id)
        job = await queue.get_job(conn, job_id)
    assert len(runs) == 1
    assert job["status"] == "finished" and job["pass_rate"] == 0.0


async def test_cancel_is_idempotent_and_stops_new_claims(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=3)
    running = await claim(pool)
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id)
        await queue.cancel_job(conn, job_id)
        hb = await queue.heartbeat(conn, running["id"], running["lease_token"], 60)
        job = await queue.get_job(conn, job_id)
    assert hb.owned and hb.cancel_requested
    assert job["status"] == "cancelling" and job["counts"] == {"cancelled": 2, "running": 1}
    assert await claim(pool) is None

    assert await finish(pool, running, Outcome(RunStatus.CANCELLED))
    async with pool.connection() as conn:
        job = await queue.get_job(conn, job_id)
    assert job["status"] == "cancelled"


async def test_reaper_cancels_rather_than_retries_a_cancelled_job(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    run = await claim(pool)
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id)
    await expire_lease(pool, run["id"])
    reaped = await reap(pool)
    assert reaped[0]["status"] == RunStatus.CANCELLED
    async with pool.connection() as conn:
        assert len(await queue.list_runs(conn, job_id)) == 1


async def test_cancel_landing_while_a_retry_is_queued_cancels_the_retry(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    run = await claim(pool)
    sandbox_error = Outcome(RunStatus.ERROR, error_kind=ErrorKind.SANDBOX, retryable=True)

    async def cancel():
        async with pool.connection() as conn:
            await queue.cancel_job(conn, job_id)

    async with pool.connection() as conn, conn.transaction():
        # The retry row is inserted but not yet committed when the cancel arrives.
        assert await queue.finish_run(conn, run, sandbox_error, max_infra_retries=2, retry_backoff_sec=0)
        cancelling = asyncio.create_task(cancel())
        await wait_for(done_or_waiting_on_a_lock(pool, cancelling), 5, interval=0.05, message="cancel to run or block")
    await cancelling

    async with pool.connection() as conn:
        job = await queue.get_job(conn, job_id)
    # Otherwise the retry stays queued, no worker claims it, and the job is "cancelling" forever.
    assert job["status"] == "cancelled" and job["counts"] == {"cancelled": 1}


async def test_retry_waits_until_the_reaped_runs_sandbox_is_deleted(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    run = await claim(pool)
    await expire_lease(pool, run["id"])
    await reap(pool)

    # The retry is queued, but the lost run's sandbox (and its agent) may still be alive.
    assert await claim(pool) is None
    async with pool.connection() as conn:
        assert await queue.runs_awaiting_cleanup(conn) == [run["id"]]
        _, waiting = await queue.list_runs(conn, job_id)
    assert waiting["waiting_for_cleanup"]

    assert await clear(pool, run["id"])
    assert not await clear(pool, run["id"])  # recorded once
    retry = await claim(pool)
    assert retry is not None and retry["retry_of"] == run["id"]


async def test_a_manual_retry_also_waits_for_the_previous_sandbox(pool, tasks):
    await make_job(pool, tasks[:1], attempts=1)
    first = await claim(pool)
    assert await finish(pool, first, Outcome(RunStatus.COMPLETED, reward=0.0))  # never retried automatically
    async with pool.connection() as conn:
        await queue.manual_retry(conn, first["id"], requested_by=USER)
    assert await claim(pool) is None
    await clear(pool, first["id"])
    assert (await claim(pool))["retry_number"] == 1


async def retry(pool, run_id):
    async with pool.connection() as conn:
        return await queue.manual_retry(conn, run_id, requested_by=USER)


async def test_manual_retry_of_any_finished_run(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    first = await claim(pool)
    assert await finish(pool, first, Outcome(RunStatus.COMPLETED, reward=1.0))  # even a pass can be rerun
    new, created = await retry(pool, first["id"])
    assert created and new["retry_of"] == first["id"] and new["retry_number"] == 1
    assert (new["retry_kind"], new["requested_by"], new["auto_retries"]) == ("manual", USER, 0)
    again, created = await retry(pool, first["id"])  # a double click
    assert again["id"] == new["id"] and not created

    async with pool.connection() as conn:
        job = await queue.get_job(conn, job_id)
        events = await queue.run_events(conn, first["id"])
    assert job["status"] == "queued" and job["total_runs"] == 1  # the job is live again
    assert job["retries"] == {"auto": 0, "manual": 1}
    assert events[-1]["kind"] == "retry_requested" and events[-1]["detail"]["by"] == USER


async def test_manual_retry_refuses_while_the_attempt_is_still_trying(pool, tasks):
    await make_job(pool, tasks[:1], attempts=1)
    running = await claim(pool)
    with pytest.raises(queue.RetryRefused, match="already has a try running"):
        await retry(pool, running["id"])
    # An automatic retry is queued: that's the attempt's next try already.
    assert await finish(pool, running, Outcome(RunStatus.ERROR, error_kind=ErrorKind.SANDBOX, retryable=True))
    with pytest.raises(queue.RetryRefused, match="already has a try queued"):
        await retry(pool, running["id"])
    assert await retry(pool, uuid4()) is None


async def test_retrying_an_old_try_extends_the_chain(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    first = await claim(pool)
    assert await finish(pool, first, Outcome(RunStatus.ERROR, error_kind=ErrorKind.SANDBOX, retryable=True))
    await clear(pool, first["id"])
    second = await claim(pool)
    assert await finish(pool, second, Outcome(RunStatus.COMPLETED, reward=0.0))
    new, _ = await retry(pool, first["id"])  # named the superseded try
    assert new["retry_of"] == second["id"] and new["retry_number"] == 2
    async with pool.connection() as conn:
        assert sum(r["effective"] for r in await queue.list_runs(conn, job_id)) == 1


async def test_a_manual_retry_gets_a_fresh_automatic_budget(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    sandbox_error = Outcome(RunStatus.ERROR, error_kind=ErrorKind.SANDBOX, retryable=True)
    for _ in range(3):  # the original and two automatic retries all fail: the cap is reached
        run = await claim(pool)
        assert await finish(pool, run, sandbox_error, max_retries=2)
        await clear(pool, run["id"])
    assert await claim(pool) is None
    await retry(pool, run["id"])
    manual = await claim(pool)
    assert await finish(pool, manual, sandbox_error, max_retries=2)
    async with pool.connection() as conn:
        runs = await queue.list_runs(conn, job_id)
    assert [(r["retry_kind"], r["auto_retries"]) for r in runs] == [
        (None, 0), ("auto", 1), ("auto", 2), ("manual", 0), ("auto", 1),
    ]


async def test_retry_in_a_cancelled_job_reopens_it_but_not_while_cancelling(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=2)
    running = await claim(pool)
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id)
        [queued_one] = [r for r in await queue.list_runs(conn, job_id) if r["status"] == RunStatus.CANCELLED]
    with pytest.raises(queue.RetryRefused, match="still being cancelled"):
        await retry(pool, queued_one["id"])
    assert await finish(pool, running, Outcome(RunStatus.CANCELLED))

    new, created = await retry(pool, queued_one["id"])  # never started, so nothing to wait for
    assert created
    assert (await claim(pool))["id"] == new["id"]
    async with pool.connection() as conn:
        job = await queue.get_job(conn, job_id)
    assert job["cancel_requested_at"] is None and job["status"] == "running"


async def test_cap_counts_sandboxes_not_yet_confirmed_deleted(pool, tasks):
    await make_job(pool, tasks[:1], attempts=2)
    first = await claim(pool, max_running=1)
    assert await finish(pool, first, Outcome(RunStatus.COMPLETED, reward=1.0))
    assert await claim(pool, max_running=1) is None  # its sandbox may still hold Daytona quota
    await clear(pool, first["id"])
    assert await claim(pool, max_running=1) is not None


async def test_runs_that_never_started_need_no_cleanup(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=2)
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id)
        assert await queue.runs_awaiting_cleanup(conn) == []


async def test_cancelling_a_finished_job_changes_nothing(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    run = await claim(pool)
    assert await finish(pool, run, Outcome(RunStatus.COMPLETED, reward=1.0))
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id, by=USER)
        job = await queue.get_job(conn, job_id)
    assert job["status"] == "finished" and job["cancel_requested_at"] is None and job["cancelled_by"] is None


async def test_cancel_records_who_asked(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id, by=USER)
        await queue.cancel_job(conn, job_id, by="someone-else@example.com")  # the first request stands
        job = await queue.get_job(conn, job_id)
    assert job["status"] == "cancelled" and job["cancelled_by"] == USER


async def test_a_run_stopped_by_shutdown_in_a_cancelled_job_ends_cancelled(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    run = await claim(pool)
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id)
    shutdown = Outcome(RunStatus.ERROR, error_kind=ErrorKind.WORKER_SHUTDOWN, retryable=True)
    assert await finish(pool, run, shutdown)
    async with pool.connection() as conn:
        [r] = await queue.list_runs(conn, job_id)
        job = await queue.get_job(conn, job_id)
    assert (r["status"], r["error_kind"]) == (RunStatus.CANCELLED, None)  # cancelled, not failed, not retried
    assert job["status"] == "cancelled"


async def test_a_failure_the_cancel_made_final_ends_cancelled_but_results_stand(pool, tasks):
    # Each worker finishes before its next heartbeat would have told it about the cancel.
    job_id = await make_job(pool, tasks[:1], attempts=3)
    runs = [await claim(pool) for _ in range(3)]
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id)
    outcomes = [
        Outcome(RunStatus.ERROR, error_kind=ErrorKind.SANDBOX, error_message="DaytonaError: boom", retryable=True),
        Outcome(RunStatus.ERROR, error_kind=ErrorKind.VERIFIER, error_message="grader crashed"),  # not retryable
        Outcome(RunStatus.TIMED_OUT, reward=0.5),  # a result
    ]
    for run, outcome in zip(runs, outcomes, strict=True):
        assert await finish(pool, run, outcome)
    async with pool.connection() as conn:
        by_id = {r["id"]: r for r in await queue.list_runs(conn, job_id)}
        job = await queue.get_job(conn, job_id)
    ended = [by_id[run["id"]] for run in runs]
    assert [(r["status"], r["error_kind"]) for r in ended] == [
        (RunStatus.CANCELLED, None), (RunStatus.ERROR, ErrorKind.VERIFIER), (RunStatus.TIMED_OUT, None),
    ]
    assert ended[0]["error_message"] == "cancelled (it also hit sandbox: DaytonaError: boom)"
    assert len(by_id) == 3 and job["status"] == "cancelled"  # nothing retried


async def test_cancel_landing_while_the_reaper_decides_ends_the_run_cancelled(pool, tasks):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    run = await claim(pool)
    await expire_lease(pool, run["id"])

    async with pool.connection() as conn, conn.transaction():
        # The cancel is made but not yet committed when the reaper looks at the run.
        await queue.cancel_job(conn, job_id)
        reaping = asyncio.create_task(reap(pool))
        await wait_for(done_or_waiting_on_a_lock(pool, reaping), 5, interval=0.05, message="reap to run or block")
    [reaped] = await reaping

    assert (reaped["status"], reaped["error_kind"]) == (RunStatus.CANCELLED, None)  # not worker_lost
    async with pool.connection() as conn:
        assert len(await queue.list_runs(conn, job_id)) == 1
        assert (await queue.get_job(conn, job_id))["status"] == "cancelled"


async def make_qa_job(pool, tasks, attempts=3):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=tasks, agent="claude-code", model="claude-sonnet-5", attempts=attempts,
            kind="qa",
        )
    return job_id


async def test_a_task_qa_job_adds_one_oracle_run_per_task(pool, tasks):
    job_id = await make_qa_job(pool, tasks, attempts=3)
    async with pool.connection() as conn:
        runs = await queue.list_runs(conn, job_id)
        job = await queue.get_job(conn, job_id)
    assert sorted((r["task_id"], r["agent"], r["attempt"]) for r in runs) == sorted(
        [(t, "claude-code", a) for t in tasks for a in (1, 2, 3)] + [(t, "oracle", 1) for t in tasks]
    )
    assert all(r["model"] is None for r in runs if r["agent"] == "oracle")
    # The oracle checks the same task version the model runs.
    assert all(len({r["task_digest"] for r in runs if r["task_id"] == t}) == 1 for t in tasks)
    assert job["kind"] == "qa" and job["total_runs"] == 8


async def test_task_qa_needs_a_model_and_enough_runs(pool, tasks):
    with pytest.raises(queue.JobRequestError, match="choose a Claude model"):
        await make_job(pool, tasks, attempts=3, kind="qa")  # make_job uses the oracle
    with pytest.raises(queue.JobRequestError, match="at least 3 model runs"):
        await make_qa_job(pool, tasks, attempts=2)
    with pytest.raises(queue.JobRequestError, match="kind must be one of"):
        await make_job(pool, tasks, kind="audit")


async def test_oracle_runs_are_claimed_first(pool, tasks):
    await make_qa_job(pool, tasks, attempts=3)
    first = [await claim(pool) for _ in tasks]
    rest = [await claim(pool) for _ in range(6)]
    assert [r["agent"] for r in first] == ["oracle", "oracle"]
    assert {r["agent"] for r in rest} == {"claude-code"}


async def test_a_task_qa_jobs_pass_rate_is_the_models(pool, tasks):
    job_id = await make_qa_job(pool, tasks[:1], attempts=3)
    for reward in (1.0, 1.0, 0.0, 0.0):  # the oracle comes first
        await finish(pool, await claim(pool), Outcome(RunStatus.COMPLETED, reward=reward, agent_sec=12.5))
    async with pool.connection() as conn:
        job = await queue.get_job(conn, job_id)
        runs = await queue.list_runs(conn, job_id)
    assert (job["passed_runs"], job["graded_runs"]) == (1, 3)
    assert job["outcomes"] == {"passed": 2, "failed": 2}  # progress still counts every run
    assert {r["agent_sec"] for r in runs} == {12.5}
