"""`rollout` command: run the API or a worker, and admin tasks."""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging
import os
import socket
import sys
from pathlib import Path

from psycopg.types.json import Jsonb

from rollout.config import Settings, load_env_files
from rollout.db import make_pool, migrate


async def _with_conn(settings: Settings, fn):
    await migrate(settings.database_url)
    pool = make_pool(settings.database_url, max_size=2)
    await pool.open(wait=True)
    try:
        async with pool.connection() as conn:
            return await fn(conn)
    finally:
        await pool.close()


async def _run_worker(settings: Settings) -> None:
    from rollout.sandboxes import Sandboxes
    from rollout.store import LocalStore
    from rollout.worker import Worker

    await migrate(settings.database_url)
    pool = make_pool(settings.database_url, max_size=settings.worker_slots * 2 + 4)
    await pool.open(wait=True)
    try:
        await Worker(settings, pool, LocalStore(settings.store_dir), Sandboxes(settings)).run()
    finally:
        await pool.close()


def cmd_import_tasks(settings: Settings, args) -> None:
    from rollout.store import LocalStore
    from rollout.tasks_registry import import_tasks

    store = LocalStore(settings.store_dir)
    results = asyncio.run(_with_conn(settings, lambda conn: import_tasks(conn, store, args.path)))
    for task_id, digest, new in results:
        print(f"{'new' if new else 'unchanged':9} {task_id:32} {digest[:12]}")


def cmd_set_overrides(settings: Settings, args) -> None:
    overrides = {
        k: v
        for k, v in {"cpus": args.cpus, "memory_mb": args.memory_mb, "storage_mb": args.storage_mb}.items()
        if v is not None
    }

    async def go(conn):
        cur = await conn.execute(
            "UPDATE tasks SET overrides = %s, updated_at = now() WHERE id = %s", (Jsonb(overrides), args.task_id)
        )
        return cur.rowcount

    if not asyncio.run(_with_conn(settings, go)):
        sys.exit(f"unknown task {args.task_id}")
    print(f"{args.task_id}: overrides = {overrides or 'none'}")


def cmd_create_token(settings: Settings, args) -> None:
    from rollout.api.auth import create_token, email_allowed

    if not email_allowed(args.email, settings.allowed_domains, settings.allowed_emails):
        sys.exit(f"{args.email} is not in ALLOWED_EMAIL_DOMAINS or ALLOWED_EMAILS")
    token = asyncio.run(_with_conn(settings, lambda conn: create_token(conn, args.email, args.label)))
    print(token)


def cmd_submit(settings: Settings, args) -> None:
    """Operator path for creating a job (warm-ups, smoke tests). Jobs are attributed to
    a cli@<host> user so the UI shows they didn't come from a signed-in person."""
    from rollout import queue
    from rollout.api.auth import upsert_user
    from rollout.qa import QA_DEFAULT_RUNS

    model = None if args.agent == "oracle" else (args.model or settings.models[0])
    attempts = args.attempts or (QA_DEFAULT_RUNS if args.qa else 1)
    options = {
        k: v for k, v in {
            "agent_timeout_multiplier": args.agent_timeout_multiplier, "cpus": args.cpus, "memory_mb": args.memory_mb,
        }.items() if v is not None
    }

    async def go(conn):
        task_ids = args.task_ids
        if task_ids == ["all"]:
            cur = await conn.execute("SELECT id FROM tasks ORDER BY id")
            task_ids = [r["id"] for r in await cur.fetchall()]
        await upsert_user(conn, args.created_by)
        job_id, _ = await queue.create_job(
            conn, created_by=args.created_by, task_ids=task_ids, agent=args.agent, model=model,
            attempts=attempts, judge=not args.no_judge, options=options, kind="qa" if args.qa else "eval",
        )
        return job_id

    try:
        print(asyncio.run(_with_conn(settings, go)))
    except queue.JobRequestError as e:
        sys.exit(str(e))


def cmd_backfill_grades(settings: Settings, args) -> None:
    """Fill runs.grade from the stored report.json of runs that finished before it existed."""
    from rollout import runner
    from rollout.store import LocalStore

    store = LocalStore(settings.store_dir)

    async def go(conn):
        cur = await conn.execute(
            "SELECT id, artifact_prefix FROM runs WHERE grade IS NULL AND artifact_prefix IS NOT NULL"
        )
        filled = 0
        for row in await cur.fetchall():
            report = runner.read_report(store.path(f"{row['artifact_prefix']}/trial"))
            if report is not None:
                await conn.execute(
                    "UPDATE runs SET grade = %s WHERE id = %s", (Jsonb(runner.grade_summary(report)), row["id"])
                )
                filled += 1
        await conn.commit()
        return f"filled grade for {filled} runs"

    print(asyncio.run(_with_conn(settings, go)))


def cmd_sandboxes(settings: Settings, args) -> None:
    from rollout.sandboxes import Sandboxes

    for sb in asyncio.run(Sandboxes(settings).list_managed()):
        print(f"{sb.id}  run={sb.run_id}  state={sb.state}")


def main(argv: list[str] | None = None) -> None:
    load_env_files()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    parser = argparse.ArgumentParser(prog="rollout")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    p = sub.add_parser("import-tasks", help="register Harbor task directories")
    p.add_argument("path", type=Path, help="a task directory, or a directory of them")
    p = sub.add_parser("set-overrides", help="platform-side resource overrides for a task")
    p.add_argument("task_id")
    p.add_argument("--cpus", type=float)
    p.add_argument("--memory-mb", type=int)
    p.add_argument("--storage-mb", type=int)
    p = sub.add_parser("create-token", help="create a personal API token")
    p.add_argument("email")
    p.add_argument("--label", default="")
    p = sub.add_parser("api", help="serve the web app and JSON API")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p = sub.add_parser("worker", help="claim and execute runs")
    p.add_argument("--slots", type=int, help="concurrent runs in this worker (default WORKER_SLOTS)")
    p = sub.add_parser("submit", help="create a job from the server's command line")
    p.add_argument("task_ids", nargs="+", help="task ids, or 'all'")
    p.add_argument("--agent", default="claude-code", choices=["claude-code", "oracle"])
    p.add_argument("--model")
    p.add_argument("--attempts", type=int, help="attempts per task (default 1; with --qa, model runs per task, default 5)")
    p.add_argument("--qa", action="store_true", help="Task QA: also run the oracle once per task, and report on the tasks")
    p.add_argument("--no-judge", action="store_true", help="programmatic grading only")
    p.add_argument("--as", dest="created_by", default=f"cli@{socket.gethostname()}")
    # For demonstrating failure handling: force an agent timeout, or a sandbox Daytona refuses.
    p.add_argument("--agent-timeout-multiplier", type=float, help="scale the tasks' agent timeouts (e.g. 0.01)")
    p.add_argument("--cpus", type=float, help="override the sandbox CPU count for every task in this job")
    p.add_argument("--memory-mb", type=int, help="override the sandbox memory for every task in this job")
    sub.add_parser("backfill-grades", help="fill in grader summaries for runs graded before they were recorded")
    sub.add_parser("sandboxes", help="list live sandboxes this instance created")
    args = parser.parse_args(argv)

    settings = Settings.from_env()
    if args.cmd == "migrate":
        print(asyncio.run(migrate(settings.database_url)) or "up to date")
    elif args.cmd == "import-tasks":
        cmd_import_tasks(settings, args)
    elif args.cmd == "set-overrides":
        cmd_set_overrides(settings, args)
    elif args.cmd == "create-token":
        cmd_create_token(settings, args)
    elif args.cmd == "submit":
        cmd_submit(settings, args)
    elif args.cmd == "backfill-grades":
        cmd_backfill_grades(settings, args)
    elif args.cmd == "sandboxes":
        cmd_sandboxes(settings, args)
    elif args.cmd == "api":
        import uvicorn

        from rollout.api.app import create_app

        uvicorn.run(create_app(settings), host=args.host, port=args.port, proxy_headers=True, forwarded_allow_ips="*")
    elif args.cmd == "worker":
        if args.slots:
            settings = dataclasses.replace(settings, worker_slots=args.slots)
        asyncio.run(_run_worker(settings))


if __name__ == "__main__":
    main()
