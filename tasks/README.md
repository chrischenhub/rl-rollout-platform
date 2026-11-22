# Example environment: `pyswe`

Three [Harbor](https://harborframework.com) tasks in one environment, a Python 3.12
workspace with a small starting repo in `/app`. They exist to exercise the platform end
to end: partial-credit rewards, an LLM judge, oracle solutions for Task QA, and graders
that are hard to game.

| Task | The agent must | Reward from | Starting state |
|---|---|---|---|
| `pyswe/ledger-bugfix` | find and fix six spec violations in a CSV ledger library, then explain them in `CHANGES.md` | 7 hidden-test criteria + 1 judged | 0.1 |
| `pyswe/rate-limiter` | implement a thread-safe token bucket and a per-key LRU limiter from a written spec | 9 hidden-test criteria, including concurrency | 0.0 |
| `pyswe/log-triage` | write a log-analysis script, find the incident in an afternoon of API logs, and write it up | 6 hidden-test criteria (re-running the script on unseen logs) + 2 judged | 0.0 |

Every reference solution scores 1.0. A rate limiter that is correct except for locking
scores 0.77: the concurrency tests caught it in each of three runs.

## Layout

```
tasks/
├── _shared/              copied into every task by sync.sh
│   ├── Dockerfile        the environment image
│   ├── grade.py          the grader: hidden pytest checks + LLM judge -> reward
│   ├── test.sh           Harbor's verifier entrypoint
│   └── conftest.py, pytest.ini
├── _tools/               authoring scripts (never shipped to a sandbox)
├── sync.sh
└── <task>/
    ├── task.toml         name (pyswe/<task>), resources, timeouts, verifier env
    ├── instruction.md    the prompt
    ├── environment/      Dockerfile + app/ (the starting repo, copied to /app)
    ├── solution/         solve.sh: the oracle
    └── tests/            rubric.json, test_hidden.py, reference material for the judge
```

Harbor uploads `tests/` only after the agent has finished, so the agent never sees the
hidden tests, the expected outputs, or the judge's reference notes.

## Grading

`tests/rubric.json` lists weighted criteria:

```json
{
  "pytest": ["test_hidden.py"],
  "deliverables": ["triage.py", "out/report.json"],
  "criteria": [
    {"id": "counts", "kind": "programmatic", "weight": 2, "tests": ["test_counts"]},
    {"id": "summary_finds_cause", "kind": "judge", "weight": 1,
     "files": ["out/summary.md"], "context": ["incident_notes.md"], "question": "Does the summary ..."}
  ]
}
```

- A **programmatic** criterion passes when every hidden test it names passes (all
  parameter cases). `grade.py` runs pytest once over the hidden tests.
- A **judge** criterion sends the instruction, the agent's `files`, the grader-only
  `context` files and the `question` to Claude (`JUDGE_MODEL`, default
  `claude-sonnet-5-5`), with a JSON-schema output format for a `{passed, reason}`
  verdict. A missing file fails the criterion without a call.
- **Reward** = passed weight / decided weight, or 0 if a deliverable is missing. A judge
  criterion that couldn't be decided (no `ANTHROPIC_API_KEY`, or the call failed after
  the SDK's retries) is left out of the reward, recorded with `passed: null`, and noted
  `skipped: ...` or `judge error: ...`.

`grade.py` writes `/logs/verifier/report.json`, the contract the platform reads:

```json
{"reward": 0.8571, "error": null, "missing_deliverables": [],
 "results": [{"id": "counts", "kind": "programmatic", "weight": 2, "passed": true, "note": ""}, ...]}
```

From it the platform shows which criteria failed on each run, turns a `judge error` on a
job that asked for the judge into a retryable `judge_api` error instead of a score
(the reward would otherwise be computed on a different basis), and flags an oracle whose
only failures are judged criteria as possibly a judge mistake. Any grader that writes
this file works with the platform.

## Making reward hacking harder

- The grader runs from its own venv (`/opt/grader`), so packages the agent installs,
  removes or patches in the system Python don't change how it's graded.
- pytest reads its config and `conftest.py` only from the tests directory (`-c`,
  `--rootdir`, `--confcutdir`) and loads no auto-discovered plugins, so files the agent
  plants in `/app` or `/` are ignored.
- `log-triage` re-runs the agent's script on two log sets it never saw (one with a
  different incident, one with none) and checks that the submitted report came from the
  script, so a hard-coded answer scores little.
- Edge cases sit right on the spec's thresholds (a minute with exactly 25% errors, one
  with 19 requests), so near-misses fail.

The agent still runs as root in the sandbox. A determined agent could edit
`/opt/grader` itself. Running the verifier in a separate container would close that
gap; Harbor's verifier shares the agent's sandbox.

## Working on tasks

```bash
tasks/sync.sh                                   # after editing _shared/ or an instruction.md
python3 tasks/_tools/make_log_triage_data.py    # regenerate log-triage's logs and expected reports
uv run pytest tests/test_example_tasks.py       # offline: start scores low, oracle scores 1.0

# In Docker with Harbor (the judge runs if ANTHROPIC_API_KEY is set):
harbor run -p tasks/log-triage -a oracle -e docker
harbor run -p tasks/log-triage -a claude-code -m anthropic/claude-sonnet-5-5 -e docker
```

The data generator computes each expected report twice, from what it wrote and by
parsing the files with the reference solution, and refuses to write them unless the two
agree.
