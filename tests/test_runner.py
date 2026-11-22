"""Classifying Harbor's results: infrastructure failure, grader failure, timeout or result;
and the job summary built on top of that."""

from __future__ import annotations

import dataclasses
import uuid
from pathlib import Path

import pytest

from rollout import runner
from rollout.job_config import command_overrides, job_config, reproduce
from rollout.models import ErrorKind as K
from rollout.models import Outcome
from rollout.models import RunStatus as S
from rollout.outcomes import run_outcome, summarize
from rollout.runner import StopReason


def result(reward=None, exc=None):
    return {
        "trial_name": "t",
        "verifier_result": {"rewards": {"reward": reward}} if reward is not None else None,
        "exception_info": {"exception_type": exc, "exception_message": "boom"} if exc else None,
    }


def report(judged_passed, note=""):
    """Shaped like the tasks' judge.py output."""
    return {"reward": 0.5, "missing_deliverables": [], "error": None, "results": [
        {"id": "prog", "kind": "programmatic", "weight": 1, "passed": True, "note": ""},
        {"id": "look", "kind": "judge", "weight": 1, "passed": judged_passed, "note": note},
    ]}


def classify(res, stop=None, **kw) -> Outcome:
    return runner.classify(res, exit_code=0, stop_reason=stop, deadline=100, **kw)


JUDGE_DOWN = report(None, "judge error: HTTP Error 529: Overloaded")
NO_JUDGE_KEY = report(None, "skipped: ANTHROPIC_API_KEY not set")


@pytest.mark.parametrize(
    "res, stop, kw, expected",
    [
        (result(1.0), None, {}, (S.COMPLETED, None, False)),
        (result(0.0), None, {}, (S.COMPLETED, None, False)),  # a zero is a result
        (result(0.3, "AgentTimeoutError"), None, {}, (S.TIMED_OUT, None, False)),
        # A grader that runs out of time is the grader's problem, not a slow agent.
        (result(None, "VerifierTimeoutError"), None, {}, (S.ERROR, K.VERIFIER, False)),
        # The deadline is the task's own timeouts plus a margin: reaching it means Harbor hung.
        (result(None), StopReason.DEADLINE, {}, (S.ERROR, K.HARBOR_HANG, True)),
        (result(None, "DaytonaError"), None, {}, (S.ERROR, K.SANDBOX, True)),
        (None, None, {}, (S.ERROR, K.HARBOR_CRASH, True)),
        (result(1.0), None, dict(report=JUDGE_DOWN, judge=True), (S.ERROR, K.JUDGE_API, True)),
        (result(0.2, "AgentTimeoutError"), None, dict(report=JUDGE_DOWN, judge=True), (S.ERROR, K.JUDGE_API, True)),
        (result(1.0), None, dict(report=NO_JUDGE_KEY, judge=True), (S.ERROR, K.JUDGE_CONFIG, False)),
        # Judge off: skipping the judged criteria is what the job asked for.
        (result(1.0), None, dict(report=NO_JUDGE_KEY, judge=False), (S.COMPLETED, None, False)),
        (result(0.5), None, dict(report=report(False, "the cube is blue"), judge=True), (S.COMPLETED, None, False)),
    ],
)
def test_classification(res, stop, kw, expected):
    o = classify(res, stop, **kw)
    assert (o.status, o.error_kind, o.retryable) == expected


def test_a_judge_outage_is_not_a_result():
    o = classify(result(1.0), report=JUDGE_DOWN, judge=True)
    assert o.reward is None and "529" in o.error_message
    assert run_outcome({"status": o.status, "error_kind": o.error_kind, "reward": o.reward}) == "infra_error"


def test_grade_summary_names_what_failed():
    assert runner.grade_summary(report(False, "the cube is blue")) == {
        "failed": ["look"], "ungraded": [], "missing": [], "error": None, "judged": ["look"],
    }
    assert runner.grade_summary(None) is None


def test_agent_time_is_harbors_agent_phase():
    res = {**result(1.0), "agent_execution": {
        "started_at": "2026-10-29T17:55:40.000630Z", "finished_at": "2026-10-29T17:58:59.031256Z",
    }}
    assert classify(res).agent_sec == pytest.approx(199.03, abs=0.01)
    assert classify(result(1.0)).agent_sec is None  # Harbor didn't get to the agent
    assert runner.agent_seconds({"agent_execution": {"started_at": "x", "finished_at": None}}) is None


def test_the_log_is_picked_by_failure_type(tmp_path):
    trial = tmp_path / "trial"
    (trial / "verifier").mkdir(parents=True)
    (trial / "exception.txt").write_text("Traceback ...\nDaytonaError: no capacity\n")
    (trial / "verifier" / "test-stdout.txt").write_text("grade.py exited non-zero\n")
    harbor_log = tmp_path / "harbor.log"
    harbor_log.write_text("progress bar\n")

    def source(status, kind=None, trial_dir=trial):
        return runner.pick_log(Outcome(status, error_kind=kind), trial_dir, harbor_log)[0]

    assert source(S.ERROR, K.SANDBOX) == "trial/exception.txt"
    assert source(S.ERROR, K.MODEL_API) == "trial/exception.txt"
    assert source(S.TIMED_OUT) == "trial/exception.txt"
    assert source(S.ERROR, K.VERIFIER) == "trial/verifier/test-stdout.txt"
    assert source(S.ERROR, K.JUDGE_API) == "trial/verifier/test-stdout.txt"
    assert source(S.ERROR, K.HARBOR_CRASH) == "harbor.log"
    assert source(S.ERROR, K.SANDBOX, trial_dir=None) == "harbor.log"  # no trial dir: fall back


def run(status, reward=None, kind=None):
    return {"status": status, "reward": reward, "error_kind": kind}


@pytest.mark.parametrize(
    "r, outcome",
    [
        (run("completed", 1.0), "passed"),
        (run("completed", 0.61), "partial"),
        (run("completed", 0.0), "failed"),
        (run("timed_out", 0.4), "timed_out"),
        (run("error", None, "sandbox"), "infra_error"),
        (run("error", None, "judge_api"), "infra_error"),
        (run("error", None, "verifier"), "grader_error"),
        (run("error", None, "judge_config"), "grader_error"),
        (run("cancelled"), "cancelled"),
    ],
)
def test_outcomes(r, outcome):
    assert run_outcome(r) == outcome


def test_pass_rate_counts_only_what_measures_the_model():
    s = summarize(
        [
            run("completed", 1.0), run("completed", 0.5), run("completed", 0.0),
            run("timed_out", None),  # the agent ran out of time and the grader didn't run: a 0
            run("error", 0.8, "model_api"),  # a reward from a broken run isn't a result
            run("error", None, "verifier"),
            run("cancelled"),
            run("running"),
        ],
        retries={"auto": 3},
    )
    assert (s["graded_runs"], s["passed_runs"], s["pass_rate"]) == (4, 1, 0.25)
    assert s["mean_reward"] == pytest.approx(1.5 / 4)
    assert s["outcomes"] == {
        "passed": 1, "partial": 1, "failed": 1, "timed_out": 1,
        "infra_error": 1, "grader_error": 1, "cancelled": 1, "running": 1,
    }
    assert s["error_kinds"] == {"model_api": 1, "verifier": 1}
    assert s["retries"] == {"auto": 3, "manual": 0}
    assert summarize([run("error", None, "sandbox")])["pass_rate"] is None  # nothing graded yet


def test_a_timeout_is_never_a_pass_whatever_it_scored():
    # It counts towards the pass rate's denominator and the mean reward, but the job
    # page shows it as "timed out", so counting it as a pass would disagree with that.
    s = summarize([run("completed", 1.0), run("timed_out", 1.0)])
    assert s["outcomes"] == {"passed": 1, "timed_out": 1}
    assert (s["graded_runs"], s["passed_runs"], s["pass_rate"], s["mean_reward"]) == (2, 1, 0.5, 1.0)


def test_the_harbor_command_never_carries_credentials(settings):
    secrets = dict(anthropic_api_key="sk-ant-SECRET", daytona_api_key="dtn_SECRET")
    s = dataclasses.replace(settings, **secrets)
    run = {"id": uuid.uuid4(), "job_id": uuid.uuid4(), "agent": "claude-code", "model": "claude-sonnet-5"}
    for judge in (True, False):
        argv = runner.harbor_command(
            s, run, Path("/t"), Path("/w"), overrides={"cpus": 8}, judge=judge, agent_timeout_multiplier=0.5
        )
        assert not any(v in arg for v in secrets.values() for arg in argv)
    assert runner.harbor_env(s)["ANTHROPIC_API_KEY"] == "sk-ant-SECRET"  # they travel this way instead


def test_overrides_are_read_back_from_the_command():
    argv = ["harbor", "run", "-p", "/t", "--override-cpus", "8.0", "--agent-timeout-multiplier", "0.03"]
    assert command_overrides(argv) == {"cpus": "8.0", "agent_timeout_multiplier": "0.03"}
    assert command_overrides(None) == {}


def test_reproduce_names_every_setting():
    job = {"agent": "oracle", "model": None, "attempts": 2, "judge": False, "options": {"cpus": 64.0}}
    again = reproduce(job, ["pyswe/ledger-bugfix"])
    assert again["cli"] == "rollout submit pyswe/ledger-bugfix --agent oracle --attempts 2 --no-judge --cpus 64"
    assert again["api"] == {
        "task_ids": ["pyswe/ledger-bugfix"], "agent": "oracle", "model": None, "attempts": 2, "judge": False,
        "options": {"cpus": 64.0},
    }


def test_unrecorded_commands_fall_back_to_the_job_options():
    job = {"agent": "claude-code", "model": "m", "attempts": 1, "judge": True, "options": {"agent_timeout_multiplier": 0.03}}
    task = {"task_id": "pyswe/ledger-bugfix", "digest": "d", "version": 1, "config": {"agent": {"timeout_sec": 900}},
            "harbor_command": None, "synced_run_id": None}
    assert job_config(job, [task])["tasks"][0]["overrides"] == {"agent_timeout_multiplier": "0.03"}
