"""A job's configuration as people read it: what it asked for, what each task ran with,
and how to run it again. Shared by the job page and GET /api/jobs/{id}."""

from __future__ import annotations

import shlex
from typing import Any

# Job options (jobs.options) and the `rollout submit` flags that set them.
OPTION_FLAGS = {"agent_timeout_multiplier": "--agent-timeout-multiplier", "cpus": "--cpus", "memory_mb": "--memory-mb"}

# `harbor run` flags that change a task's settings, and the task.toml key each overrides.
HARBOR_OVERRIDES = {
    "--override-cpus": "cpus",
    "--override-memory-mb": "memory_mb",
    "--override-storage-mb": "storage_mb",
    "--agent-timeout-multiplier": "agent_timeout_multiplier",
}


def option_labels(options: dict[str, Any]) -> list[str]:
    """Job options in words, for the job header: they make results non-comparable."""
    labels = []
    if options.get("agent_timeout_multiplier") is not None:
        labels.append(f"agent timeout × {options['agent_timeout_multiplier']:g}")
    if options.get("cpus") is not None:
        labels.append(f"{options['cpus']:g} CPUs")
    if options.get("memory_mb") is not None:
        labels.append(f"{options['memory_mb']} MB memory")
    return labels


def task_settings(config: dict[str, Any]) -> dict[str, Any]:
    """The task.toml values that decide how a run is resourced and timed."""
    env, agent, verifier = (config.get(k) or {} for k in ("environment", "agent", "verifier"))
    return {
        "cpus": env.get("cpus"),
        "memory_mb": env.get("memory_mb"),
        "storage_mb": env.get("storage_mb"),
        "build_timeout_sec": env.get("build_timeout_sec"),
        "agent_timeout_sec": agent.get("timeout_sec"),
        "verifier_timeout_sec": verifier.get("timeout_sec"),
    }


def command_overrides(argv: list[str] | None) -> dict[str, str]:
    """The task settings a recorded Harbor command overrode."""
    argv = argv or []
    return {HARBOR_OVERRIDES[a]: argv[i + 1] for i, a in enumerate(argv[:-1]) if a in HARBOR_OVERRIDES}


def reproduce(job: dict[str, Any], task_ids: list[str]) -> dict[str, Any]:
    """How to start the same job again: the CLI command and the API request body.
    Either runs the tasks' current versions, which may differ from this job's."""
    qa = job.get("kind") == "qa"
    argv = ["rollout", "submit", *task_ids, "--agent", job["agent"]]
    if job["model"]:
        argv += ["--model", job["model"]]
    if qa:
        argv.append("--qa")
    argv += ["--attempts", str(job["attempts"])]
    if not job["judge"]:
        argv.append("--no-judge")
    options = job.get("options") or {}
    for key, flag in OPTION_FLAGS.items():
        if options.get(key) is not None:
            argv += [flag, f"{options[key]:g}"]
    body = {"task_ids": task_ids, "agent": job["agent"], "model": job["model"], "attempts": job["attempts"],
            "judge": job["judge"]}
    if qa:
        body = {"kind": "qa", **body}
    if options:
        body["options"] = options
    return {"cli": shlex.join(argv), "api": body}


def job_config(job: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
    """Everything needed to see how a job ran and to run it again. Overrides come from
    the recorded Harbor command; for runs from before commands were recorded, from the
    job's own options (a task's platform overrides at the time are then unknown)."""
    from_options = {k: f"{v:g}" for k, v in (job.get("options") or {}).items()}
    return {
        "options": job.get("options") or {},
        "option_labels": option_labels(job.get("options") or {}),
        "tasks": [
            {
                "task_id": t["task_id"],
                "digest": t["digest"],
                "version": t["version"],
                "task_toml": task_settings(t["config"]),
                "overrides": command_overrides(t["harbor_command"]) if t["harbor_command"] else from_options,
                "harbor_command": t["harbor_command"],
                "harbor_command_text": shlex.join(t["harbor_command"]) if t["harbor_command"] else None,
                "synced_run_id": t["synced_run_id"],
            }
            for t in tasks
        ],
        "reproduce": reproduce(job, [t["task_id"] for t in tasks]),
    }
