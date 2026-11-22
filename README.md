# Rollout: hosted RL rollouts and agent evals

A platform that runs AI agents on reinforcement-learning tasks at scale and grades what
they did. It takes tasks in [Harbor](https://harborframework.com) format, runs each
attempt as a rollout in a fresh [Daytona](https://www.daytona.io) sandbox, records the
transcript and reward, and shows the results in a web UI and a JSON API. Task QA jobs
turn the same machinery on the tasks themselves, flagging graders that reject a known
good answer and tasks that give no learning signal.

The repo also has an example environment ([`tasks/`](tasks/)) with three tasks whose
graders give partial credit, use an LLM judge, and resist reward hacking.

- **Nothing lost, nothing run twice.** Postgres is the queue. Workers hold leases with
  fencing tokens, and a retry can't start until its predecessor's sandbox is confirmed
  deleted. Killing a worker with `kill -9` mid-job loses and duplicates no run; a test
  does exactly that.
- **Failures sorted by who has to act.** Infrastructure errors (sandbox, model API,
  Harbor, judge API, lost worker) are retried automatically with exponential backoff.
  Grader errors and timeouts aren't. A reward of 0 is a result, never a failure. Every
  run that didn't pass says why, with the log lines that show it.
- **Retries, cancel and manual retry** keep every failed try as its own row, linked to
  the one it replaced. Cancel is idempotent and destroys running sandboxes.
- **Task QA**: K model runs plus an oracle run per task, flagging broken graders,
  zero-signal tasks, timeouts and erratic run times, each flag with a reason a person
  can confirm or dismiss.
- **Reproducible jobs**: each job pins task versions by content digest, and each run
  records the exact `harbor run` command it executed.

```
browser / curl ──HTTPS──► Caddy ──► api      (FastAPI: JSON API, pages, Google sign-in; writes rows only)
                                       │
                                    Postgres  (system of record, and the queue)
                                       │
                                    worker×N  (claim run → `harbor run -n 1` subprocess → sync → record)
                                       │
                                    Daytona sandboxes, named rollout-<instance>-<run id>
```

## Repository layout

```
rollout/                Python package
├── api/                FastAPI app: JSON API, server-rendered pages, Google sign-in, API tokens
├── queue.py            jobs, runs, claims, leases, reaping, retries, cancel
├── worker.py           claims runs, supervises Harbor, syncs artifacts, records outcomes
├── runner.py           builds the Harbor command; classifies results into outcomes
├── qa.py               Task QA report and flags
├── sandboxes.py        sandbox lookup, deletion and the janitor
├── tasks_registry.py   content-addressed task bundles
├── harbor_ext/         Harbor's Daytona environment, creating sandboxes under a name we choose
└── migrations/         SQL schema
tests/                  pytest suite; fake_harbor.py stands in for Harbor and Daytona
tasks/                  example environment: three tasks, a shared grader, an LLM judge
ERROR_HANDLING.md       every error kind, whether it's retried, and why
compose.yaml            Postgres, api, worker (compose.prod.yaml adds Caddy for HTTPS)
deploy.sh               rsync to a server, rebuild, restart
```

## Quick start

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d postgres
uv sync
cp .env.example .env              # platform settings
cat > secrets.env <<'EOF'         # credentials, never committed
ANTHROPIC_API_KEY=...
DAYTONA_API_KEY=...
DAYTONA_API_URL=https://app.daytona.io/api
EOF

uv run rollout import-tasks tasks
uv run rollout create-token you@example.com     # prints a bearer token for the API

uv run rollout api        # http://localhost:8000
uv run rollout worker     # in another terminal; run several to see claims spread

uv run pytest             # queue invariants, recovery, web, example tasks
```

`HARBOR_BIN=tests/fake_harbor.py` runs the whole platform without Daytona or model
spend. Sign-in to the UI needs a Google OAuth client (`GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET`) and an allowed account (`ALLOWED_EMAIL_DOMAINS` or
`ALLOWED_EMAILS`); the API works with a token alone.

## Design in five rules

1. **Postgres is the queue.** Creating a job inserts the job and all its runs in one
   transaction. Workers claim with `FOR UPDATE SKIP LOCKED` under an advisory lock that
   makes the global concurrency cap (`MAX_RUNNING`) exact. There's no second queue to
   drift from the database.
2. **One run row executes at most once.** A retry is a new row with `retry_of` pointing
   at the row it replaces, so failure records survive. A unique key on
   `(job, task, agent, attempt, retry_number)` makes duplicate retries impossible.
3. **Leases with fencing.** A claim bumps `lease_token`; the worker heartbeats every
   `HEARTBEAT_SEC`, and every write it makes is conditioned on that token. Any live
   worker reaps a run whose lease expired: it's marked `error/worker_lost` and retried
   (or `cancelled`, if its job was cancelled). A worker that comes back late finds its
   writes rejected. A worker also fences itself: if it can't renew a lease, it SIGKILLs
   that run's Harbor one heartbeat interval *before* the lease could expire, so by the
   time a run can be reaped, nothing is left that could still create a sandbox for it.
4. **One Harbor process per run, supervised.** On cancel, shutdown or the platform
   deadline (the sum of the task's own timeouts plus a margin), the worker stops Harbor
   with SIGTERM (Harbor cancels the trial and keeps a partial transcript), then SIGKILL
   if it doesn't exit. A hung Daytona call inside Harbor holds one slot until the
   deadline, never the queue. The worker's own Daytona calls have request timeouts and
   run on a separate small thread pool, so even calls that hang can't stall task setup
   or artifact sync.
5. **Sandboxes are found by a name the platform chooses, and a retry waits for its
   predecessor's to be gone.** Harbor runs with `-e rollout_daytona:RolloutDaytonaEnvironment`
   (`rollout/harbor_ext/`), Harbor's Daytona environment with one change: the sandbox is
   created under the name `rollout-<instance>-<run id>`. Harbor never reports sandbox
   ids, and Daytona's list endpoint can't be trusted (on 2026-10-29 it stopped returning
   live sandboxes at all, by label or unfiltered), but `get(name)` always works, and
   names are unique, so a second create under one name fails instead of leaking. A run
   that started keeps `sandbox_cleared_at` empty until a worker has deleted its sandbox
   *and* a fresh lookup by name finds nothing (a `destroying` or merely stopped sandbox
   doesn't count as gone). Until then:
   - no other try of the same task attempt (automatic, reaper or manual retry) can be
     claimed, so an earlier try's agent can never be running next to its retry;
   - the run counts against `MAX_RUNNING`, which therefore caps sandboxes that may
     exist on Daytona, not just rows in `running`.

   The worker checks right after recording an outcome (Harbor has usually deleted the
   sandbox already) and after each reap; every worker re-checks all unconfirmed runs
   each `REAP_INTERVAL_SEC`, so a Daytona outage delays retries instead of leaking
   sandboxes. A janitor also deletes any labelled sandbox whose run is inactive (best
   effort, since it relies on Daytona's list), and local work dirs that killed workers
   left behind.

## Run states and outcomes

`queued → running → completed | error | timed_out | cancelled`

- `completed`: graded. A reward of 0 is a result and is never retried.
- `timed_out`: the agent ran out of time (Harbor's `AgentTimeoutError`). Harbor still
  runs the grader, so the run keeps whatever reward it earned.
- `error`, with an `error_kind`, is one of two things:
  - **infrastructure**, retried up to `MAX_INFRA_RETRIES` times with exponential backoff:
    `setup` (unpacking the task bundle), `sandbox`, `model_api` (rate limits and
    overload; bad auth or an unknown model isn't retried), `harbor_crash`,
    `harbor_error`, `harbor_hang` (Harbor outlived the platform deadline, so it hung),
    `judge_api` (the LLM judge call failed), `sync_failed`, `worker_lost`,
    `worker_shutdown`;
  - **grader**, not retried, since the task's grading needs fixing: `verifier` (the
    grader crashed, timed out or wrote no reward) and `judge_config` (the job asked for
    the judge but the grader had no API key).

A grader shouldn't fail outright when its judge call does; the example grader drops the
undecided criteria from the reward and scores the rest. So the worker reads
`verifier/report.json` after every run, and if the job had the judge on and any judged
criterion says `judge error`, the run becomes `error/judge_api` with no reward and is
retried. A judge outage never shows up as an ordinary (often higher) score.

The UI and API show an **outcome** derived from that (`rollout/outcomes.py`): `passed`
(reward 1), `partial`, `failed` (reward 0), `timed out`, `infra error`, `grader error`,
`cancelled`. A job's pass rate and mean reward are over **graded** runs: completed runs
plus timeouts (scored at their reward, or 0 if the grader didn't run). A pass is the
`passed` outcome, so a timeout is never one, whatever it scored, and the pass count
always matches the outcome counts beside it. Infra errors, grader errors and cancelled
runs say nothing about the model, so they're left out and counted separately. The job
page shows the denominator ("4 of 6 graded"), errors by kind, and how many automatic
retries ran.

Every run that didn't pass says why in the job table. For errors, that's the error and,
expandable, the last lines of the log that explains it: `trial/exception.txt` (Harbor's
traceback) for sandbox, model and Harbor errors and for timeouts,
`trial/verifier/test-stdout.txt` for grader and judge errors, and `harbor.log`
otherwise. For graded failures, it's the criteria that failed or the deliverables that
were missing. [`ERROR_HANDLING.md`](ERROR_HANDLING.md) has the full mapping.

A job is `queued`, `running`, `finished` (every run terminal; the counts say how they
ended), `cancelling` or `cancelled`.

## Retries

A task attempt is one chain of tries: each retry is a new row whose `retry_of` points at
the try it replaces, so every failure record survives, and the job page shows each try
as "attempt 2 · retry 1". Only the latest try counts in the job summary.

- **Automatic**, after an infrastructure error: up to `MAX_INFRA_RETRIES` in a row, with
  backoff `RETRY_BACKOFF_SEC × 2^n`. Task failures, grader errors and timeouts aren't
  retried.
- **Manual**, from the Retry button on any finished run or `POST /api/runs/{id}/retry`.
  It queues a new try after the attempt's latest one (whichever try you named), records
  who asked, and starts a fresh automatic-retry budget. Asking again while that retry is
  pending returns it (200) instead of queueing a second. It's refused (409) while the
  attempt is still being tried, or while its job is still cancelling. In a cancelled job
  that has stopped, it reopens the job.
- Either way, a retry starts only once the previous try's sandbox is confirmed deleted.

Every job page also shows how the job ran and how to run it again: the job's settings,
each task's pinned version with its `task.toml` resources and timeouts next to anything
the platform overrode, the exact `harbor run` command line of the task's latest try
(credentials go through Harbor's environment, never its arguments), a link to Harbor's
own resolved `trial/config.json`, and the `rollout submit` command and API request that
rerun the job. Job options such as a timeout multiplier are flagged in the job header,
since such results aren't comparable with ordinary ones.

To see each kind of failure on a running deployment:

```bash
# timeout: scale ledger-bugfix's 900 s agent timeout down to 27 s
rollout submit pyswe/ledger-bugfix --agent-timeout-multiplier 0.03
# infrastructure: Daytona refuses a 64-CPU sandbox; the run is retried with backoff, then gives up
rollout submit pyswe/ledger-bugfix --agent oracle --no-judge --cpus 64
```

## Cancelling a job

`POST /api/jobs/{id}/cancel`, or the Cancel button on a job page, records who asked and:

- cancels queued runs at once; they never start. A run a worker had just claimed is
  checked once more right before Harbor launches, so it can't create a sandbox either;
- stops running runs within one heartbeat: the worker sends Harbor SIGTERM, Harbor
  cancels the trial, writes what it has (the partial transcript included) and deletes
  its sandbox; the worker syncs those artifacts and records the run as `cancelled`, not
  failed. If Harbor doesn't exit within `KILL_GRACE_SEC` it is killed, and either way the
  sandbox is deleted by name and confirmed gone;
- never retries anything in the job. A run that fails in a way that would have been
  retried (a Daytona error, say) before its worker hears of the cancel also ends
  `cancelled`, with the error kept in its message.

It's idempotent: calling it again changes nothing, and calling it on a finished job
leaves that job as it is. It's safe while a worker restarts: a run its worker was
shutting down ends `cancelled`, and a run whose worker died is cancelled by the reaper,
which destroys its sandbox.

## Task QA

A Task QA job judges the tasks instead of the model. Choose "Task QA" as the job type
(or `"kind": "qa"` in the API, `rollout submit --qa`): each selected task runs K times
(3–10, default 5) with the model and once with the oracle, its reference solution. The
oracle runs are claimed first, so the grader's verdicts come early, but model runs don't
wait for them: any slot the oracles leave free goes to a model run. All runs of a task
pin the same task version.

The job page's task report shows, per task: whether the oracle passed, the pass rate and
mean reward over graded model runs, agent time (Harbor's `agent_execution` phase, since
the wall clock mostly measures sandbox setup), and the timeout rate. Counting follows
the job summary, and only the latest try of each run counts, so retrying a run updates
the report.

Once all of a task's runs have finished, `rollout/qa.py` flags it, each flag with a
reason:

| Flag | Raised when | The reason says |
|---|---|---|
| `oracle_failed` | the oracle scored below 1, timed out, or the grader errored on it | which criteria and deliverables failed; if only LLM-judged criteria failed, that the judge may be wrong and to retry the oracle first |
| `no_signal` | at least 2 graded runs, and a pass rate of 0% or 100%, except a 0% whose rewards span ≥ 0.2: partial credit still differs between runs, which is what RL learns from | the reward range, criteria every run failed, and how far the true pass rate could be from what K runs showed (95% bound) |
| `duration_variance` | at least 3 runs finished on their own, and the slowest took ≥ 3× and ≥ 2 min longer than the fastest | the range and median agent time |
| `timeouts` | at least 2 graded runs, and ≥ 30%, hit the agent time limit | the limit (with any job multiplier) and how many more finished within its last 10% |

An oracle that failed for infrastructure reasons makes the check *inconclusive* rather
than a flag. Thresholds are in `qa.Thresholds`. Anyone signed in can mark a flag valid
or a false positive, with a note (`POST /api/jobs/{id}/flag-reviews`); flags are
computed from the runs on every read, and the latest review shows.

## What happens when a worker is killed mid-job

Queued runs are untouched. The killed worker's runs stop heartbeating; within
`LEASE_TTL_SEC` (plus one reap interval) another worker, or the same one after restart,
reaps them and queues linked retries, then deletes their sandboxes. Each retry starts
only once Daytona has no sandbox under the name of the run it replaces. No run is lost:
every row reaches a terminal state. None is duplicated: each row executes at most once,
the old worker can no longer record a result (fencing token), the retry is a separate
row linked to the original (unique retry key), and the retry can't start until the
orphaned sandbox, where the old agent would otherwise keep running, is confirmed
deleted. The old Harbor process dies with the worker's container (the worker is its PID
1); a worker that is merely cut off from Postgres kills it before the lease runs out.
`tests/test_worker.py::test_killed_worker_neither_loses_nor_duplicates_runs` does
exactly this with `kill -9`. Rehearsed on a deployed instance with 4 Claude runs in
flight: all 4 were reaped and their sandboxes destroyed 49 s after the kill, and every
retry completed.

A graceful stop (`docker compose stop worker`, deploys) is faster: the worker stops
claiming, SIGTERMs its Harbor processes, syncs what they wrote, and hands the runs back
as `worker_shutdown` retries immediately.

## API

All endpoints need `Authorization: Bearer <token>` (or a signed-in session).
Interactive docs are at `/docs` once signed in. Agent-written files are served with a
sandbox CSP, so an HTML artifact can't run script as the viewer.

| Method | Path | |
|---|---|---|
| GET | `/api/environments` | |
| GET | `/api/environments/{env}/tasks` | tasks with their current digest |
| GET | `/api/tasks/{task_id}` | e.g. `/api/tasks/pyswe/log-triage` |
| POST | `/api/jobs` | `{"task_ids": [...], "agent": "claude-code"\|"oracle", "model": "...", "attempts": 1-10, "judge": true}`, optional `"kind": "qa"`, optional `"options": {"agent_timeout_multiplier", "cpus", "memory_mb"}`; optional `Idempotency-Key` header |
| GET | `/api/jobs`, `/api/jobs/{id}` | status, `config` (task versions, settings, Harbor commands, how to rerun), counts per status and outcome, pass rate and mean reward over `graded_runs`, `error_kinds`, `retries`; runs with their `outcome`; for a Task QA job, `qa`: the per-task report and flags with their reviews |
| POST | `/api/jobs/{id}/cancel` | idempotent |
| POST | `/api/jobs/{id}/flag-reviews` | `{"task_id", "code", "verdict": "valid"\|"false_positive", "note"}` |
| POST | `/api/runs/{id}/retry` | manual retry: 201 new try, 200 if one is already pending, 409 if refused |
| GET | `/api/runs/{id}` | run, event log, artifact list |
| GET | `/api/runs/{id}/transcript` | `trajectory.json` (model) or `oracle.txt` |
| GET | `/api/runs/{id}/files/{path}` | any synced artifact, e.g. `trial/verifier/report.json` |

```bash
curl -X POST localhost:8000/api/jobs -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task_ids": ["pyswe/ledger-bugfix", "pyswe/log-triage"], "model": "claude-sonnet-5-5", "attempts": 4}'
```

## Task hosting

`rollout import-tasks` registers each Harbor task directory under its `task.toml` name
(`pyswe/log-triage`; the part before the slash is the environment), stores an immutable
bundle keyed by a sha256 of its contents, and points the task at that digest. Runs pin
the digest at job creation, so re-importing an edited task never changes what an
existing job runs. Workers unpack bundles into a local cache keyed by digest. Resource
overrides (`rollout set-overrides <task> --cpus 8 --memory-mb 16384`) live on the
platform, outside the task content.

## Deployment

One small VM runs everything; the heavy work happens in Daytona.

```bash
DEPLOY_HOST=root@203.0.113.10 SITE_ADDRESS=203-0-113-10.sslip.io ./deploy.sh --tasks

# on the server
C="docker compose -f compose.yaml -f compose.prod.yaml --profile app"
$C logs -f worker
$C exec worker rollout submit all --agent oracle --no-judge
$C exec worker rollout sandboxes
```

`deploy.sh` rsyncs the code to `/opt/rollout/platform` (settings in `.env`, credentials
in `secrets.env`, mode 600), rebuilds, and restarts; worker restarts are safe mid-job.
`--tasks` also syncs `tasks/` and re-imports it; edited tasks get new digests. Caddy gets
a Let's Encrypt certificate for `SITE_ADDRESS`; without a domain, sslip.io resolves
`<ip-with-dashes>.sslip.io` to the server and Google accepts it as an OAuth redirect
host.

Sign-in is Google OpenID Connect. The callback accepts only verified accounts whose
Workspace (`hd` claim) is in `ALLOWED_EMAIL_DOMAINS`, so a personal Google account
using a company address is rejected. `ALLOWED_EMAILS` lets named individual accounts in
(still Google-verified) without opening a whole domain like gmail.com. Signed-in users
create API tokens at `/account`. The UI shows times in `DISPLAY_TIMEZONE`; the API
returns UTC timestamps with an explicit offset.
