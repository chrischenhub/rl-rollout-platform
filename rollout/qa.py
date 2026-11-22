"""Task QA: judge the tasks instead of the model.

A QA job runs each task K times with a model and once with the oracle (the task's
reference solution). This module turns those runs into a report per task, and flags
tasks that look broken or useless for training, each with a reason a person can check.
The report is derived from the runs on every read, never stored, so a retry that
changes a run changes the report; only people's verdicts on flags are stored.

Counting follows rollout.outcomes, like the job summary: a timeout counts as whatever
the grader gave it; infrastructure errors, grader errors and cancelled runs say nothing
about the task and are left out (and shown as such).
"""

from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass
from typing import Any

from rollout.api.timefmt import duration
from rollout.models import TERMINAL, Agent
from rollout.outcomes import GRADED, run_outcome, summarize

QA_MIN_RUNS = 3  # fewer model runs per task can't show variation
QA_DEFAULT_RUNS = 5


@dataclass(frozen=True)
class Thresholds:
    # no_signal: an all-pass or all-fail streak shorter than this means little.
    signal_min_runs: int = 2
    # no_signal on all-fail: with rewards spread this widely, partial credit still gives
    # signal (RL learns from rewards differing between attempts), so it isn't raised.
    partial_spread: float = 0.2
    # duration_variance: over this many finished runs, the slowest took this many times
    # the fastest, and at least this much longer (so 20 s vs 70 s on a trivial task isn't
    # flagged). Timed-out runs are left out: they stopped at the limit, and `timeouts` covers them.
    duration_min_runs: int = 3
    duration_ratio: float = 3.0
    duration_min_spread_sec: float = 120.0
    # timeouts: at least this many graded runs hit the agent time limit, and this share of them.
    timeout_min_runs: int = 2
    timeout_min_rate: float = 0.3
    # A run that finished after this share of its limit nearly timed out.
    near_timeout: float = 0.9


THRESHOLDS = Thresholds()

FLAG_LABELS = {
    "oracle_failed": "oracle failed",
    "no_signal": "no signal",
    "duration_variance": "duration varies",
    "timeouts": "timeouts",
}

# The oracle's run outcome, as a verdict on the grader. An infrastructure error says
# nothing about the grader, so the check is inconclusive until the oracle is retried.
ORACLE_STATES = {"partial": "failed", "infra_error": "inconclusive"}
ORACLE_FAILED = ("failed", "timed_out", "grader_error")

NOT_COUNTED = ("infra_error", "grader_error", "cancelled")


def report(
    job: dict[str, Any], runs: list[dict[str, Any]], tasks: list[dict[str, Any]], reviews: list[dict[str, Any]],
    thresholds: Thresholds = THRESHOLDS,
) -> dict[str, Any]:
    """The Task QA report for a job, from its runs (as queue.list_runs returns them), its
    tasks (queue.job_tasks) and the flag reviews so far (queue.flag_reviews)."""
    multiplier = (job.get("options") or {}).get("agent_timeout_multiplier")
    base_limits = {t["task_id"]: (t["config"].get("agent") or {}).get("timeout_sec") for t in tasks}
    by_task: dict[str, list[dict[str, Any]]] = {}
    for r in runs:
        if r["effective"]:
            by_task.setdefault(r["task_id"], []).append(r)
    reviewed = {(v["task_id"], v["code"]): v for v in reviews}
    rows = [
        task_report(
            task_id, task_runs, base_limit_sec=base_limits.get(task_id), multiplier=multiplier,
            reviews={code: v for (t, code), v in reviewed.items() if t == task_id}, t=thresholds,
        )
        for task_id, task_runs in by_task.items()
    ]
    flags = [f for row in rows for f in row["flags"]]
    return {
        "tasks": rows,
        "flagged_tasks": sum(bool(row["flags"]) for row in rows),
        "flags": len(flags),
        "reviewed_flags": sum(f["review"] is not None for f in flags),
        "thresholds": asdict(thresholds),
    }


def task_report(
    task_id: str, runs: list[dict[str, Any]], *, base_limit_sec: float | None, multiplier: float | None,
    reviews: dict[str, dict[str, Any]], t: Thresholds = THRESHOLDS,
) -> dict[str, Any]:
    """One task's numbers and flags, over the latest try of each of its runs."""
    model = [r for r in runs if r["agent"] != Agent.ORACLE]
    oracle = next((r for r in runs if r["agent"] == Agent.ORACLE), None)
    summary = summarize(model)
    graded = [r for r in model if run_outcome(r) in GRADED]
    rewards = [r["reward"] or 0.0 for r in graded]
    timed_out = [r for r in graded if run_outcome(r) == "timed_out"]
    finished = [r["agent_sec"] for r in graded if run_outcome(r) != "timed_out" and r.get("agent_sec") is not None]
    limit = base_limit_sec * multiplier if base_limit_sec and multiplier else base_limit_sec
    near = sum(sec >= t.near_timeout * limit for sec in finished) if limit else 0

    row = {
        "task_id": task_id,
        "done": all(r["status"] in TERMINAL for r in runs),
        "finished_runs": sum(r["status"] in TERMINAL for r in runs),
        "total_runs": len(runs),
        "model_runs": len(model),
        "graded_runs": summary["graded_runs"],
        "passed_runs": summary["passed_runs"],
        "pass_rate": summary["pass_rate"],
        "mean_reward": summary["mean_reward"],
        "reward_range": [min(rewards), max(rewards)] if rewards else None,
        "not_counted": {o: n for o, n in summary["outcomes"].items() if o in NOT_COUNTED},
        "timed_out_runs": len(timed_out),
        "timeout_rate": len(timed_out) / len(graded) if graded else None,
        "near_timeout_runs": near,
        "agent_time": (
            {"mean": statistics.fmean(finished), "median": statistics.median(finished),
             "min": min(finished), "max": max(finished), "runs": len(finished)}
            if finished else None
        ),
        "agent_timeout_sec": limit,
        "oracle": _oracle(oracle),
        "flags": [],
        "note": None,
    }
    if not row["done"]:
        return row  # partial numbers would raise and drop flags as runs finish
    flags = _flags(row, graded, oracle, base_limit_sec=base_limit_sec, multiplier=multiplier, t=t)
    row["flags"] = [
        {"code": code, "label": FLAG_LABELS[code], "reason": reason, "review": reviews.get(code)}
        for code, reason in flags
    ]
    if row["graded_runs"] < t.signal_min_runs:
        row["note"] = f"only {row['graded_runs']} graded model run{'s' if row['graded_runs'] != 1 else ''}: too few to judge"
    elif _partial_credit_signal(row, t):
        lo, hi = row["reward_range"]
        row["note"] = (f"0% pass rate, but rewards ranged {lo:.2f}–{hi:.2f}: partial credit still gives signal, "
                       "so not flagged as no signal")
    return row


def _partial_credit_signal(row: dict[str, Any], t: Thresholds) -> bool:
    """No run got full marks, but rewards differ enough between runs to learn from."""
    return row["pass_rate"] == 0.0 and row["reward_range"][1] - row["reward_range"][0] >= t.partial_spread


def _oracle(run: dict[str, Any] | None) -> dict[str, Any] | None:
    if run is None:
        return None
    outcome = run_outcome(run)
    return {
        "run_id": run["id"],
        "state": ORACLE_STATES.get(outcome, outcome),
        "outcome": outcome,
        "reward": run["reward"],
        "error_kind": run["error_kind"],
    }


def _flags(
    row: dict[str, Any], graded: list[dict[str, Any]], oracle: dict[str, Any] | None, *,
    base_limit_sec: float | None, multiplier: float | None, t: Thresholds,
) -> list[tuple[str, str]]:
    flags: list[tuple[str, str]] = []
    limit = row["agent_timeout_sec"]
    limit_text = f"{duration(limit)} agent time limit" if limit else "agent time limit"
    if multiplier and base_limit_sec:
        limit_text += f" (task.toml's {duration(base_limit_sec)} × {multiplier:g} for this job)"

    # The oracle is known-good: if the grader rejects it, the grader is likely broken.
    state = row["oracle"]["state"] if row["oracle"] else None
    if state in ORACLE_FAILED:
        if state == "failed":
            reason = f"the reference solution scored {oracle['reward'] or 0.0:.2f}, so the grader rejects a known-good answer"
            grade = oracle.get("grade") or {}
            if grade.get("missing"):
                reason += f"; missing {', '.join(grade['missing'])}"
            if grade.get("failed"):
                reason += f"; failed {', '.join(grade['failed'])}"
                if grade.get("judged") and set(grade["failed"]) <= set(grade["judged"]):
                    reason += " (all decided by the LLM judge, which can be wrong: retry the oracle before fixing the task)"
        elif state == "timed_out":
            reason = f"the reference solution hit the {limit_text}"
        else:
            detail = (oracle.get("grade") or {}).get("error") or oracle.get("error_message") or oracle["error_kind"]
            reason = f"the grader failed on the reference solution: {str(detail)[:300]}"
        flags.append(("oracle_failed", reason))

    # RL learns from the difference between attempts; all-pass or all-fail has none,
    # unless partial credit still varies (then task_report notes why it isn't raised).
    n = row["graded_runs"]
    if n >= t.signal_min_runs and row["pass_rate"] in (0.0, 1.0) and not _partial_credit_signal(row, t):
        if row["pass_rate"] == 1.0:
            reason = (f"all {n} graded runs got full marks: too easy to teach anything "
                      f"(with {n} runs the true pass rate could still be as low as {0.05 ** (1 / n):.0%})")
        else:
            lo, hi = row["reward_range"]
            reason = f"none of {n} graded runs got full marks"
            reason += f"; every run scored {lo:.2f}" if hi == lo else f"; rewards only ranged {lo:.2f}–{hi:.2f}"
            if always := _always_failed(graded):
                reason += f"; every run failed {', '.join(always)}"
            if state in ORACLE_FAILED:
                reason += "; likely the grader's fault, since the oracle failed too"
            reason += f" (with {n} runs the true pass rate could still be up to {1 - 0.05 ** (1 / n):.0%})"
        flags.append(("no_signal", reason))

    times = row["agent_time"]
    if times and times["runs"] >= t.duration_min_runs:
        lo, hi = times["min"], times["max"]
        if hi - lo >= t.duration_min_spread_sec and hi >= t.duration_ratio * max(lo, 1.0):
            flags.append((
                "duration_variance",
                f"agent time ranged {duration(lo)}–{duration(hi)} ({hi / max(lo, 1.0):.1f}×) over "
                f"{times['runs']} finished runs, median {duration(times['median'])}",
            ))

    if row["timed_out_runs"] >= t.timeout_min_runs and (row["timeout_rate"] or 0) >= t.timeout_min_rate:
        reason = f"{row['timed_out_runs']} of {n} graded runs hit the {limit_text}"
        if row["near_timeout_runs"]:
            reason += f"; {row['near_timeout_runs']} more finished in its last {1 - t.near_timeout:.0%}"
        flags.append(("timeouts", reason))
    return flags


def _always_failed(graded: list[dict[str, Any]]) -> list[str]:
    """Rubric criteria that failed in every graded run that has a grader report."""
    grades = [r["grade"] for r in graded if r.get("grade")]
    if not grades:
        return []
    common = set(grades[0].get("failed") or [])
    for g in grades[1:]:
        common &= set(g.get("failed") or [])
    return [c for c in grades[0].get("failed") or [] if c in common]
