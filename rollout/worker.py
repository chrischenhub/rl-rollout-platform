"""Worker: claims runs, executes each as a supervised `harbor run`, reports back.

A worker can die at any point. Correctness comes from the lease, not from the
worker finishing: a run whose lease expires is reaped by any live worker
(see queue.reap_expired), and its sandbox is destroyed by label.

A retry must not start while an earlier try's sandbox may be alive (its agent would
still be running, unfenced). Two things make that hold:
- A worker kills its Harbor before its lease can expire (the fence, below), so once
  a run is finished or reaped, nothing can create a sandbox for it any more.
- Every finished run's sandbox is deleted, by the name the run gave it, and then
  confirmed gone by a fresh lookup before sandbox_cleared_at is set; the queue won't
  claim another try of that task attempt until then. Any worker retries the check
  every reap interval.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import signal
import socket
import time
import uuid
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from rollout import queue, runner
from rollout.config import Settings
from rollout.models import ErrorKind, Outcome, RunStatus
from rollout.runner import StopReason
from rollout.sandboxes import Sandboxes
from rollout.store import LocalStore
from rollout.tasks_registry import materialize

log = logging.getLogger("rollout.worker")


class Worker:
    def __init__(self, settings: Settings, pool: AsyncConnectionPool, store: LocalStore, sandboxes: Sandboxes):
        self.settings = settings
        self.pool = pool
        self.store = store
        self.sandboxes = sandboxes
        self.id = f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self.active: dict[UUID, asyncio.Task] = {}
        self.stopping = asyncio.Event()
        self._stop_signal_at = 0.0
        self._wake = asyncio.Event()
        self._background: set[asyncio.Task] = set()
        self._clearing: set[UUID] = set()
        self.harbor_groups: set[int] = set()

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, self._on_signal, sig)
        log.info(
            "worker %s starting: %d slots, global cap %d, lease %ds",
            self.id, self.settings.worker_slots, self.settings.max_running, self.settings.lease_ttl_sec,
        )
        loops = [asyncio.create_task(self._maintenance_loop()), asyncio.create_task(self._cleanup_loop())]
        try:
            await self._claim_loop()
        finally:
            if self.active:
                log.info("stopping %d active runs", len(self.active))
                await asyncio.gather(*self.active.values(), return_exceptions=True)
            for loop_task in loops:
                loop_task.cancel()
            await asyncio.gather(*loops, *self._background, return_exceptions=True)
            log.info("worker %s stopped", self.id)

    def _on_signal(self, sig: signal.Signals) -> None:
        if self.stopping.is_set():
            # Wrappers like `uv run` forward the same signal we already received from
            # the terminal or pkill; only a deliberate second signal forces an exit.
            if time.monotonic() - self._stop_signal_at < 1.0:
                return
            log.warning("second %s; killing harbor and exiting without cleanup", sig.name)
            for group in self.harbor_groups:
                try:
                    os.killpg(group, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            os._exit(1)
        log.info("received %s: no new claims; stopping active runs (signal again to force)", sig.name)
        self._stop_signal_at = time.monotonic()
        self.stopping.set()
        self._wake.set()

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _claim_loop(self) -> None:
        while not self.stopping.is_set():
            run = None
            if len(self.active) < self.settings.worker_slots:
                claimed_at = time.monotonic()  # no later than the lease's start
                try:
                    async with self.pool.connection() as conn:
                        run = await queue.claim_run(
                            conn,
                            worker_id=self.id,
                            lease_ttl_sec=self.settings.lease_ttl_sec,
                            max_running=self.settings.max_running,
                        )
                except Exception:
                    log.exception("claim failed")
            if run is not None:
                log.info("claimed run %s (%s attempt %d)", run["id"], run["task_id"], run["attempt"])
                self.active[run["id"]] = asyncio.create_task(self._execute(run, claimed_at), name=f"run-{run['id']}")
                continue  # try to fill the next free slot straight away
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.poll_sec)
            except TimeoutError:
                pass

    async def _execute(self, run: dict[str, Any], claimed_at: float) -> None:
        try:
            await RunExecution(self, run, claimed_at).execute()
        except Exception:
            # The lease will expire and the reaper will retry the run.
            log.exception("run %s: unexpected worker error", run["id"])
        finally:
            self.active.pop(run["id"], None)
            self._wake.set()

    async def _maintenance_loop(self) -> None:
        last_janitor = float("-inf")
        while True:
            try:
                async with self.pool.connection() as conn:
                    reaped = await queue.reap_expired(
                        conn,
                        max_infra_retries=self.settings.max_infra_retries,
                        retry_backoff_sec=self.settings.retry_backoff_sec,
                    )
                for run in reaped:
                    log.warning("reaped run %s from %s -> %s", run["id"], run["worker_id"], run["status"])
                    self._spawn(self._clear_sandboxes(run["id"]))
            except Exception:
                log.exception("reap failed")
            if time.monotonic() - last_janitor > self.settings.janitor_interval_sec:
                last_janitor = time.monotonic()
                try:
                    await self._janitor()
                except Exception:
                    log.exception("janitor failed")
            await asyncio.sleep(self.settings.reap_interval_sec)

    async def _clear_sandboxes(self, run_id: UUID) -> None:
        """Delete a finished run's sandbox; once Daytona has none left, record it, which
        frees its slot under the cap and lets a retry of it start."""
        if run_id in self._clearing:
            return
        self._clearing.add(run_id)
        try:
            if not await self.sandboxes.ensure_deleted(run_id):
                log.info("run %s: sandbox not gone yet; checking again in %ds", run_id, self.settings.reap_interval_sec)
                return
            async with self.pool.connection() as conn:
                if await queue.mark_sandbox_cleared(conn, run_id):
                    log.info("run %s: sandboxes confirmed deleted", run_id)
                    self._wake.set()  # a slot, and maybe a retry, is free
        except Exception as e:
            log.warning("run %s: sandbox cleanup failed (%s); will retry", run_id, e)
        finally:
            self._clearing.discard(run_id)

    async def _cleanup_loop(self) -> None:
        """Keep checking every finished run whose sandboxes aren't confirmed deleted,
        whichever worker ran it. Separate from reaping, so a slow Daytona can't delay
        reaps."""
        while True:
            try:
                async with self.pool.connection() as conn:
                    pending = await queue.runs_awaiting_cleanup(conn)
                for run_id in pending:
                    await self._clear_sandboxes(run_id)
            except Exception:
                log.exception("sandbox cleanup pass failed")
            await asyncio.sleep(self.settings.reap_interval_sec)

    async def _inactive(self, run_ids: list[UUID]) -> set[UUID]:
        async with self.pool.connection() as conn:
            statuses = await queue.statuses_for_runs(conn, run_ids)
        return {r for r in run_ids if statuses.get(r) not in (RunStatus.QUEUED, RunStatus.RUNNING)}

    async def _janitor(self) -> None:
        """Delete work dirs (left behind by killed workers) and destroy labelled
        sandboxes whose run is no longer active."""
        dirs = {}
        for path in self.settings.work_dir.glob("*"):
            try:
                dirs[UUID(path.name)] = path
            except ValueError:
                continue
        for run_id in await self._inactive([r for r in dirs if r not in self.active]):
            log.info("janitor: removing work dir of run %s", run_id)
            shutil.rmtree(dirs[run_id], ignore_errors=True)

        found = await self.sandboxes.list_managed()
        if not found:
            return
        inactive = await self._inactive([s.run_id for s in found if s.run_id])
        for sb in found:
            if sb.run_id and sb.run_id not in inactive:
                continue
            log.warning("janitor: destroying orphaned sandbox %s (run %s)", sb.id, sb.run_id)
            try:
                await self.sandboxes.destroy(sb.id)
            except Exception as e:
                log.warning("janitor: could not destroy %s: %s", sb.id, e)


class RunExecution:
    """One claimed run, from task setup to recorded outcome."""

    def __init__(self, worker: Worker, run: dict[str, Any], claimed_at: float | None = None):
        self.worker = worker
        self.settings = worker.settings
        self.run = run
        self.run_id: UUID = run["id"]
        self.token: int = run["lease_token"]
        self.workdir: Path = self.settings.work_dir / str(self.run_id)
        self.owned = True
        self.cancel_requested = False
        # The fence: Harbor is killed at this (monotonic) time unless a heartbeat moves
        # it. It's one heartbeat interval earlier than the lease can expire, counted
        # from before the request that set the lease, so Harbor is always dead before
        # the reaper can take the run over and clean up its sandboxes.
        self.fence_at = self._fence_from(time.monotonic() if claimed_at is None else claimed_at)

    def _fence_from(self, sent_at: float) -> float:
        return sent_at + self.settings.lease_ttl_sec - self.settings.heartbeat_sec

    async def execute(self) -> None:
        keep_lease = asyncio.create_task(self._keep_lease())
        try:
            outcome = await self._attempt()
            if outcome is None or not self.owned:
                log.warning("run %s: lease lost; the reaper owns this run now", self.run_id)
                return
            await self._finish(outcome)
            # Only once the outcome is recorded, so a worker dying during cleanup can't
            # cost a finished result. Harbor deletes its sandbox on a normal exit, so this
            # usually just confirms it; it also covers kills, hangs and Harbor bugs. In
            # the background, so a slow Daytona call doesn't hold this worker's slot; if
            # it doesn't confirm, the cleanup loop keeps trying.
            self.worker._spawn(self.worker._clear_sandboxes(self.run_id))
        finally:
            keep_lease.cancel()
            await asyncio.gather(keep_lease, return_exceptions=True)
            shutil.rmtree(self.workdir, ignore_errors=True)

    async def _keep_lease(self) -> None:
        while self.owned:
            await asyncio.sleep(self.settings.heartbeat_sec)
            sent_at = time.monotonic()
            try:
                async with asyncio.timeout(self.settings.heartbeat_sec):
                    async with self.worker.pool.connection() as conn:
                        hb = await queue.heartbeat(conn, self.run_id, self.token, self.settings.lease_ttl_sec)
            except Exception as e:
                log.warning("run %s: heartbeat failed: %r", self.run_id, e)
                if time.monotonic() >= self.fence_at:
                    self.owned = False  # the lease may expire any moment now
                    return
                continue
            self.fence_at = self._fence_from(sent_at)
            if not hb.owned:
                self.owned = False
                return
            if hb.cancel_requested and not self.cancel_requested:
                log.info("run %s: cancel requested", self.run_id)
            self.cancel_requested = hb.cancel_requested

    async def _attempt(self) -> Outcome | None:
        s = self.settings
        try:
            async with self.worker.pool.connection() as conn:
                ctx = await queue.run_context(conn, self.run)
            task_dir = await asyncio.to_thread(
                materialize, self.worker.store, s.task_cache_dir, ctx["slug"], ctx["digest"], ctx["bundle_key"]
            )
        except Exception as e:
            log.exception("run %s: task setup failed", self.run_id)
            return Outcome(RunStatus.ERROR, error_kind=ErrorKind.SETUP, error_message=f"task setup failed: {e}", retryable=True)

        shutil.rmtree(self.workdir, ignore_errors=True)
        self.workdir.mkdir(parents=True)
        log_path = self.workdir / "harbor.log"
        options = ctx["options"] or {}
        overrides = {**(ctx["overrides"] or {}), **{k: options[k] for k in ("cpus", "memory_mb") if k in options}}
        multiplier = options.get("agent_timeout_multiplier")
        cmd = runner.harbor_command(
            s, self.run, task_dir, self.workdir, overrides=overrides, judge=ctx["judge"],
            agent_timeout_multiplier=multiplier,
        )
        deadline = runner.deadline_sec(ctx["config"], s.deadline_margin_sec, multiplier or 1.0)
        try:
            async with self.worker.pool.connection() as conn:
                await queue.record_command(conn, self.run, cmd)
        except Exception as e:  # informational only: not worth failing the run over
            log.warning("run %s: could not record the harbor command: %s", self.run_id, e)

        # Last look before anything touches Daytona: a cancel that landed after this run
        # was claimed (and before a heartbeat noticed) must not start a sandbox.
        try:
            async with asyncio.timeout(s.heartbeat_sec):
                async with self.worker.pool.connection() as conn:
                    hb = await queue.heartbeat(conn, self.run_id, self.token, s.lease_ttl_sec)
        except Exception as e:
            return Outcome(
                RunStatus.ERROR, error_kind=ErrorKind.SETUP, retryable=True,
                error_message=f"could not confirm the run was still wanted before starting Harbor: {e}",
            )
        if not hb.owned:
            return None
        if hb.cancel_requested:
            log.info("run %s: job cancelled before harbor started", self.run_id)
            return Outcome(RunStatus.CANCELLED, error_message="cancelled before Harbor started; no sandbox was created")

        with log_path.open("wb") as log_file:
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=asyncio.subprocess.STDOUT,
                    env=runner.harbor_env(s),
                    cwd=self.workdir,
                    start_new_session=True,  # its own process group, so we can signal all of it
                )
            except OSError as e:
                return Outcome(
                    RunStatus.ERROR, error_kind=ErrorKind.HARBOR_CRASH,
                    error_message=f"could not start harbor: {e}", retryable=True,
                )
            log.info("run %s: harbor pid %d, deadline %.0fs", self.run_id, proc.pid, deadline)
            self.worker.harbor_groups.add(proc.pid)
            try:
                stop_reason = await self._supervise(proc, time.monotonic() + deadline)
            finally:
                # Even if supervising failed or this task was cancelled: an orphaned
                # Harbor could still create a sandbox after the run was cleaned up.
                if proc.returncode is None:
                    self._signal(proc, signal.SIGKILL)
                    await proc.wait()
                self.worker.harbor_groups.discard(proc.pid)
        if not self.owned:
            return None

        trial_dir, result = runner.find_trial(self.workdir, str(self.run_id))
        report = runner.read_report(trial_dir)
        outcome = runner.classify(
            result, exit_code=proc.returncode, stop_reason=stop_reason, deadline=deadline,
            report=report, judge=ctx["judge"],
        )
        outcome.log_source, outcome.log_tail = runner.pick_log(outcome, trial_dir, log_path)

        prefix = f"runs/{self.run_id}"
        try:
            await asyncio.to_thread(self._sync, prefix, trial_dir, log_path)
            outcome.artifact_prefix = prefix
        except Exception as e:
            log.exception("run %s: artifact sync failed", self.run_id)
            if outcome.status == RunStatus.COMPLETED:
                outcome.status, outcome.error_kind, outcome.retryable = RunStatus.ERROR, ErrorKind.SYNC_FAILED, True
            outcome.error_message = "; ".join(filter(None, [outcome.error_message, f"artifact sync failed: {e}"]))
        return outcome

    async def _supervise(self, proc: asyncio.subprocess.Process, deadline: float) -> StopReason | None:
        """Wait for Harbor, stopping it on cancel, worker shutdown, deadline or lost lease."""
        reason: StopReason | None = None
        stop_sent_at = 0.0
        while True:
            try:
                await asyncio.wait_for(proc.wait(), timeout=min(1.0, max(0.0, self.fence_at - time.monotonic())))
                return reason
            except TimeoutError:
                pass
            if self.owned and time.monotonic() >= self.fence_at:
                log.warning("run %s: lease not renewed in time; killing harbor before it can expire", self.run_id)
                self.owned = False
            if not self.owned:
                self._signal(proc, signal.SIGKILL)
                await proc.wait()
                return reason
            if reason is None:
                if self.cancel_requested:
                    reason = StopReason.CANCELLED
                elif self.worker.stopping.is_set():
                    reason = StopReason.SHUTDOWN
                elif time.monotonic() > deadline:
                    reason = StopReason.DEADLINE
                if reason is not None:
                    log.info("run %s: stopping harbor (%s)", self.run_id, reason)
                    # Harbor handles SIGTERM by cancelling the trial: it saves what it
                    # has (partial transcript included) and stops the sandbox.
                    self._signal(proc, signal.SIGTERM)
                    stop_sent_at = time.monotonic()
            elif time.monotonic() - stop_sent_at > self.settings.kill_grace_sec:
                log.warning("run %s: harbor ignored SIGTERM for %ds; killing", self.run_id, self.settings.kill_grace_sec)
                self._signal(proc, signal.SIGKILL)

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, sig: signal.Signals) -> None:
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass

    def _sync(self, prefix: str, trial_dir: Path | None, log_path: Path) -> None:
        if trial_dir is not None:
            self.worker.store.put_dir(f"{prefix}/trial", trial_dir)
        self.worker.store.put_file(f"{prefix}/harbor.log", log_path)

    async def _finish(self, outcome: Outcome) -> None:
        for attempt in range(6):
            try:
                async with self.worker.pool.connection() as conn:
                    ok = await queue.finish_run(
                        conn,
                        self.run,
                        outcome,
                        max_infra_retries=self.settings.max_infra_retries,
                        retry_backoff_sec=self.settings.retry_backoff_sec,
                    )
                if ok:
                    log.info(
                        "run %s: %s reward=%s %s",
                        self.run_id, outcome.status, outcome.reward, outcome.error_kind or "",
                    )
                else:
                    log.warning("run %s: lease lost before finish; outcome discarded", self.run_id)
                return
            except Exception:
                log.exception("run %s: could not record outcome (attempt %d)", self.run_id, attempt + 1)
                if not self.owned:
                    return
                await asyncio.sleep(2**attempt)
