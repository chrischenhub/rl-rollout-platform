"""The example tasks in tasks/, graded offline without Docker or an API key.

For each task: the starting state must score low, and the reference solution must pass
every programmatic criterion (the LLM-judged ones are skipped without a key). This
catches broken graders and broken oracles before a job spends sandboxes on them."""

from __future__ import annotations

import filecmp
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TASKS = Path(__file__).resolve().parent.parent / "tasks"
TASK_DIRS = sorted(p for p in TASKS.iterdir() if (p / "task.toml").is_file())
SHARED_TESTS = ["grade.py", "conftest.py", "pytest.ini", "test.sh"]


def grade(task: Path, app: Path, logs: Path) -> dict:
    env = dict(os.environ, APP_DIR=str(app), TESTS_DIR=str(task / "tests"), LOG_DIR=str(logs),
               AGENT_PYTHON=sys.executable, ANTHROPIC_API_KEY="")
    subprocess.run([sys.executable, str(task / "tests" / "grade.py"), str(task / "tests" / "rubric.json")],
                   env=env, capture_output=True, timeout=600, check=False)
    return json.loads((logs / "report.json").read_text())


@pytest.fixture(params=TASK_DIRS, ids=lambda p: p.name)
def task(request) -> Path:
    return request.param


def test_shared_files_are_synced(task):
    for name in SHARED_TESTS:
        assert filecmp.cmp(TASKS / "_shared" / name, task / "tests" / name, shallow=False), f"run tasks/sync.sh ({name})"
    assert filecmp.cmp(TASKS / "_shared" / "Dockerfile", task / "environment" / "Dockerfile", shallow=False)
    assert filecmp.cmp(task / "instruction.md", task / "tests" / "instruction.md", shallow=False)


def test_starting_state_scores_low(task, tmp_path):
    app = shutil.copytree(task / "environment" / "app", tmp_path / "app")
    report = grade(task, app, tmp_path / "logs")
    assert report["error"] is None
    assert report["reward"] <= 0.3, report


def test_reference_solution_passes(task, tmp_path):
    app = shutil.copytree(task / "environment" / "app", tmp_path / "app")
    # solve.sh calls python3; make that the interpreter running the tests (it has pytest).
    path = f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}"
    subprocess.run([str(task / "solution" / "solve.sh")], env=dict(os.environ, APP_DIR=str(app), PATH=path),
                   check=True, capture_output=True, timeout=600)
    report = grade(task, app, tmp_path / "logs")
    assert report["error"] is None and report["missing_deliverables"] == []
    failed = [r for r in report["results"] if r["kind"] == "programmatic" and not r["passed"]]
    assert failed == []
    assert all(r["passed"] is None and r["note"].startswith("skipped") for r in report["results"] if r["kind"] == "judge")
    assert report["reward"] == 1.0
