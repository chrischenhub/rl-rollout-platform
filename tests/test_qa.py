"""Task QA: the per-task report and its flags, from runs as the queue returns them."""

from __future__ import annotations

from uuid import uuid4

from rollout import qa


def run(agent="claude-code", status="completed", reward=1.0, agent_sec=60.0, *, task="env/a", error_kind=None,
        error_message=None, grade=None, effective=True):
    return {
        "id": uuid4(), "task_id": task, "agent": agent, "status": status, "reward": reward, "agent_sec": agent_sec,
        "error_kind": error_kind, "error_message": error_message, "grade": grade, "effective": effective,
    }


def oracle(**kw):
    return run("oracle", **kw)


def model(*rewards, **kw):
    return [run(reward=r, **kw) for r in rewards]


def grade(*failed, judged=()):
    return {"failed": list(failed), "ungraded": [], "missing": [], "error": None, "judged": list(judged)}


def task(runs, limit=600, multiplier=None, reviews=None):
    return qa.task_report("env/a", runs, base_limit_sec=limit, multiplier=multiplier, reviews=reviews or {})


def codes(row):
    return [f["code"] for f in row["flags"]]


def reason(row, code):
    return next(f["reason"] for f in row["flags"] if f["code"] == code)


def test_a_healthy_task_has_no_flags():
    row = task([oracle(), *model(1.0, 0.0, 1.0, 0.5, 0.0)])
    assert codes(row) == [] and row["note"] is None
    assert row["oracle"]["state"] == "passed"
    assert (row["passed_runs"], row["graded_runs"], row["pass_rate"]) == (2, 5, 0.4)
    assert row["reward_range"] == [0.0, 1.0] and row["timeout_rate"] == 0.0
    assert row["agent_time"]["mean"] == 60.0


def test_an_oracle_the_grader_rejects_points_at_the_grader():
    row = task([oracle(reward=0.5, grade=grade("red_material")), *model(1.0, 0.0, 0.5)])
    assert codes(row) == ["oracle_failed"] and row["oracle"]["state"] == "failed"
    assert "scored 0.50" in reason(row, "oracle_failed") and "failed red_material" in reason(row, "oracle_failed")
    assert "LLM judge" not in reason(row, "oracle_failed")

    # Only the judge's criteria failed: the judge may be wrong, so say retry first.
    row = task([oracle(reward=0.5, grade=grade("looks_right", judged=["looks_right"])), *model(1.0, 0.0, 0.5)])
    assert "retry the oracle" in reason(row, "oracle_failed")


def test_an_oracle_that_times_out_or_breaks_the_grader_is_flagged():
    row = task([oracle(status="timed_out", reward=None), *model(1.0, 0.0, 0.5)])
    assert codes(row) == ["oracle_failed"] and "hit the 10m 00s agent time limit" in reason(row, "oracle_failed")

    row = task([oracle(status="error", reward=None, error_kind="verifier", error_message="RewardFileNotFoundError: x"),
                *model(1.0, 0.0, 0.5)])
    assert codes(row) == ["oracle_failed"] and "RewardFileNotFoundError" in reason(row, "oracle_failed")


def test_an_infrastructure_failure_leaves_the_oracle_check_inconclusive():
    row = task([oracle(status="error", reward=None, error_kind="sandbox"), *model(1.0, 0.0, 0.5)])
    assert row["oracle"]["state"] == "inconclusive" and codes(row) == []


def test_all_pass_has_no_signal_and_says_how_sure_that_is():
    row = task([oracle(), *model(1.0, 1.0, 1.0, 1.0, 1.0)])
    assert codes(row) == ["no_signal"]
    assert "all 5 graded runs got full marks" in reason(row, "no_signal")
    assert "could still be as low as 55%" in reason(row, "no_signal")


def test_all_fail_is_no_signal_only_if_partial_credit_barely_varies():
    # No full marks, but rewards differ between runs: that difference is the signal.
    spread = task([oracle(), *model(0.3, 0.9, 0.5)])
    assert codes(spread) == [] and spread["pass_rate"] == 0.0
    assert spread["note"] == ("0% pass rate, but rewards ranged 0.30–0.90: partial credit still gives signal, "
                              "so not flagged as no signal")

    # The note shows next to other flags too: a broken oracle with spread-out model rewards.
    broken_oracle = task([oracle(reward=0.62), *model(0.92, 0.92, 0.23)])
    assert codes(broken_oracle) == ["oracle_failed"] and "0.23–0.92" in broken_oracle["note"]

    narrow = task([oracle(), *model(0.4, 0.5, 0.45)])
    assert codes(narrow) == ["no_signal"] and narrow["note"] is None
    assert "rewards only ranged 0.40–0.50" in reason(narrow, "no_signal")
    assert "could still be up to 63%" in reason(narrow, "no_signal")

    flat = task([oracle(), *model(0.4, 0.4, 0.4, grade=grade("red_material", "camera"))])
    assert "every run scored 0.40" in reason(flat, "no_signal")
    assert "every run failed red_material, camera" in reason(flat, "no_signal")

    broken = task([oracle(reward=0.0), *model(0.0, 0.0, 0.0)])
    assert codes(broken) == ["oracle_failed", "no_signal"]
    assert "likely the grader's fault" in reason(broken, "no_signal")


def test_runs_that_say_nothing_about_the_task_are_left_out():
    row = task([
        oracle(), *model(1.0, 0.0, 1.0),
        run(status="error", reward=None, error_kind="sandbox"), run(status="cancelled", reward=None),
    ])
    assert row["graded_runs"] == 3 and row["not_counted"] == {"infra_error": 1, "cancelled": 1}
    assert codes(row) == []

    too_few = task([oracle(), *model(1.0), run(status="error", reward=None, error_kind="sandbox")])
    assert codes(too_few) == [] and too_few["note"] == "only 1 graded model run: too few to judge"


def test_wildly_varying_agent_time_is_flagged():
    row = task([oracle(), run(reward=1.0, agent_sec=60), run(reward=0.0, agent_sec=70), run(reward=1.0, agent_sec=400)])
    assert codes(row) == ["duration_variance"]
    assert reason(row, "duration_variance") == "agent time ranged 1m 00s–6m 40s (6.7×) over 3 finished runs, median 1m 10s"

    # Three times as long, but only 50 s apart: noise on a quick task.
    quick = task([oracle(), run(reward=1.0, agent_sec=20), run(reward=0.0, agent_sec=30), run(reward=1.0, agent_sec=70)])
    assert codes(quick) == []

    # Timed-out runs stopped at the limit: they're the timeouts flag's business, not this one's.
    capped = task([oracle(), run(reward=1.0, agent_sec=60), run(reward=0.0, agent_sec=70),
                   run(status="timed_out", reward=0.0, agent_sec=600)])
    assert "duration_variance" not in codes(capped) and capped["agent_time"]["runs"] == 2


def test_runs_that_keep_hitting_the_time_limit_are_flagged():
    row = task([
        oracle(), *model(1.0, 0.0, agent_sec=300), run(reward=0.0, agent_sec=560),
        run(status="timed_out", reward=0.0, agent_sec=600), run(status="timed_out", reward=None, agent_sec=600),
    ])
    assert codes(row) == ["timeouts"] and row["timeout_rate"] == 0.4
    assert reason(row, "timeouts") == "2 of 5 graded runs hit the 10m 00s agent time limit; 1 more finished in its last 10%"

    one = task([oracle(), *model(1.0, 0.0, 1.0, 0.0), run(status="timed_out", reward=0.0)])
    assert codes(one) == []  # a single unlucky run isn't a pattern

    scaled = task([oracle(), *model(1.0), *[run(status="timed_out", reward=0.0, agent_sec=60)] * 2], multiplier=0.1)
    assert "hit the 1m 00s agent time limit (task.toml's 10m 00s × 0.1 for this job)" in reason(scaled, "timeouts")


def test_flags_wait_until_the_task_has_finished():
    row = task([oracle(), *model(1.0, 1.0), run(status="running", reward=None, agent_sec=None)])
    assert not row["done"] and row["flags"] == [] and row["finished_runs"] == 3


def test_report_covers_each_task_and_attaches_reviews():
    job = {"options": {}}
    runs = [
        oracle(task="env/a"), *model(1.0, 1.0, 1.0, task="env/a"),
        run(status="error", reward=None, error_kind="sandbox", task="env/a", effective=False),  # replaced by a retry
        oracle(task="env/b"), *model(1.0, 0.0, 0.5, task="env/b"),
    ]
    tasks = [{"task_id": t, "config": {"agent": {"timeout_sec": 300}}} for t in ("env/a", "env/b")]
    review = {"task_id": "env/a", "code": "no_signal", "verdict": "false_positive", "note": "meant as a smoke test"}
    stale = {"task_id": "env/b", "code": "timeouts", "verdict": "valid", "note": ""}  # that flag no longer stands

    report = qa.report(job, runs, tasks, [review, stale])
    a, b = report["tasks"]
    assert (a["task_id"], a["model_runs"], a["agent_timeout_sec"]) == ("env/a", 3, 300)
    assert a["flags"][0]["review"] == review and b["flags"] == []
    assert (report["flagged_tasks"], report["flags"], report["reviewed_flags"]) == (1, 1, 1)
