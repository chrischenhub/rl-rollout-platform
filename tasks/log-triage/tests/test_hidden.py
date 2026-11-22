"""Hidden checks for pyswe/log-triage. Each test maps to a rubric criterion.

The agent's report.json is compared with the expected one, and the agent's script is
re-run on two log sets it never saw, so hard-coded answers don't pass."""

import json
import os
import subprocess
from pathlib import Path

import pytest

APP_DIR = Path(os.environ.get("APP_DIR", "/app"))
TESTS = Path(__file__).resolve().parent
AGENT_PYTHON = os.environ.get("AGENT_PYTHON", "python3")


def expected(name: str) -> dict:
    return json.loads((TESTS / name).read_text())


@pytest.fixture(scope="module")
def report() -> dict:
    return json.loads((APP_DIR / "out" / "report.json").read_text())


def run_script(logs: Path, out: Path) -> dict:
    proc = subprocess.run([AGENT_PYTHON, str(APP_DIR / "triage.py"), "--logs", str(logs), "--out", str(out)],
                          cwd=out.parent, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, f"triage.py exited {proc.returncode}: {proc.stderr[-2000:]}"
    return json.loads((out / "report.json").read_text())


# counts
def test_counts(report):
    want = expected("expected_main.json")
    got = {k: report.get(k) for k in ("total_requests", "malformed_lines", "status_classes")}
    assert got == {k: want[k] for k in got}


# p95_latency
def test_p95_latency(report):
    assert report.get("p95_latency_ms") == expected("expected_main.json")["p95_latency_ms"]


# top_5xx
def test_top_5xx_paths(report):
    assert report.get("top_5xx_paths") == expected("expected_main.json")["top_5xx_paths"]


# incident_window
def test_incident_window(report):
    assert report.get("incident") == expected("expected_main.json")["incident"]


# reproducible
def test_report_comes_from_the_script(report, tmp_path):
    assert run_script(APP_DIR / "logs", tmp_path / "out") == report


# generalizes
def test_holdout_with_incident(tmp_path):
    assert run_script(TESTS / "holdout_incident", tmp_path / "out") == expected("expected_holdout_incident.json")


def test_holdout_without_incident(tmp_path):
    assert run_script(TESTS / "holdout_quiet", tmp_path / "out") == expected("expected_holdout_quiet.json")
