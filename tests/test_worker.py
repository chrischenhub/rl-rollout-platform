"""End to end: real worker processes (one test drives a worker in-process), fake
Harbor. Covers the crash, cancel, shutdown and hang behaviour the spec asks us to
design for."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from collections import Counter
from pathlib import Path

from conftest import USER, job_runs, wait_for, wait_job_done
from rollout import qa, queue
from rollout.models import ErrorKind, Outcome, RunStatus
from rollout.sandboxes import Sandboxes
from rollout.store import LocalStore
from rollout.worker import RunExecution, Worker


async def make_job(pool, tasks, attempts=2, agent="claude-code", model="claude-sonnet-5"):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=tasks, agent=agent, model=model, attempts=attempts
        )
    return job_id


async def wait_running(pool, job_id, n):
    async def check():
        return sum(r["status"] == RunStatus.RUNNING for r in await job_runs(pool, job_id)) >= n

    await wait_for(check, 20, message=f"{n} running runs")


def ledger(tmp_path: Path) -> Counter:
    path = tmp_path / "ledger.txt"
    return Counter(path.read_text().split()) if path.exists() else Counter()


async def test_job_runs_to_completion(pool, tasks, settings, start_worker, tmp_path):
    job_id = await make_job(pool, tasks, attempts=2)
    worker = start_worker(FAKE_HARBOR_SLEEP="0.3")
    runs = await wait_job_done(pool, job_id)
    assert worker.stop() == 0

    assert len(runs) == 4
    assert all(r["status"] == RunStatus.COMPLETED and r["reward"] == 1.0 for r in runs)
    store = LocalStore(settings.store_dir)
    for r in runs:
        assert store.exists(f"{r['artifact_prefix']}/trial/agent/trajectory.json")
        argv = json.loads(store.path(f"{r['artifact_prefix']}/trial/invocation.json").read_text())
        assert r["harbor_command"][1:] == argv  # the recorded command is what Harbor received
        assert argv[argv.index("-m") + 1] == "anthropic/claude-sonnet-5"
        eks = dict(argv[i + 1].split("=", 1) for i, a in enumerate(argv) if a == "--ek")
        assert eks["sandbox_name"] == f"rollout-test-{r['id']}"
        assert json.loads(eks["labels"])["platform.run_id"] == str(r["id"])
        assert argv[argv.index("-e") + 1] == "rollout_daytona:RolloutDaytonaEnvironment"
    assert set(ledger(tmp_path).values()) == {1}


async def test_outcomes_are_classified(pool, tasks, start_worker):
    job_id = await make_job(pool, tasks, attempts=1)
    start_worker(
        FAKE_HARBOR_SLEEP="0.2", FAKE_HARBOR_MODE_ECHO_A="fail", FAKE_HARBOR_MODE_ECHO_B="sandbox_error",
        MAX_INFRA_RETRIES="1",
    )
    runs = await wait_job_done(pool, job_id)
    by_task = {}
    for r in runs:
        by_task.setdefault(r["task_id"], []).append(r)

    [a] = by_task["testenv/echo-a"]
    assert a["status"] == RunStatus.COMPLETED and a["reward"] == 0.0  # a zero reward is a result
    b = by_task["testenv/echo-b"]
    assert [r["error_kind"] for r in b] == [ErrorKind.SANDBOX, ErrorKind.SANDBOX]
    assert b[1]["retry_of"] == b[0]["id"]  # retry is linked, original kept
    assert b[0]["outcome"] == "infra_error" and b[0]["log_source"] == "trial/exception.txt"
    assert "DaytonaError" in b[0]["log_tail"]


async def test_grading_problems_are_not_results(pool, tasks, start_worker):
    job_id = await make_job(pool, tasks, attempts=1)  # the judge is on
    start_worker(
        FAKE_HARBOR_SLEEP="0.2", FAKE_HARBOR_MODE_ECHO_A="judge_error", FAKE_HARBOR_MODE_ECHO_B="partial",
        MAX_INFRA_RETRIES="1",
    )
    runs = await wait_job_done(pool, job_id)
    a = [r for r in runs if r["task_id"] == "testenv/echo-a"]
    # judge.py would have scored the programmatic checks alone (reward 1.0); the platform
    # treats the outage as infrastructure: no reward, retried, then given up.
    assert [(r["error_kind"], r["reward"]) for r in a] == [(ErrorKind.JUDGE_API, None)] * 2
    assert a[0]["log_source"] == "trial/verifier/test-stdout.txt" and "529" in a[0]["log_tail"]
    [b] = [r for r in runs if r["task_id"] == "testenv/echo-b"]
    assert b["outcome"] == "partial" and b["grade"]["failed"] == ["looks_right"]

    async with pool.connection() as conn:
        job = await queue.get_job(conn, job_id)
    assert job["outcomes"] == {"partial": 1, "infra_error": 1}
    assert (job["graded_runs"], job["pass_rate"], job["mean_reward"]) == (1, 0.0, 0.5)
    assert job["error_kinds"] == {"judge_api": 1} and job["retries"] == {"auto": 1, "manual": 0}


async def test_killed_worker_neither_loses_nor_duplicates_runs(pool, tasks, start_worker, tmp_path):
    job_id = await make_job(pool, tasks, attempts=2)
    first = start_worker("first", FAKE_HARBOR_SLEEP="60")
    await wait_running(pool, job_id, 4)
    first.kill()  # SIGKILL: no cleanup, no goodbye

    start_worker("second", FAKE_HARBOR_SLEEP="0.3")
    runs = await wait_job_done(pool, job_id)

    originals = [r for r in runs if r["retry_of"] is None]
    retries = [r for r in runs if r["retry_of"] is not None]
    assert len(originals) == 4 and all(r["error_kind"] == ErrorKind.WORKER_LOST for r in originals)
    assert len(retries) == 4 and all(r["status"] == RunStatus.COMPLETED for r in retries)
    assert {r["retry_of"] for r in retries} == {r["id"] for r in originals}
    assert set(ledger(tmp_path).values()) == {1}  # every run row executed exactly once
    async with pool.connection() as conn:
        job = await queue.get_job(conn, job_id)
    assert job["status"] == "finished" and job["counts"] == {"completed": 4}


async def test_cancel_stops_running_runs_and_keeps_partial_transcript(pool, tasks, settings, start_worker):
    job_id = await make_job(pool, tasks, attempts=1)
    start_worker(FAKE_HARBOR_SLEEP="60")
    await wait_running(pool, job_id, 2)
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id)

    runs = await wait_job_done(pool, job_id, timeout=20)
    assert all(r["status"] == RunStatus.CANCELLED for r in runs)
    store = LocalStore(settings.store_dir)
    for r in runs:
        transcript = json.loads(store.path(f"{r['artifact_prefix']}/trial/agent/trajectory.json").read_text())
        assert transcript["steps"][-1]["message"] == "partial work before cancel"


async def test_graceful_shutdown_hands_runs_back(pool, tasks, start_worker):
    job_id = await make_job(pool, tasks[:1], attempts=2)
    first = start_worker("first", FAKE_HARBOR_SLEEP="60")
    await wait_running(pool, job_id, 2)
    assert first.stop(timeout=20) == 0

    runs = await job_runs(pool, job_id)
    stopped = [r for r in runs if r["retry_of"] is None]
    assert all(r["error_kind"] == ErrorKind.WORKER_SHUTDOWN for r in stopped)

    start_worker("second", FAKE_HARBOR_SLEEP="0.3")
    runs = await wait_job_done(pool, job_id)
    assert sorted(r["status"] for r in runs if r["effective"]) == ["completed", "completed"]


async def test_hung_harbor_hits_the_deadline_and_is_killed(pool, tasks, start_worker):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    # Fixture timeouts sum to 4s; with no margin and Harbor ignoring SIGTERM the
    # worker must escalate to SIGKILL.
    start_worker(FAKE_HARBOR_MODE="stubborn", DEADLINE_MARGIN_SEC="0", KILL_GRACE_SEC="2", MAX_INFRA_RETRIES="0")
    [run] = await wait_job_done(pool, job_id, timeout=30)
    # Harbor enforces the task's timeouts itself; outliving all of them means it hung,
    # which is the platform's problem, not a slow agent.
    assert run["status"] == RunStatus.ERROR and run["error_kind"] == ErrorKind.HARBOR_HANG
    assert run["outcome"] == "infra_error" and "deadline" in run["error_message"]


async def test_result_survives_the_worker_dying_during_sandbox_cleanup(pool, tasks, settings):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    cleanup_started = asyncio.Event()

    class HangingSandboxes:
        async def ensure_deleted(self, run_id):
            cleanup_started.set()
            await asyncio.Event().wait()

    worker = Worker(settings, pool, LocalStore(settings.store_dir), HangingSandboxes())
    async with pool.connection() as conn:
        run = await queue.claim_run(conn, worker_id=worker.id, lease_ttl_sec=60, max_running=10)
    execution = asyncio.create_task(RunExecution(worker, run).execute())
    await asyncio.wait_for(cleanup_started.wait(), 30)
    # The worker dies mid-cleanup; its lease, if it still holds one, then expires.
    for task in (execution, *worker._background):
        task.cancel()
    await asyncio.gather(execution, *worker._background, return_exceptions=True)
    async with pool.connection() as conn:
        await conn.execute(
            "UPDATE runs SET lease_expires_at = now() - interval '1 second' WHERE id = %s AND status = 'running'",
            (run["id"],),
        )
        reaped = await queue.reap_expired(conn, max_infra_retries=2, retry_backoff_sec=0)
        runs = await queue.list_runs(conn, job_id)

    assert reaped == []  # nothing to redo
    [r] = runs
    assert r["status"] == RunStatus.COMPLETED and r["reward"] == 1.0
    assert r["sandbox_cleared_at"] is None  # the cleanup loop of any worker takes it from here
    assert LocalStore(settings.store_dir).exists(f"{r['artifact_prefix']}/trial/agent/trajectory.json")


async def test_janitor_removes_work_dirs_of_finished_runs(pool, tasks, settings, start_worker):
    await make_job(pool, tasks[:1], attempts=2)
    async with pool.connection() as conn:
        done = await queue.claim_run(conn, worker_id="gone", lease_ttl_sec=60, max_running=10)
        await queue.finish_run(conn, done, Outcome(RunStatus.COMPLETED, reward=1.0), max_infra_retries=2, retry_backoff_sec=0)
        live = await queue.claim_run(conn, worker_id="elsewhere", lease_ttl_sec=60, max_running=10)
    stale, active, other = (settings.work_dir / name for name in (str(done["id"]), str(live["id"]), "notes"))
    for d in (stale, active, other):
        d.mkdir(parents=True)

    start_worker()  # the janitor's first pass runs at startup

    async def removed():
        return not stale.exists()

    await wait_for(removed, 15, message="stale work dir to be removed")
    assert active.exists() and other.exists()


class SlowToDeleteSandboxes:
    """Daytona that keeps listing a run's sandbox for its first few checks."""

    def __init__(self, checks_before_gone: dict):
        self.checks_before_gone = checks_before_gone
        self.checks = Counter()

    async def ensure_deleted(self, run_id):
        self.checks[run_id] += 1
        return self.checks[run_id] > self.checks_before_gone.get(run_id, 0)

    async def list_managed(self):
        return []


async def test_retry_starts_only_after_the_lost_runs_sandbox_is_gone(pool, tasks, settings):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    async with pool.connection() as conn:  # claimed by a worker that then died
        lost = await queue.claim_run(conn, worker_id="dead", lease_ttl_sec=1, max_running=10)
    sandboxes = SlowToDeleteSandboxes({lost["id"]: 3})
    worker = Worker(
        dataclasses.replace(settings, reap_interval_sec=1, poll_sec=0.2),
        pool, LocalStore(settings.store_dir), sandboxes,
    )
    running = asyncio.create_task(worker.run())
    try:
        original, retry = await wait_job_done(pool, job_id)
    finally:
        worker.stopping.set()
        worker._wake.set()
        await running

    assert original["error_kind"] == ErrorKind.WORKER_LOST
    assert sandboxes.checks[lost["id"]] == 4  # three "still there", then gone
    assert retry["status"] == RunStatus.COMPLETED
    assert retry["started_at"] > original["sandbox_cleared_at"]


async def test_harbor_is_killed_before_its_lease_can_expire(pool, tasks, settings, monkeypatch, tmp_path):
    """A worker cut off from Postgres stops its Harbor while it still holds the lease, so
    once anyone can reap the run, nothing is left that could create a sandbox for it."""
    settings = dataclasses.replace(settings, lease_ttl_sec=6, heartbeat_sec=2)
    monkeypatch.setenv("FAKE_HARBOR_SLEEP", "60")
    monkeypatch.setenv("FAKE_HARBOR_LEDGER", str(tmp_path / "ledger.txt"))

    real_heartbeat, calls = queue.heartbeat, []

    async def unreachable_after_launch(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:  # the check just before Harbor starts
            return await real_heartbeat(*args, **kwargs)
        await asyncio.Event().wait()

    monkeypatch.setattr(queue, "heartbeat", unreachable_after_launch)
    await make_job(pool, tasks[:1], attempts=1)
    worker = Worker(settings, pool, LocalStore(settings.store_dir), Sandboxes(settings))
    claimed_at = time.monotonic()
    async with pool.connection() as conn:
        run = await queue.claim_run(conn, worker_id=worker.id, lease_ttl_sec=settings.lease_ttl_sec, max_running=10)
    await asyncio.wait_for(RunExecution(worker, run, claimed_at).execute(), 20)
    elapsed = time.monotonic() - claimed_at

    async with pool.connection() as conn:
        cur = await conn.execute("SELECT status, lease_expires_at > now() AS leased FROM runs WHERE id = %s", (run["id"],))
        row = await cur.fetchone()
    assert ledger(tmp_path)[str(run["id"])] == 1  # Harbor did start
    assert not worker.harbor_groups  # and is dead
    assert row["status"] == RunStatus.RUNNING and row["leased"]  # before the lease ran out
    assert elapsed < settings.lease_ttl_sec


async def test_a_manual_retry_runs_like_any_other(pool, tasks, start_worker):
    job_id = await make_job(pool, tasks[:1], attempts=1)
    start_worker(FAKE_HARBOR_SLEEP="0.2", FAKE_HARBOR_MODE="partial")
    [first] = await wait_job_done(pool, job_id)
    async with pool.connection() as conn:
        new, created = await queue.manual_retry(conn, first["id"], requested_by=USER)
    assert created
    runs = {r["id"]: r for r in await wait_job_done(pool, job_id)}
    first, retry = runs[first["id"]], runs[new["id"]]
    assert retry["outcome"] == "partial" and retry["retry_of"] == first["id"] and not first["effective"]
    assert retry["started_at"] > first["sandbox_cleared_at"]


async def test_a_run_cancelled_before_launch_never_starts_harbor(pool, tasks, settings, monkeypatch, tmp_path):
    monkeypatch.setenv("FAKE_HARBOR_LEDGER", str(tmp_path / "ledger.txt"))
    job_id = await make_job(pool, tasks[:1], attempts=1)
    worker = Worker(settings, pool, LocalStore(settings.store_dir), Sandboxes(settings))
    async with pool.connection() as conn:
        run = await queue.claim_run(conn, worker_id=worker.id, lease_ttl_sec=60, max_running=10)
        await queue.cancel_job(conn, job_id)  # lands after the claim, before any heartbeat
    await RunExecution(worker, run).execute()
    async with pool.connection() as conn:
        [r] = await queue.list_runs(conn, job_id)
    assert r["status"] == RunStatus.CANCELLED and "before Harbor started" in r["error_message"]
    assert not ledger(tmp_path)  # Harbor, and so any sandbox, never started


async def test_cancel_while_the_worker_restarts(pool, tasks, start_worker):
    job_id = await make_job(pool, tasks, attempts=1)
    first = start_worker("first", FAKE_HARBOR_SLEEP="60")
    await wait_running(pool, job_id, 2)
    async with pool.connection() as conn:
        await queue.cancel_job(conn, job_id)
    assert first.stop(timeout=20) == 0  # e.g. a deploy, at the same moment
    start_worker("second")
    runs = await wait_job_done(pool, job_id, timeout=20)
    assert [r["status"] for r in runs] == [RunStatus.CANCELLED] * 2  # cancelled, not failed, no retries


async def test_a_task_qa_job_reports_on_the_tasks(pool, tasks, start_worker):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=tasks, agent="claude-code", model="claude-sonnet-5", attempts=3, kind="qa"
        )
    # The model passes everything; echo-b's grader rejects its own reference solution.
    worker = start_worker(FAKE_HARBOR_SLEEP="0.3", FAKE_HARBOR_MODE_ECHO_B_ORACLE="partial")
    runs = await wait_job_done(pool, job_id)
    assert worker.stop() == 0

    async with pool.connection() as conn:
        job = await queue.get_job(conn, job_id)
        report = qa.report(job, runs, await queue.job_tasks(conn, job_id), [])
    a, b = report["tasks"]
    assert (a["oracle"]["state"], b["oracle"]["state"]) == ("passed", "failed")
    assert [f["code"] for f in a["flags"]] == ["no_signal"]
    assert [f["code"] for f in b["flags"]] == ["oracle_failed", "no_signal"]
    assert "failed looks_right (all decided by the LLM judge" in b["flags"][0]["reason"]
    # Agent time comes from Harbor's agent phase, not the run's wall clock.
    assert all(0.25 <= r["agent_sec"] < r["duration_sec"] for r in runs)
