"""Daytona sandbox cleanup, independent of Harbor.

Harbor deletes its sandbox when it exits normally. These calls cover everything
else: a killed or hung Harbor, a crashed worker, a cancelled run. A run's sandbox is
created under a name derived from the run id (runner.sandbox_name), so cleanup never
needs a sandbox id that only a dead process knew, and doesn't depend on Daytona's
list endpoint, which on 2026-10-29 stopped showing live sandboxes at all. Labels are
still set, for the janitor's best-effort sweep and the Daytona dashboard.

The SDK blocks, and a thread stuck in a hung call can't be stopped. Every request
carries its own timeout, and all calls run on this module's small thread pool, so
hung Daytona calls can only delay other Daytona calls, never task setup or artifact
sync, which use asyncio's default pool.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from uuid import UUID

from rollout.config import Settings
from rollout.runner import LABEL_INSTANCE, LABEL_MANAGED, LABEL_RUN, sandbox_name

log = logging.getLogger(__name__)

CALL_TIMEOUT_SEC = 120
REQUEST_TIMEOUT_SEC = 30
THREADS = 4
GONE_STATES = {"destroyed", "destroying"}


def _state(sandbox) -> str:
    state = getattr(sandbox, "state", None)
    return str(getattr(state, "value", state))


def _gone(sandbox) -> bool:
    return _state(sandbox) in GONE_STATES


@dataclass(frozen=True)
class ManagedSandbox:
    id: str
    run_id: UUID | None
    state: str


class Sandboxes:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.enabled = bool(settings.daytona_api_key)
        self._client = None
        self._lock = threading.Lock()
        self._threads = ThreadPoolExecutor(max_workers=THREADS, thread_name_prefix="daytona")

    def _daytona(self):
        with self._lock:
            if self._client is None:
                from daytona import Daytona, DaytonaConfig

                self._client = Daytona(
                    DaytonaConfig(api_key=self.settings.daytona_api_key, api_url=self.settings.daytona_api_url)
                )
            return self._client

    async def _call(self, fn, *args):
        loop = asyncio.get_running_loop()
        return await asyncio.wait_for(loop.run_in_executor(self._threads, fn, *args), CALL_TIMEOUT_SEC)

    def _list(self, labels: dict[str, str]) -> list:
        from daytona import ListSandboxesQuery

        return list(self._daytona().list(ListSandboxesQuery(labels=labels), request_timeout=REQUEST_TIMEOUT_SEC))

    def _destroy(self, sandbox) -> bool:
        """Delete a sandbox. False if there was nothing to do yet: it's already gone, or
        it's mid-transition (usually Harbor deleting it already; the janitor checks again
        later)."""
        from daytona import DaytonaNotFoundError

        try:
            sandbox.delete(timeout=60)
            return True
        except DaytonaNotFoundError:
            return False
        except Exception as e:
            if "state change in progress" in str(e).lower():
                log.info("sandbox %s is mid-transition; leaving it to the janitor", sandbox.id)
                return False
            # Some API keys can't delete. Harbor's sandboxes are ephemeral, so
            # stopping one makes Daytona delete it.
            log.warning("delete of sandbox %s failed (%s); stopping it instead", sandbox.id, e)
            sandbox.stop(timeout=60)
            return True

    def _get(self, name: str):
        """The sandbox with this name, or None if Daytona has no such sandbox left."""
        from daytona import DaytonaNotFoundError

        try:
            sandbox = self._daytona().get(name, request_timeout=REQUEST_TIMEOUT_SEC)
        except DaytonaNotFoundError:
            return None
        return None if _state(sandbox) == "destroyed" else sandbox

    def _ensure_deleted(self, name: str) -> bool:
        sandbox = self._get(name)
        if sandbox is None:
            return True
        if _state(sandbox) != "destroying":
            log.info("deleting sandbox %s (%s, %s)", name, sandbox.id, _state(sandbox))
            self._destroy(sandbox)
        # Only a fresh lookup confirms it: a delete can return before Daytona is done.
        return self._get(name) is None

    def _destroy_by_id(self, sandbox_id: str) -> None:
        from daytona import DaytonaNotFoundError

        try:
            sandbox = self._daytona().get(sandbox_id, request_timeout=REQUEST_TIMEOUT_SEC)
        except DaytonaNotFoundError:
            return
        self._destroy(sandbox)

    async def ensure_deleted(self, run_id: UUID) -> bool:
        """Delete this run's sandbox. True only once Daytona says no sandbox of that
        name exists (or it is destroyed); False means check again later (it is
        mid-transition, or its delete hasn't finished)."""
        if not self.enabled:
            return True
        return await self._call(self._ensure_deleted, sandbox_name(self.settings.instance, run_id))

    async def destroy(self, sandbox_id: str) -> None:
        if self.enabled:
            await self._call(self._destroy_by_id, sandbox_id)

    async def list_managed(self) -> list[ManagedSandbox]:
        """Sandboxes this platform instance created that still exist."""
        if not self.enabled:
            return []
        labels = {LABEL_MANAGED: "true", LABEL_INSTANCE: self.settings.instance}
        found = await self._call(self._list, labels)
        out = []
        for sb in (sb for sb in found if not _gone(sb)):
            try:
                run_id = UUID(sb.labels.get(LABEL_RUN, ""))
            except ValueError:
                run_id = None
            state = getattr(sb, "state", "")
            out.append(ManagedSandbox(id=sb.id, run_id=run_id, state=str(getattr(state, "value", state))))
        return out
