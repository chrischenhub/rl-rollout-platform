"""Harbor's Daytona environment, with the sandbox name chosen by the platform.

Loaded by Harbor, not by the platform: the worker runs
`harbor run -e rollout_daytona:RolloutDaytonaEnvironment --ek sandbox_name=...` with
this directory on PYTHONPATH, so it must only import Harbor.

Why: Harbor never reports the id of the sandbox it creates, and Daytona's list
endpoint can't be relied on to show live sandboxes (on 2026-10-29 it returned none
of ours, by label or unfiltered). A name the platform derives from the run id lets
it delete and confirm by `get(name)` alone. Names are unique per organization, so a
second create under the same name fails instead of making a sandbox nobody tracks.
"""

from harbor.environments.daytona.environment import DaytonaEnvironment


class RolloutDaytonaEnvironment(DaytonaEnvironment):
    def __init__(self, *args, sandbox_name: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._rollout_sandbox_name = sandbox_name

    async def _create_sandbox(self, params, daytona=None):
        params.name = self._rollout_sandbox_name
        return await super()._create_sandbox(params=params, daytona=daytona)
