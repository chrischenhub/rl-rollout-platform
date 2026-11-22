"""Sandbox cleanup against a fake Daytona client: calls that hang, sandboxes already gone."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from uuid import uuid4

from daytona import DaytonaNotFoundError

from rollout import sandboxes
from rollout.runner import sandbox_name
from rollout.sandboxes import Sandboxes


class FakeSandbox:
    def __init__(self, id: str, name: str = "", delete_error: Exception | None = None, state: str = "started"):
        self.id = id
        self.name = name
        self.state = state
        self.labels = {}
        self.delete_error = delete_error
        self.stopped = False
        self.deleted = False

    def delete(self, timeout=None):
        if isinstance(self.delete_error, DaytonaNotFoundError):
            self.deleted = True  # someone else got there first
        if self.delete_error:
            raise self.delete_error
        self.deleted = True

    def stop(self, timeout=None):
        self.stopped = True


class FakeDaytona:
    """Like Daytona on 2026-10-29: get() by id or name works, list() shows nothing."""

    def __init__(self, found=(), hang: threading.Event | None = None):
        self.found = list(found)
        self.hang = hang
        self.request_timeouts = []

    def list(self, query, request_timeout=None):
        self.request_timeouts.append(request_timeout)
        return []

    def get(self, id_or_name, request_timeout=None):
        self.request_timeouts.append(request_timeout)
        if self.hang:
            self.hang.wait()
        for sb in self.found:
            if id_or_name in (sb.id, sb.name) and not sb.deleted:
                return sb
        raise DaytonaNotFoundError(f"Sandbox with ID or name {id_or_name} not found")


def make_sandboxes(settings, client: FakeDaytona) -> Sandboxes:
    sb = Sandboxes(dataclasses.replace(settings, daytona_api_key="test"))
    sb._client = client
    return sb


def run_sandbox(settings, **kw) -> tuple:
    run_id = uuid4()
    return run_id, FakeSandbox(f"sb-{run_id.hex[:6]}", name=sandbox_name(settings.instance, run_id), **kw)


async def test_hung_daytona_calls_cannot_starve_task_setup_or_sync(settings, monkeypatch):
    monkeypatch.setattr(sandboxes, "CALL_TIMEOUT_SEC", 0.2)
    release = threading.Event()
    client = FakeDaytona(hang=release)
    sb = make_sandboxes(settings, client)
    try:
        # More hung calls than asyncio's default pool has threads (at most 32).
        results = await asyncio.gather(*(sb.ensure_deleted(uuid4()) for i in range(40)), return_exceptions=True)
        assert all(isinstance(r, TimeoutError) for r in results)
        # Task setup and artifact sync run on the default pool, which must still be free.
        assert await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 2) == "free"
        assert client.request_timeouts and None not in client.request_timeouts
    finally:
        release.set()


async def test_sandbox_already_gone_is_not_a_cleanup_failure(settings):
    # Harbor deleted it between our lookup and our delete.
    run_id, gone = run_sandbox(settings, delete_error=DaytonaNotFoundError("Sandbox with ID or name sb-1 not found"))
    sb = make_sandboxes(settings, FakeDaytona(found=[gone]))
    assert await sb.ensure_deleted(run_id)
    assert not gone.stopped
    await sb.destroy(gone.id)  # the janitor's path: get() itself says not found


async def test_deleted_means_daytona_has_no_sandbox_of_that_name(settings):
    run_id, live = run_sandbox(settings)
    assert await make_sandboxes(settings, FakeDaytona(found=[live])).ensure_deleted(run_id)
    assert live.deleted

    run_id, destroying = run_sandbox(settings, state="destroying")
    sb = make_sandboxes(settings, FakeDaytona(found=[destroying]))
    assert not await sb.ensure_deleted(run_id)  # on its way out, but still there
    destroying.state = "destroyed"
    assert await sb.ensure_deleted(run_id)

    run_id, busy = run_sandbox(settings, delete_error=Exception("Sandbox state change in progress"))
    assert not await make_sandboxes(settings, FakeDaytona(found=[busy])).ensure_deleted(run_id)

    # Stopping is the fallback when delete fails, but a stopped sandbox isn't deleted.
    run_id, undeletable = run_sandbox(settings, delete_error=Exception("forbidden"))
    assert not await make_sandboxes(settings, FakeDaytona(found=[undeletable])).ensure_deleted(run_id)
    assert undeletable.stopped


async def test_another_runs_sandbox_is_not_this_runs(settings):
    _, other = run_sandbox(settings)
    assert await make_sandboxes(settings, FakeDaytona(found=[other])).ensure_deleted(uuid4())
    assert not other.deleted
