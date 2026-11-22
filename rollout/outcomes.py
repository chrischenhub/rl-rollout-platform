"""What a run's result means, for people: derived from the stored status, never stored.

The stored status says what happened mechanically (completed, error, timed_out, ...).
The outcome says what it means for the evaluation: did the model pass, fail, run out
of time, or did something other than the model go wrong? The job table, its legend,
the API and the job summary all use this one mapping.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from rollout.models import ErrorKind, RunStatus

# Error kinds that mean the task's grading is broken, rather than the infrastructure.
GRADER_ERRORS = frozenset({ErrorKind.VERIFIER, ErrorKind.JUDGE_CONFIG})

# Display order: results first, then what doesn't count, then work in progress.
ORDER = ("passed", "partial", "failed", "timed_out", "infra_error", "grader_error", "cancelled", "running", "queued")
LABELS = {o: o.replace("_", " ") for o in ORDER}
# Outcomes that measure the model, so they count towards pass rate and mean reward.
GRADED = frozenset({"passed", "partial", "failed", "timed_out"})


def run_outcome(run: dict[str, Any]) -> str:
    status = run["status"]
    if status == RunStatus.COMPLETED:
        reward = run["reward"] or 0.0
        return "passed" if reward >= 1.0 else "failed" if reward <= 0.0 else "partial"
    if status == RunStatus.ERROR:
        return "grader_error" if run["error_kind"] in GRADER_ERRORS else "infra_error"
    return str(status)  # timed_out, cancelled, running, queued


def summarize(runs: Iterable[dict[str, Any]], retries: dict[str, int] | None = None) -> dict[str, Any]:
    """Job summary over the effective runs (the latest try of each task attempt).

    Pass rate and mean reward are over graded runs: completed ones, and agent timeouts,
    which count as whatever the grader gave them, or 0 if it didn't run. Infrastructure
    errors, grader errors and cancelled runs say nothing about the model, so they are
    left out and reported separately instead. A pass is the "passed" outcome, so a
    timeout is never one, whatever it scored: the pass count matches the outcome counts.
    """
    runs = list(runs)
    outcomes = Counter(run_outcome(r) for r in runs)
    scores = [r["reward"] or 0.0 for r in runs if run_outcome(r) in GRADED]
    return {
        "outcomes": {o: outcomes[o] for o in ORDER if outcomes[o]},
        "error_kinds": dict(Counter(r["error_kind"] for r in runs if r["status"] == RunStatus.ERROR).most_common()),
        "graded_runs": len(scores),
        "passed_runs": outcomes["passed"],
        "pass_rate": outcomes["passed"] / len(scores) if scores else None,
        "mean_reward": sum(scores) / len(scores) if scores else None,
        "retries": {"auto": (retries or {}).get("auto", 0), "manual": (retries or {}).get("manual", 0)},
    }
