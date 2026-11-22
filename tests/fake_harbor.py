#!/usr/bin/env python3
"""Stand-in for `harbor run`, for exercising the worker without Daytona or a model.

Point HARBOR_BIN at this file. Behaviour comes from the environment:
  FAKE_HARBOR_MODE          pass | fail | partial | crash | sandbox_error | agent_timeout | verifier_timeout
                            | judge_error | judge_skipped | hang | stubborn
  FAKE_HARBOR_MODE_<SLUG>   per-task override, e.g. FAKE_HARBOR_MODE_ECHO_A=fail
  FAKE_HARBOR_MODE_<SLUG>_<AGENT>  per task and agent, e.g. FAKE_HARBOR_MODE_ECHO_B_ORACLE=partial
  FAKE_HARBOR_SLEEP         seconds of simulated agent work (default 1)
  FAKE_HARBOR_LEDGER        file that gets one line per invocation (the run id)

It writes the same trial layout Harbor does, handles SIGTERM like Harbor (records
CancelledError and keeps the partial transcript), and exits if its parent worker
dies, as Harbor does when the worker's container is killed.
"""

import argparse
import json
import os
import signal
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("command")
    p.add_argument("-p", dest="path", required=True)
    p.add_argument("-a", dest="agent")
    p.add_argument("-m", dest="model")
    p.add_argument("-e", dest="env")
    p.add_argument("-n", dest="n")
    p.add_argument("--job-name", required=True)
    p.add_argument("-o", dest="jobs_dir", required=True)
    p.add_argument("-y", action="store_true")
    p.add_argument("--ek", action="append", default=[])
    p.add_argument("--ve", action="append", default=[])
    p.add_argument("--override-cpus")
    p.add_argument("--override-memory-mb")
    p.add_argument("--override-storage-mb")
    p.add_argument("--agent-timeout-multiplier")
    args = p.parse_args()

    slug = Path(args.path).name
    key = f"FAKE_HARBOR_MODE_{slug.upper().replace('-', '_')}"
    agent_key = f"{key}_{(args.agent or '').upper().replace('-', '_')}"
    mode = os.environ.get(agent_key, os.environ.get(key, os.environ.get("FAKE_HARBOR_MODE", "pass")))
    work = float(os.environ.get("FAKE_HARBOR_SLEEP", "1"))
    if ledger := os.environ.get("FAKE_HARBOR_LEDGER"):
        with open(ledger, "a") as f:
            f.write(args.job_name + "\n")

    trial = Path(args.jobs_dir) / args.job_name / f"{slug}__fake"
    (trial / "agent").mkdir(parents=True)
    (trial / "verifier").mkdir()
    (trial / "invocation.json").write_text(json.dumps(sys.argv[1:]))
    started = now()
    agent_started = agent_finished = None
    steps = [{"step_id": 1, "source": "user", "message": (Path(args.path) / "instruction.md").read_text()}]

    def write_transcript() -> None:
        name = "oracle.txt" if args.agent == "oracle" else "trajectory.json"
        body = "oracle ran\n" if args.agent == "oracle" else json.dumps({"schema_version": "fake", "steps": steps})
        (trial / "agent" / name).write_text(body)

    def write_report(judged_passed: bool | None, note: str = "") -> float:
        """What the tasks' grade.py writes: criteria the judge couldn't decide drop out."""
        results = [
            {"id": "has_output", "kind": "programmatic", "weight": 1, "passed": True, "note": ""},
            {"id": "looks_right", "kind": "judge", "weight": 1, "passed": judged_passed, "note": note},
        ]
        decided = [r for r in results if r["passed"] is not None]
        reward = sum(r["passed"] for r in decided) / len(decided)
        report = {"reward": reward, "error": None, "missing_deliverables": [], "results": results}
        (trial / "verifier" / "report.json").write_text(json.dumps(report))
        (trial / "verifier" / "test-stdout.txt").write_text(json.dumps(report, indent=2) + "\n")
        return reward

    def write_result(reward: float | None, exc: tuple[str, str] | None = None) -> None:
        if reward is not None:
            (trial / "verifier" / "reward.txt").write_text(str(reward))
        if exc:
            (trial / "exception.txt").write_text(f"Traceback (most recent call last):\n  ...\n{exc[0]}: {exc[1]}\n")
        result = {
            "trial_name": trial.name,
            "task_name": slug,
            "started_at": started,
            "finished_at": now(),
            "agent_execution": {"started_at": agent_started, "finished_at": agent_finished} if agent_finished else None,
            "agent_result": {"cost_usd": 0.01},
            "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
            "exception_info": (
                {"exception_type": exc[0], "exception_message": exc[1], "exception_traceback": "", "occurred_at": now()}
                if exc else None
            ),
        }
        (trial / "result.json").write_text(json.dumps(result))

    def on_sigterm(signum, frame):
        if mode == "stubborn":
            return
        steps.append({"step_id": 2, "source": "agent", "message": "partial work before cancel"})
        write_transcript()
        write_result(None, ("CancelledError", ""))
        sys.exit(130)

    signal.signal(signal.SIGTERM, on_sigterm)

    if mode == "crash":
        print("fake harbor: simulated crash", flush=True)
        sys.exit(2)

    parent = os.getppid()
    until = float("inf") if mode in ("hang", "stubborn") else time.time() + work
    agent_started = now()
    print(f"fake harbor: {slug} mode={mode}", flush=True)
    while time.time() < until:
        # Reparented (ppid 1 or a new parent): the worker died, possibly before we
        # recorded its pid.
        if os.getppid() != parent or parent == 1:
            sys.exit(1)
        time.sleep(0.1)

    steps.append({"step_id": 2, "source": "agent", "message": "done"})
    agent_finished = now()
    write_transcript()
    if mode == "pass":
        write_result(write_report(True))
    elif mode == "fail":
        write_result(0.0)
    elif mode == "partial":
        write_result(write_report(False, "the cube is blue"))
    elif mode == "agent_timeout":
        write_result(0.0, ("AgentTimeoutError", "Agent execution timed out after 2 seconds"))
    elif mode == "verifier_timeout":
        write_result(None, ("VerifierTimeoutError", "Verifier execution timed out after 2 seconds"))
    elif mode == "judge_error":
        write_result(write_report(None, "judge error: HTTP Error 529: Overloaded"))
    elif mode == "judge_skipped":
        write_result(write_report(None, "skipped: ANTHROPIC_API_KEY not set"))
    elif mode == "sandbox_error":
        write_result(None, ("DaytonaError", "Failed to create sandbox: simulated"))
    else:
        sys.exit(f"unknown FAKE_HARBOR_MODE {mode}")


if __name__ == "__main__":
    main()
