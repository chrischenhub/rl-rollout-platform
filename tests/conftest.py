"""Shared fixtures. Tests run against a real Postgres (`docker compose up -d postgres`)
in a throwaway `rollout_test` database, and use tests/fake_harbor.py instead of Harbor."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psycopg
import pytest

from rollout import queue
from rollout.api.auth import upsert_user
from rollout.config import Settings
from rollout.db import make_pool, migrate
from rollout.models import TERMINAL
from rollout.store import LocalStore
from rollout.tasks_registry import import_tasks

ADMIN_URL = os.environ.get("TEST_ADMIN_DATABASE_URL", "postgresql://rollout:rollout@localhost:5433/rollout")
TEST_URL = ADMIN_URL.rsplit("/", 1)[0] + "/rollout_test"
HERE = Path(__file__).parent
FAKE_HARBOR = HERE / "fake_harbor.py"
USER = "dev@example.com"


@pytest.fixture(scope="session")
def database_url() -> str:
    try:
        with psycopg.connect(ADMIN_URL, autocommit=True, connect_timeout=3) as conn:
            conn.execute("DROP DATABASE IF EXISTS rollout_test WITH (FORCE)")
            conn.execute("CREATE DATABASE rollout_test")
    except psycopg.OperationalError as e:
        pytest.exit(f"Postgres not reachable at {ADMIN_URL} (run `docker compose up -d postgres`): {e}", 2)
    asyncio.run(migrate(TEST_URL))
    return TEST_URL


# Short timings so lease expiry, reaping and kill escalation happen within seconds.
FAST = dict(
    LEASE_TTL_SEC="3",
    HEARTBEAT_SEC="1",
    POLL_SEC="0.2",
    REAP_INTERVAL_SEC="1",
    KILL_GRACE_SEC="3",
    RETRY_BACKOFF_SEC="0",
    DEADLINE_MARGIN_SEC="60",
    MAX_INFRA_RETRIES="2",
    WORKER_SLOTS="4",
    MAX_RUNNING="8",
)


@pytest.fixture
def settings(database_url: str, tmp_path: Path) -> Settings:
    return dataclasses.replace(
        Settings.from_env(),
        database_url=database_url,
        data_dir=tmp_path / "data",
        instance="test",
        harbor_bin=str(FAKE_HARBOR),
        daytona_api_key=None,
        anthropic_api_key=None,
        allowed_domains=("example.com",),
        lease_ttl_sec=3,
        heartbeat_sec=1,
        max_infra_retries=2,
        retry_backoff_sec=0,
    )


@pytest.fixture
async def pool(settings: Settings):
    async with await psycopg.AsyncConnection.connect(settings.database_url, autocommit=True) as conn:
        await conn.execute(
            "TRUNCATE run_events, runs, jobs, api_tokens, users, task_versions, tasks, environments CASCADE"
        )
    p = make_pool(settings.database_url, max_size=30)
    await p.open(wait=True)
    yield p
    await p.close()


@pytest.fixture
async def tasks(pool, settings: Settings) -> list[str]:
    async with pool.connection() as conn:
        results = await import_tasks(conn, LocalStore(settings.store_dir), HERE / "fixtures" / "tasks")
        await upsert_user(conn, USER)
    return [task_id for task_id, _, _ in results]


class WorkerProcess:
    """A real `rollout worker` subprocess, so tests can SIGKILL it."""

    def __init__(self, settings: Settings, tmp_path: Path, name: str, **env: str):
        self.log_path = tmp_path / f"{name}.log"
        full_env = {
            **os.environ,
            **FAST,
            "DATABASE_URL": settings.database_url,
            "DATA_DIR": str(settings.data_dir),
            "HARBOR_BIN": str(FAKE_HARBOR),
            "ROLLOUT_INSTANCE": "test",
            "DAYTONA_API_KEY": "",
            "FAKE_HARBOR_LEDGER": str(tmp_path / "ledger.txt"),
            **env,
        }
        # cwd is tmp_path so the project's .env / secrets.env are never loaded.
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "rollout.cli", "worker"],
            env=full_env,
            cwd=tmp_path,
            stdout=self.log_path.open("w"),
            stderr=subprocess.STDOUT,
        )

    def kill(self) -> None:
        self.proc.kill()
        self.proc.wait(10)

    def stop(self, timeout: float = 30) -> int:
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        return self.proc.wait(timeout)

    def log(self) -> str:
        return self.log_path.read_text()


@pytest.fixture
def start_worker(settings: Settings, tmp_path: Path):
    started: list[WorkerProcess] = []

    def start(name: str = "worker", **env: str) -> WorkerProcess:
        w = WorkerProcess(settings, tmp_path, name, **env)
        started.append(w)
        return w

    yield start
    for w in started:
        if w.proc.poll() is None:
            w.kill()


async def wait_for(predicate, timeout: float = 30, interval: float = 0.2, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = await predicate()
        if result:
            return result
        await asyncio.sleep(interval)
    raise AssertionError(f"timed out after {timeout}s waiting for {message}")


async def job_runs(pool, job_id) -> list[dict]:
    async with pool.connection() as conn:
        return await queue.list_runs(conn, job_id)


async def wait_job_done(pool, job_id, timeout: float = 60) -> list[dict]:
    async def done():
        runs = await job_runs(pool, job_id)
        return runs if all(r["status"] in TERMINAL for r in runs if r["effective"]) else None

    return await wait_for(done, timeout, message=f"job {job_id} to finish")
