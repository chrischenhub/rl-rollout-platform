"""Task grader: hidden pytest checks plus LLM-judged criteria, combined into one reward.

    python grade.py /tests/rubric.json

Reads the rubric (format in tasks/README.md), grades the agent's work in APP_DIR, and
writes LOG_DIR/report.json and LOG_DIR/reward.txt. The reward is the passed weight over
the weight that was decided; a missing deliverable makes it 0. Judge criteria that
couldn't be decided (no API key, or the judge call failed) are left out of the reward
and recorded with passed: null and a note saying why, so the platform can tell a
judge outage apart from a real score.

The grader runs from its own venv (/opt/grader in the task image), so what the agent
installs in the system Python can't change how it is graded.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

APP_DIR = Path(os.environ.get("APP_DIR", "/app")).resolve()
TESTS_DIR = Path(os.environ.get("TESTS_DIR", Path(__file__).parent)).resolve()
LOG_DIR = Path(os.environ.get("LOG_DIR", "/logs/verifier")).resolve()
PYTEST_TIMEOUT_SEC = int(os.environ.get("PYTEST_TIMEOUT_SEC", "300"))
JUDGE_MODEL = os.environ.get("JUDGE_MODEL") or "claude-sonnet-5-5"
MAX_FILE_CHARS = 60_000

JUDGE_SYSTEM = (
    "You grade one criterion of a rubric for a reinforcement-learning task. An AI agent "
    "worked on the task; you see the task instruction, the files the criterion names, and "
    "reference material the agent never saw. Decide strictly whether the files meet the "
    "criterion as written. Judge only what the criterion asks; ignore style unless it "
    "asks about style. If a file is empty or does not address the criterion, it fails."
)
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "reason": {"type": "string", "description": "One or two sentences citing the evidence."},
    },
    "required": ["passed", "reason"],
    "additionalProperties": False,
}


def run_pytest(files: list[str]) -> dict[str, bool] | None:
    """Run the hidden tests once. Returns {test name: passed}, or None if pytest
    produced no report at all (a grader failure, not a task failure)."""
    junit = LOG_DIR / "junit.xml"
    junit.unlink(missing_ok=True)
    env = dict(
        os.environ,
        APP_DIR=str(APP_DIR),
        PYTHONDONTWRITEBYTECODE="1",
        # Only plugins we name: nothing the agent pip-installed hooks into the run.
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    cmd = [
        sys.executable, "-m", "pytest", *files,
        "-q", "-p", "no:cacheprovider",
        # Config and conftest.py come only from the tests dir: an ini file or conftest
        # the agent planted in /app or / is never read.
        "-c", str(TESTS_DIR / "pytest.ini"), "--rootdir", str(TESTS_DIR), "--confcutdir", str(TESTS_DIR),
        "--junitxml", str(junit), "-o", "junit_family=xunit2",
    ]
    with (LOG_DIR / "pytest.log").open("w") as log:
        try:
            subprocess.run(cmd, cwd=TESTS_DIR, env=env, stdout=log, stderr=subprocess.STDOUT,
                           timeout=PYTEST_TIMEOUT_SEC, check=False)
        except subprocess.TimeoutExpired:
            log.write(f"\npytest timed out after {PYTEST_TIMEOUT_SEC}s\n")
    if not junit.is_file():
        return None
    outcomes: dict[str, bool] = {}
    for case in ET.parse(junit).iter("testcase"):
        bad = any(child.tag in ("failure", "error", "skipped") for child in case)
        outcomes[case.get("name", "")] = not bad
    return outcomes


def programmatic(criterion: dict, outcomes: dict[str, bool]) -> tuple[bool, str]:
    """A criterion passes when every test it names ran and passed (all parameter cases)."""
    failed, missing = [], []
    for test in criterion["tests"]:
        cases = {k: v for k, v in outcomes.items() if k == test or k.startswith(test + "[")}
        if not cases:
            missing.append(test)
        failed += [k for k, ok in cases.items() if not ok]
    if missing:
        return False, f"not run (the module under test probably failed to import): {', '.join(missing)}"
    if failed:
        return False, f"failed: {', '.join(failed)}"
    return True, ""


def read_text(path: Path) -> str:
    text = path.read_text(errors="replace")
    if len(text) > MAX_FILE_CHARS:
        # Too long to judge in full; say so rather than silently cutting it.
        raise ValueError(f"{path.name} is {len(text)} characters, over the {MAX_FILE_CHARS} the judge reads")
    return text


def judge(criterion: dict, instruction: str) -> tuple[bool | None, str]:
    missing = [f for f in criterion.get("files", []) if not (APP_DIR / f).is_file()]
    if missing:
        return False, f"missing {', '.join(missing)}"
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None, "skipped: ANTHROPIC_API_KEY is not set, so the LLM judge did not run"
    try:
        import anthropic

        parts = [f"<instruction>\n{instruction}\n</instruction>"]
        for f in criterion.get("files", []):
            parts.append(f'<agent_file path="{f}">\n{read_text(APP_DIR / f)}\n</agent_file>')
        for f in criterion.get("context", []):
            parts.append(f'<reference path="{f}">\n{read_text(TESTS_DIR / f)}\n</reference>')
        parts.append(f"<criterion>\n{criterion['question']}\n</criterion>")

        # The SDK retries connection errors, 429, 529 and other 5xx with backoff.
        client = anthropic.Anthropic(timeout=120.0, max_retries=4)
        response = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=8000,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": "\n\n".join(parts)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        )
        if response.stop_reason != "end_turn":
            return None, f"judge error: stopped with {response.stop_reason} ({response._request_id})"
        text = next(b.text for b in response.content if b.type == "text")
        verdict = json.loads(text)
        return bool(verdict["passed"]), str(verdict["reason"])
    except Exception as e:  # any failure leaves the criterion undecided, never failed
        return None, f"judge error: {type(e).__name__}: {e}"[:1000]


def grade(rubric_path: Path) -> dict:
    rubric = json.loads(rubric_path.read_text())
    instruction_file = TESTS_DIR / "instruction.md"
    instruction = instruction_file.read_text() if instruction_file.is_file() else ""
    missing = [d for d in rubric.get("deliverables", []) if not (APP_DIR / d).exists()]

    criteria = rubric["criteria"]
    outcomes: dict[str, bool] = {}
    if any(c["kind"] == "programmatic" for c in criteria):
        ran = run_pytest(rubric.get("pytest", ["."]))
        if ran is None:
            return {"reward": None, "error": "pytest wrote no report; see pytest.log",
                    "missing_deliverables": missing, "results": []}
        outcomes = ran

    results = []
    for c in criteria:
        if c["kind"] == "programmatic":
            passed, note = programmatic(c, outcomes)
        elif c["kind"] == "judge":
            passed, note = judge(c, instruction)
        else:
            raise ValueError(f"unknown criterion kind {c['kind']!r} for {c['id']}")
        results.append({"id": c["id"], "kind": c["kind"], "weight": c["weight"], "passed": passed,
                        "note": note, "description": c.get("description", "")})

    decided = [r for r in results if r["passed"] is not None]
    total = sum(r["weight"] for r in decided)
    reward = 0.0 if missing or not total else sum(r["weight"] for r in decided if r["passed"]) / total
    return {"reward": round(reward, 4), "error": None, "missing_deliverables": missing, "results": results}


def main() -> int:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    report = grade(Path(sys.argv[1]))
    (LOG_DIR / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if report["reward"] is None:
        return 1  # no reward.txt: Harbor records a verifier error
    (LOG_DIR / "reward.txt").write_text(f"{report['reward']:.4f}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
