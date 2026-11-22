"""Turn a run into a `harbor run` command, and Harbor's trial output into an Outcome.

Each platform run is one `harbor run -n 1` subprocess. The platform owns queueing,
concurrency, retries and state; Harbor owns exactly one trial.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from rollout.config import Settings
from rollout.models import ErrorKind, Outcome, RunStatus

LABEL_MANAGED = "platform.managed"
LABEL_INSTANCE = "platform.instance"
LABEL_RUN = "platform.run_id"
LABEL_JOB = "platform.job_id"

# Harbor's Daytona environment, but creating the sandbox under sandbox_name(), so the
# platform can find it without Daytona's list (see harbor_ext/rollout_daytona.py).
HARBOR_EXT_DIR = Path(__file__).parent / "harbor_ext"
HARBOR_ENVIRONMENT = "rollout_daytona:RolloutDaytonaEnvironment"

LOG_TAIL_CHARS = 4000

# Harbor exception names (harbor 0.25), grouped by what they mean for the platform.
# Only the agent running out of time is a timeout; a grader that does is a grader error.
TIMEOUT_ERRORS = {"AgentTimeoutError"}
SANDBOX_ERRORS = {"EnvironmentStartTimeoutError", "AgentSetupTimeoutError"}
VERIFIER_ERRORS = {
    "RewardFileNotFoundError", "RewardFileEmptyError", "VerifierOutputParseError",
    "AddTestsDirError", "DownloadVerifierDirError", "VerifierTimeoutError",
}
# The model provider failed under the agent: the attempt says nothing about the task.
MODEL_API_TRANSIENT = {
    "ApiRateLimitError", "ApiInternalServerError", "ApiOverloadedError",
    "ApiConnectionClosedError", "ApiResponseStalledError", "UnknownApiError",
}
# Retrying won't help until someone fixes configuration or billing.
MODEL_API_FATAL = {
    "AgentAuthenticationError", "ModelNotFoundError", "ApiUsageLimitError", "ApiProviderResourceNotFoundError",
}


class StopReason(StrEnum):
    """Why the worker stopped Harbor before it exited on its own."""

    CANCELLED = "cancelled"
    SHUTDOWN = "shutdown"
    DEADLINE = "deadline"


def sandbox_labels(instance: str, run: dict[str, Any]) -> dict[str, str]:
    return {
        LABEL_MANAGED: "true",
        LABEL_INSTANCE: instance,
        LABEL_RUN: str(run["id"]),
        LABEL_JOB: str(run["job_id"]),
    }


def sandbox_name(instance: str, run_id: Any) -> str:
    """The one sandbox name a run may use. Daytona names are unique per organization,
    and a run row executes at most once."""
    return f"rollout-{instance}-{run_id}"


def deadline_sec(config: dict, margin_sec: int, agent_timeout_multiplier: float = 1.0) -> float:
    """Platform-side wall clock limit: every phase's own timeout plus a margin for
    agent install and artifact download. Harbor enforces the per-phase timeouts;
    this catches Harbor itself hanging."""
    env = config.get("environment", {})
    agent = config.get("agent", {})
    verifier = config.get("verifier", {})
    return (
        float(env.get("build_timeout_sec", 600))
        + float(agent.get("timeout_sec", 3600)) * max(agent_timeout_multiplier, 1.0)
        + float(verifier.get("timeout_sec", 600))
        + margin_sec
    )


def harbor_command(
    settings: Settings, run: dict[str, Any], task_dir: Path, workdir: Path, *, overrides: dict, judge: bool,
    agent_timeout_multiplier: float | None = None,
) -> list[str]:
    cmd = [
        settings.harbor_bin, "run",
        "-p", str(task_dir),
        "-a", run["agent"],
        "-e", HARBOR_ENVIRONMENT,
        "-n", "1",
        "--job-name", str(run["id"]),
        "-o", str(workdir),
        "-y",
        "--ek", f"sandbox_name={sandbox_name(settings.instance, run['id'])}",
        "--ek", "labels=" + json.dumps(sandbox_labels(settings.instance, run)),
        "--ve", f"JUDGE_MODEL={settings.judge_model}",
    ]
    if run["model"]:
        cmd += ["-m", f"anthropic/{run['model']}"]
    if not judge:
        # Without a key the grader skips LLM-judged criteria (programmatic only).
        cmd += ["--ve", "ANTHROPIC_API_KEY="]
    if overrides.get("cpus"):
        cmd += ["--override-cpus", str(overrides["cpus"])]
    if overrides.get("memory_mb"):
        cmd += ["--override-memory-mb", str(overrides["memory_mb"])]
    if overrides.get("storage_mb"):
        cmd += ["--override-storage-mb", str(overrides["storage_mb"])]
    if agent_timeout_multiplier:
        cmd += ["--agent-timeout-multiplier", str(agent_timeout_multiplier)]
    return cmd


def harbor_env(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HARBOR_EXT_DIR), env.get("PYTHONPATH")]))
    for key, value in {
        "ANTHROPIC_API_KEY": settings.anthropic_api_key,
        "DAYTONA_API_KEY": settings.daytona_api_key,
        "DAYTONA_API_URL": settings.daytona_api_url,
    }.items():
        if value:
            env[key] = value
    return env


def find_trial(workdir: Path, job_name: str) -> tuple[Path | None, dict | None]:
    """Locate the trial directory Harbor wrote and load its result.json if present."""
    job_dir = workdir / job_name
    if not job_dir.is_dir():
        return None, None
    trials = sorted(p for p in job_dir.iterdir() if p.is_dir())
    if not trials:
        return None, None
    trial = trials[0]
    result_path = trial / "result.json"
    if not result_path.is_file():
        return trial, None
    try:
        return trial, json.loads(result_path.read_text())
    except json.JSONDecodeError:
        return trial, None


def read_report(trial_dir: Path | None) -> dict | None:
    """The grader's report.json, if the task's grader wrote one."""
    if trial_dir is None:
        return None
    try:
        report = json.loads((trial_dir / "verifier" / "report.json").read_text())
    except (OSError, ValueError):
        return None
    return report if isinstance(report, dict) else None


def tail(path: Path, chars: int = LOG_TAIL_CHARS) -> str:
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - chars * 4))
            return f.read().decode("utf-8", errors="replace")[-chars:]
    except OSError:
        return ""


def _reward(result: dict) -> tuple[float | None, dict | None]:
    rewards = (result.get("verifier_result") or {}).get("rewards")
    if not rewards:
        return None, None
    if "reward" in rewards:
        return float(rewards["reward"]), rewards
    if len(rewards) == 1:
        return float(next(iter(rewards.values()))), rewards
    return None, rewards


def agent_seconds(result: dict) -> float | None:
    """How long the agent itself worked, from Harbor's agent_execution timing."""
    timing = result.get("agent_execution") or {}
    try:
        started = datetime.fromisoformat(timing["started_at"])
        finished = datetime.fromisoformat(timing["finished_at"])
    except (KeyError, TypeError, ValueError):
        return None
    return max(0.0, (finished - started).total_seconds())


def grade_summary(report: dict | None) -> dict | None:
    """The grader's verdict in brief, from verifier/report.json (see tasks/README.md for
    the format): which criteria failed, which deliverables were missing, and any grader
    error. Criteria nobody decided (passed: null) are listed as ungraded; `judged` names
    the criteria an LLM judge decides, as opposed to programmatic checks."""
    if not isinstance(report, dict):
        return None
    results = [r for r in report.get("results") or [] if isinstance(r, dict)]
    return {
        "failed": [r.get("id") for r in results if r.get("passed") is False],
        "ungraded": [r.get("id") for r in results if r.get("passed") is None],
        "missing": list(report.get("missing_deliverables") or []),
        "error": report.get("error"),
        "judged": [r.get("id") for r in results if r.get("kind") == "judge"],
    }


def judge_problem(report: dict | None, judge: bool) -> tuple[ErrorKind, str] | None:
    """A reward the grader computed without the judge, on a job that asked for it.

    The grader drops criteria it couldn't judge from the reward instead of failing, so a
    judge outage would otherwise look like an ordinary (often better) result."""
    if not judge or not isinstance(report, dict):
        return None
    undecided = [
        r for r in report.get("results") or []
        if isinstance(r, dict) and r.get("kind") == "judge" and r.get("passed") is None
    ]
    failed = [r for r in undecided if str(r.get("note", "")).startswith("judge error")]
    if failed:
        return ErrorKind.JUDGE_API, (
            f"LLM judge failed on {len(failed)} judged criteria, so the reward would have covered "
            f"programmatic checks only: {str(failed[0].get('note'))[:500]}"
        )
    if any(str(r.get("note", "")).startswith("skipped") for r in undecided):
        return ErrorKind.JUDGE_CONFIG, (
            "the job asked for the LLM judge, but the grader ran without ANTHROPIC_API_KEY and skipped "
            "the LLM-judged criteria"
        )
    return None


def classify(
    result: dict | None, *, exit_code: int | None, stop_reason: StopReason | None, deadline: float,
    report: dict | None = None, judge: bool = False,
) -> Outcome:
    outcome = _classify(result, exit_code=exit_code, stop_reason=stop_reason, deadline=deadline)
    outcome.grade = grade_summary(report)
    if outcome.status in (RunStatus.COMPLETED, RunStatus.TIMED_OUT) and (problem := judge_problem(report, judge)):
        outcome.status, (outcome.error_kind, outcome.error_message) = RunStatus.ERROR, problem
        outcome.reward = None  # not a result: it was graded on a different basis
        outcome.retryable = outcome.error_kind == ErrorKind.JUDGE_API
    return outcome


def _classify(
    result: dict | None, *, exit_code: int | None, stop_reason: StopReason | None, deadline: float
) -> Outcome:
    reward, rewards = _reward(result) if result else (None, None)
    exc = (result or {}).get("exception_info") or {}
    etype = exc.get("exception_type")
    emsg = f"{etype}: {exc.get('exception_message', '')}"[:2000] if etype else None
    base = dict(
        reward=reward,
        rewards=rewards,
        agent_sec=agent_seconds(result) if result else None,
        cost_usd=((result or {}).get("agent_result") or {}).get("cost_usd"),
        trial_name=(result or {}).get("trial_name"),
    )

    if stop_reason == StopReason.CANCELLED:
        return Outcome(RunStatus.CANCELLED, error_message="cancelled by user", **base)
    if stop_reason == StopReason.SHUTDOWN:
        return Outcome(
            RunStatus.ERROR, error_kind=ErrorKind.WORKER_SHUTDOWN,
            error_message="worker shut down mid-run", retryable=True, **base,
        )
    if stop_reason == StopReason.DEADLINE:
        # The deadline is the sum of the task's own timeouts plus a margin, all of which
        # Harbor enforces itself. Reaching it means Harbor hung, not that the agent was slow.
        return Outcome(
            RunStatus.ERROR, error_kind=ErrorKind.HARBOR_HANG, retryable=True,
            error_message=f"Harbor still running after the platform deadline of {deadline:.0f}s "
            "(the task's own timeouts plus a margin); stopped it",
            **base,
        )
    if result is None:
        return Outcome(
            RunStatus.ERROR, error_kind=ErrorKind.HARBOR_CRASH,
            error_message=f"harbor exited with code {exit_code} without writing a trial result",
            retryable=True, **base,
        )
    if etype is None:
        if reward is None:
            return Outcome(RunStatus.ERROR, error_kind=ErrorKind.VERIFIER, error_message="grader produced no reward", **base)
        return Outcome(RunStatus.COMPLETED, **base)
    if etype in TIMEOUT_ERRORS:
        return Outcome(RunStatus.TIMED_OUT, error_message=emsg, **base)
    if etype in MODEL_API_TRANSIENT:
        return Outcome(RunStatus.ERROR, error_kind=ErrorKind.MODEL_API, error_message=emsg, retryable=True, **base)
    if etype in MODEL_API_FATAL:
        return Outcome(RunStatus.ERROR, error_kind=ErrorKind.MODEL_API, error_message=emsg, **base)
    if etype.startswith("Daytona") or etype in SANDBOX_ERRORS:
        return Outcome(RunStatus.ERROR, error_kind=ErrorKind.SANDBOX, error_message=emsg, retryable=True, **base)
    if etype in VERIFIER_ERRORS:
        return Outcome(RunStatus.ERROR, error_kind=ErrorKind.VERIFIER, error_message=emsg, **base)
    if reward is not None:
        # The agent failed on its own terms (non-zero exit, context overflow, refusal)
        # and the grader still scored the sandbox: that is a task result.
        return Outcome(RunStatus.COMPLETED, error_message=emsg, **base)
    return Outcome(RunStatus.ERROR, error_kind=ErrorKind.HARBOR_ERROR, error_message=emsg, retryable=True, **base)


# Where to look first for why a run ended the way it did, by error kind. Paths are
# relative to the trial directory; the first that exists wins, else harbor.log.
GRADER_LOGS = ("verifier/test-stdout.txt",)
HARBOR_TRACEBACK = ("exception.txt",)
LOG_FILES: dict[ErrorKind | None, tuple[str, ...]] = {
    ErrorKind.VERIFIER: GRADER_LOGS,
    ErrorKind.JUDGE_API: GRADER_LOGS,
    ErrorKind.JUDGE_CONFIG: GRADER_LOGS,
    ErrorKind.SANDBOX: HARBOR_TRACEBACK,
    ErrorKind.MODEL_API: HARBOR_TRACEBACK,
    ErrorKind.HARBOR_ERROR: HARBOR_TRACEBACK,
}


def pick_log(outcome: Outcome, trial_dir: Path | None, harbor_log: Path) -> tuple[str, str]:
    """(source, tail) of the log that best explains the outcome. harbor.log is Harbor's
    console output, mostly progress; a traceback or the grader's output says more."""
    candidates = LOG_FILES.get(outcome.error_kind, ())
    if outcome.status == RunStatus.TIMED_OUT:
        candidates = HARBOR_TRACEBACK
    for rel in candidates:
        if trial_dir is not None and (trial_dir / rel).is_file():
            return f"trial/{rel}", tail(trial_dir / rel)
    return "harbor.log", tail(harbor_log)
