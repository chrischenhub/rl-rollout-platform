"""Status vocabulary shared by the queue, the worker and the API."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"  # graded; reward may be anything in [0, 1], including 0
    ERROR = "error"  # infrastructure or grader failure; see ErrorKind
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"


TERMINAL = frozenset({RunStatus.COMPLETED, RunStatus.ERROR, RunStatus.TIMED_OUT, RunStatus.CANCELLED})


class ErrorKind(StrEnum):
    # Infrastructure: not the model's doing, retried automatically.
    SETUP = "setup"  # task bundle could not be prepared on the worker
    SANDBOX = "sandbox"  # sandbox create/start/build failed
    MODEL_API = "model_api"  # model provider failed (rate limit, overload; auth isn't retried)
    HARBOR_CRASH = "harbor_crash"  # Harbor exited without writing a trial result
    HARBOR_ERROR = "harbor_error"  # Harbor recorded an exception we don't classify further
    HARBOR_HANG = "harbor_hang"  # past the platform deadline: Harbor didn't enforce its own timeouts
    JUDGE_API = "judge_api"  # the LLM judge call failed, so judged criteria went ungraded
    SYNC_FAILED = "sync_failed"  # artifacts could not be saved
    WORKER_LOST = "worker_lost"  # lease expired: worker crashed or was partitioned
    WORKER_SHUTDOWN = "worker_shutdown"  # worker stopped gracefully mid-run
    # Grader: the task's grading is broken or misconfigured. Not retried.
    VERIFIER = "verifier"  # grader failed, timed out or produced no reward
    JUDGE_CONFIG = "judge_config"  # judge was on but the grader had no API key


class Agent(StrEnum):
    CLAUDE_CODE = "claude-code"
    ORACLE = "oracle"


class JobKind(StrEnum):
    EVAL = "eval"  # how well does the model do on these tasks?
    QA = "qa"  # are these tasks any good? K model runs and one oracle run per task


@dataclass
class Outcome:
    """What a finished execution reports back to the queue."""

    status: RunStatus
    reward: float | None = None
    rewards: dict | None = None
    error_kind: ErrorKind | None = None
    error_message: str | None = None
    log_tail: str | None = None
    log_source: str | None = None  # which artifact log_tail is from, e.g. "trial/exception.txt"
    grade: dict | None = None  # the grader's verdict in brief (see runner.grade_summary)
    agent_sec: float | None = None  # Harbor's agent_execution phase
    cost_usd: float | None = None
    trial_name: str | None = None
    artifact_prefix: str | None = None
    retryable: bool = False
