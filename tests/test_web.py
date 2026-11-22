"""Pages, sign-in rules and the API's auth, through the ASGI app."""

from __future__ import annotations

import base64
import dataclasses
import json
import re
import uuid

import httpx
import pytest
from itsdangerous import TimestampSigner

from conftest import USER
from rollout import queue
from rollout.api.app import create_app
from rollout.api.auth import create_token, identity_error, safe_next
from rollout.models import ErrorKind, Outcome, RunStatus
from rollout.store import LocalStore

DOMAINS = ("example.com",)


@pytest.mark.parametrize(
    "claims, ok",
    [
        ({"email": "a@example.com", "email_verified": True, "hd": "example.com"}, True),
        ({"email": "A@Example.com", "email_verified": True, "hd": "example.com"}, True),
        # A personal Google account registered with an example.com address has no hd.
        ({"email": "a@example.com", "email_verified": True}, False),
        ({"email": "a@example.com", "email_verified": False, "hd": "example.com"}, False),
        ({"email": "a@gmail.com", "email_verified": True}, False),
        ({"email": "a@evil.com", "email_verified": True, "hd": "evil.com"}, False),
        ({"email": "a@notexample.com", "email_verified": True, "hd": "notexample.com"}, False),
    ],
)
def test_identity_rules(claims, ok):
    assert (identity_error(claims, DOMAINS) is None) == ok


def test_individually_allowed_email():
    emails = ("me@gmail.com",)
    assert identity_error({"email": "Me@gmail.com", "email_verified": True}, DOMAINS, emails) is None
    assert identity_error({"email": "me@gmail.com", "email_verified": False}, DOMAINS, emails) is not None
    assert identity_error({"email": "other@gmail.com", "email_verified": True}, DOMAINS, emails) is not None


@pytest.mark.parametrize(
    "raw, expected",
    [("/jobs/1", "/jobs/1"), ("//evil.com", "/"), ("https://evil.com", "/"), ("/\\evil.com", "/"), (None, "/")],
)
def test_safe_next(raw, expected):
    assert safe_next(raw) == expected


def session_cookie(secret: str, data: dict) -> str:
    """Sign a session the way Starlette's SessionMiddleware does."""
    return TimestampSigner(secret).sign(base64.b64encode(json.dumps(data).encode())).decode()


@pytest.fixture
async def app(pool, settings, tasks):
    app = create_app(settings)
    app.state.pool = pool  # ASGITransport doesn't run the lifespan
    return app


def client(app, email: str | None = None) -> httpx.AsyncClient:
    cookies = {"rollout_session": session_cookie(app.state.settings.session_secret, {"email": email})} if email else {}
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", cookies=cookies)


async def test_pages_redirect_to_login(app):
    async with client(app) as c:
        r = await c.get("/jobs")
    assert r.status_code == 303 and r.headers["location"] == "/login?next=/jobs"


async def test_session_from_disallowed_domain_is_dropped(app):
    async with client(app, "someone@gmail.com") as c:
        r = await c.get("/")
    assert r.status_code == 303 and r.headers["location"].startswith("/login")


async def test_allowlisted_email_gets_in_and_no_domain_hint_is_sent(pool, settings, tasks):
    app = create_app(
        dataclasses.replace(settings, allowed_emails=("me@gmail.com",), google_client_id="id", google_client_secret="s")
    )
    app.state.pool = pool
    async with client(app, "me@gmail.com") as c:
        assert (await c.get("/")).status_code == 200
        start = await c.get("/auth/google")
    assert start.status_code == 302 and "hd=" not in start.headers["location"]


async def test_api_requires_a_valid_token(app, pool):
    async with pool.connection() as conn:
        token = await create_token(conn, USER)
    async with client(app) as c:
        assert (await c.get("/api/environments")).status_code == 401
        assert (await c.get("/api/environments", headers={"Authorization": "Bearer nope"})).status_code == 401
        r = await c.get("/api/environments/testenv/tasks", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200 and {t["id"] for t in r.json()} == {"testenv/echo-a", "testenv/echo-b"}


async def test_create_job_from_the_form_once(app, pool):
    async with client(app, USER) as c:
        page = await c.get("/environments/testenv")
        assert page.status_code == 200 and "testenv/echo-a" in page.text
        form_token = re.search(r'name="form_token" value="([^"]+)"', page.text).group(1)
        form = {
            "environment_id": "testenv",
            "form_token": form_token,
            "task_ids": ["testenv/echo-a", "testenv/echo-b"],
            "agent_model": "claude-code:claude-sonnet-5",
            "attempts": "2",
            "judge": "on",
        }
        first = await c.post("/jobs", data=form)
        second = await c.post("/jobs", data=form)  # double submit
        assert first.status_code == 303 and first.headers["location"] == second.headers["location"]
        job_page = await c.get(first.headers["location"])
        assert job_page.status_code == 200 and job_page.text.count("testenv/echo-a</code></a>") == 2  # two run rows

        job_id = first.headers["location"].rsplit("/", 1)[-1]
        assert (await c.post(f"/jobs/{job_id}/cancel")).status_code == 303
    async with pool.connection() as conn:
        job = await queue.get_job(conn, job_id)
    assert job["status"] == "cancelled" and job["counts"] == {"cancelled": 4}


async def test_form_rejects_bad_input(app):
    async with client(app, USER) as c:
        r = await c.post("/jobs", data={"environment_id": "testenv", "agent_model": "oracle", "attempts": "1"})
    assert r.status_code == 422 and "choose at least one task" in r.text


async def test_run_page_renders_transcript_and_report(app, pool, settings):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=["testenv/echo-a"], agent="claude-code", model="claude-sonnet-5", attempts=1
        )
        run = await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
    store = LocalStore(settings.store_dir)
    prefix = f"runs/{run['id']}"
    trajectory = {
        "steps": [
            {"step_id": 1, "source": "user", "message": "Write hello"},
            {
                "step_id": 2, "source": "agent", "message": "On it",
                "tool_calls": [{"function_name": "Bash", "arguments": {"command": "echo hello > /app/out.txt"}}],
                "observation": {"results": [{"content": "ok"}]},
            },
        ],
        "final_metrics": {"total_steps": 2, "total_prompt_tokens": 10, "total_completion_tokens": 5, "total_cost_usd": 0.01},
    }
    report = {"reward": 1.0, "results": [{"id": "has_hello", "kind": "programmatic", "weight": 1, "passed": True, "note": ""}]}
    store.path(f"{prefix}/trial/agent").mkdir(parents=True)
    store.path(f"{prefix}/trial/verifier").mkdir(parents=True)
    store.path(f"{prefix}/trial/agent/trajectory.json").write_text(json.dumps(trajectory))
    store.path(f"{prefix}/trial/verifier/report.json").write_text(json.dumps(report))
    async with pool.connection() as conn:
        await queue.finish_run(
            conn, run, Outcome(RunStatus.COMPLETED, reward=1.0, artifact_prefix=prefix),
            max_infra_retries=0, retry_backoff_sec=0,
        )

    async with client(app, USER) as c:
        page = await c.get(f"/runs/{run['id']}")
        raw = await c.get(f"/api/runs/{run['id']}/transcript")  # the session also works for the API
    assert page.status_code == 200
    assert "$ echo hello &gt; /app/out.txt" in page.text and "has_hello" in page.text
    assert raw.status_code == 200 and raw.json()["steps"][1]["message"] == "On it"


async def test_cancel_stays_available_while_cancelling(app, pool):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=["testenv/echo-a"], agent="oracle", model=None, attempts=2
        )
        await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
        await queue.cancel_job(conn, job_id)
    async with client(app, USER) as c:
        page = await c.get(f"/jobs/{job_id}")
    # Cancel is idempotent, so offering it again is how a stuck cancel gets unstuck.
    assert ">cancelling<" in page.text and "Cancel job" in page.text


async def test_run_api_hides_the_lease_and_sandboxes_agent_files(app, pool, settings):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=["testenv/echo-a"], agent="oracle", model=None, attempts=1
        )
        run = await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
        prefix = f"runs/{run['id']}"
        store = LocalStore(settings.store_dir)
        store.path(f"{prefix}/trial/agent").mkdir(parents=True)
        store.path(f"{prefix}/trial/agent/page.html").write_text("<script>fetch('/api/jobs')</script>")
        await queue.finish_run(
            conn, run, Outcome(RunStatus.COMPLETED, reward=1.0, artifact_prefix=prefix),
            max_infra_retries=0, retry_backoff_sec=0,
        )
    async with client(app, USER) as c:
        detail = await c.get(f"/api/runs/{run['id']}")
        job = await c.get(f"/api/jobs/{job_id}")
        page = await c.get(f"/api/runs/{run['id']}/files/trial/agent/page.html")
    assert "lease_token" not in detail.json() and "lease_token" not in job.json()["runs"][0]
    assert detail.json()["files"] == ["trial/agent/page.html"]
    assert page.headers["content-security-policy"] == "sandbox"
    assert page.headers["x-content-type-options"] == "nosniff"


async def test_api_docs_need_sign_in(app):
    async with client(app) as c:
        docs = await c.get("/docs")
        schema = await c.get("/openapi.json")
        assert (await c.get("/redoc")).status_code == 404
    assert docs.status_code == 303 and docs.headers["location"] == "/login?next=/docs"
    assert schema.status_code == 401
    async with client(app, USER) as c:
        assert (await c.get("/docs")).status_code == 200
        assert "/api/jobs" in (await c.get("/openapi.json")).json()["paths"]


async def test_job_page_tells_results_from_failures(app, pool):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=["testenv/echo-a"], agent="oracle", model=None, attempts=4
        )
        for outcome in (
            Outcome(RunStatus.COMPLETED, reward=1.0),
            Outcome(RunStatus.COMPLETED, reward=0.0, grade={"failed": ["red_material"], "ungraded": [], "missing": [], "error": None}),
            Outcome(RunStatus.TIMED_OUT, error_message="AgentTimeoutError: Agent execution timed out after 30 seconds"),
            Outcome(
                RunStatus.ERROR, error_kind=ErrorKind.SANDBOX, error_message="DaytonaError: no capacity",
                log_tail="Traceback (most recent call last):\nDaytonaError: no capacity", log_source="trial/exception.txt",
            ),
        ):
            run = await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
            await queue.finish_run(conn, run, outcome, max_infra_retries=0, retry_backoff_sec=0)
    async with client(app, USER) as c:
        page = (await c.get(f"/jobs/{job_id}")).text
        api = (await c.get(f"/api/jobs/{job_id}")).json()

    for label in ("passed", "failed", "timed out", "infra error"):
        assert f">{label}</span>" in page
    assert "1 of 3 graded" in page and re.search(r"Not counted[^<]*\s1 infra error\.", page)
    assert "failed red_material" in page  # why a graded run failed, without opening it
    assert "Last lines of <code>trial/exception.txt</code>" in page
    assert api["outcomes"] == {"passed": 1, "failed": 1, "timed_out": 1, "infra_error": 1}
    assert api["pass_rate"] == pytest.approx(1 / 3) and api["error_kinds"] == {"sandbox": 1}
    assert sorted(r["outcome"] for r in api["runs"]) == ["failed", "infra_error", "passed", "timed_out"]


async def test_retry_endpoint_and_buttons(app, pool):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=["testenv/echo-a"], agent="oracle", model=None, attempts=1
        )
        run = await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
        await queue.finish_run(conn, run, Outcome(RunStatus.COMPLETED, reward=0.0), max_infra_retries=0, retry_backoff_sec=0)
        token = await create_token(conn, USER)
    auth = {"Authorization": f"Bearer {token}"}
    async with client(app, USER) as c:
        run_page = (await c.get(f"/runs/{run['id']}")).text
        job_page = (await c.get(f"/jobs/{job_id}")).text
        assert f'action="/runs/{run["id"]}/retry"' in run_page and f'action="/runs/{run["id"]}/retry"' in job_page

        first = await c.post(f"/api/runs/{run['id']}/retry", headers=auth)
        again = await c.post(f"/api/runs/{run['id']}/retry", headers=auth)
        assert first.status_code == 201 and again.status_code == 200
        new = first.json()
        assert again.json()["id"] == new["id"]
        assert (new["retry_of"], new["retry_kind"], new["requested_by"]) == (str(run["id"]), "manual", USER)
        assert (await c.post(f"/api/runs/{uuid.uuid4()}/retry", headers=auth)).status_code == 404

        # The new try is the attempt's latest now: no retry button for the old one.
        old_page = (await c.get(f"/runs/{run['id']}")).text
        assert f'action="/runs/{run["id"]}/retry"' not in old_page
        new_page = (await c.get(f"/runs/{new['id']}")).text
        assert f"requested by {USER}" in new_page
        assert "retry 1 · manual" in (await c.get(f"/jobs/{job_id}")).text

    async with pool.connection() as conn:  # a run that's still going can't be retried yet
        await queue.create_job(conn, created_by=USER, task_ids=["testenv/echo-b"], agent="oracle", model=None, attempts=1)
        running = await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
    async with client(app, USER) as c:
        busy = await c.post(f"/api/runs/{running['id']}/retry", headers=auth)
        form = await c.post(f"/runs/{running['id']}/retry")
    assert busy.status_code == 409 and "already has a try running" in busy.json()["detail"]
    assert form.status_code == 409 and "Not retried: this task attempt already has a try running" in form.text


async def test_retry_form_redirects_to_the_new_run(app, pool):
    async with pool.connection() as conn:
        await queue.create_job(conn, created_by=USER, task_ids=["testenv/echo-a"], agent="oracle", model=None, attempts=1)
        run = await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
        await queue.finish_run(
            conn, run, Outcome(RunStatus.ERROR, error_kind=ErrorKind.VERIFIER), max_infra_retries=0, retry_backoff_sec=0
        )
    async with client(app, USER) as c:
        r = await c.post(f"/runs/{run['id']}/retry")
    assert r.status_code == 303 and r.headers["location"].startswith("/runs/")
    assert r.headers["location"] != f"/runs/{run['id']}"


async def test_job_page_shows_how_the_job_ran_and_how_to_rerun_it(app, pool):
    async with pool.connection() as conn:
        token = await create_token(conn, USER)
    async with client(app, USER) as c:
        created = await c.post(
            "/api/jobs", headers={"Authorization": f"Bearer {token}"},
            json={"task_ids": ["testenv/echo-a"], "attempts": 1, "options": {"agent_timeout_multiplier": 0.5}},
        )
        assert created.status_code == 201 and created.json()["options"] == {"agent_timeout_multiplier": 0.5}
        job_id = created.json()["id"]
        async with pool.connection() as conn:  # a worker starts it
            run = await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
            await queue.record_command(conn, run, ["harbor", "run", "-p", "/cache/echo-a", "--agent-timeout-multiplier", "0.5"])
        page = (await c.get(f"/jobs/{job_id}")).text
        api = (await c.get(f"/api/jobs/{job_id}")).json()
        run_page = (await c.get(f"/runs/{run['id']}")).text

    assert '<span class="badge flag">agent timeout × 0.5</span>' in page  # visible without scrolling
    assert "× 0.5\n      = <b>1 s</b>" in page  # echo-a's 2 s agent timeout, halved
    assert all("harbor run -p /cache/echo-a --agent-timeout-multiplier 0.5" in p for p in (page, run_page))
    assert "rollout submit testenv/echo-a --agent claude-code --model claude-sonnet-5 --attempts 1 --agent-timeout-multiplier 0.5" in page
    [task] = api["config"]["tasks"]
    assert task["overrides"] == {"agent_timeout_multiplier": "0.5"} and task["task_toml"]["agent_timeout_sec"] == 2
    assert api["config"]["reproduce"]["api"]["options"] == {"agent_timeout_multiplier": 0.5}


def test_ui_times_are_pacific_and_say_so():
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo

    from rollout.api.timefmt import clock, when

    pacific = ZoneInfo("America/Los_Angeles")
    assert when(datetime(2026, 10, 29, 23, 43, 38, tzinfo=timezone.utc), pacific) == "2026-10-29 16:43:38 PDT"
    assert when(datetime(2026, 12, 1, 20, 0, tzinfo=timezone.utc), pacific) == "2026-12-01 12:00:00 PST"
    assert when(None, pacific) == "—"
    assert clock("2026-10-29T23:44:00.123Z", pacific) == "16:44:00"  # transcript step times
    assert clock("not a time", pacific) == "" and clock(None, pacific) == ""


async def test_cancel_endpoint_is_idempotent_and_says_who_cancelled(app, pool):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=["testenv/echo-a"], agent="oracle", model=None, attempts=2
        )
        token = await create_token(conn, USER)
    auth = {"Authorization": f"Bearer {token}"}
    async with client(app, USER) as c:
        first = await c.post(f"/api/jobs/{job_id}/cancel", headers=auth)
        second = await c.post(f"/api/jobs/{job_id}/cancel", headers=auth)
        page = (await c.get(f"/jobs/{job_id}")).text
        missing = await c.post(f"/api/jobs/{uuid.uuid4()}/cancel", headers=auth)
    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "cancelled" and second.json()["counts"] == {"cancelled": 2}
    assert second.json()["cancelled_by"] == USER and f"by {USER}." in page
    assert missing.status_code == 404


async def test_task_qa_from_the_form(app, pool):
    async with client(app, USER) as c:
        page = await c.get("/environments/testenv")
        assert '<option value="qa">Task QA: check the tasks</option>' in page.text
        form_token = re.search(r'name="form_token" value="([^"]+)"', page.text).group(1)
        form = {
            "environment_id": "testenv", "form_token": form_token, "kind": "qa",
            "task_ids": ["testenv/echo-a", "testenv/echo-b"], "agent_model": "claude-code:claude-sonnet-5",
            "attempts": "3", "judge": "on",
        }
        created = await c.post("/jobs", data=form)
        assert created.status_code == 303
        job_page = (await c.get(created.headers["location"])).text
        api = (await c.get("/api" + created.headers["location"])).json()
        jobs_page = (await c.get("/jobs")).text
        oracle_qa = await c.post("/jobs", data={**form, "form_token": "other", "agent_model": "oracle"})

    assert "Task QA · claude-sonnet-5 × 3 and the oracle once per task" in job_page
    assert '<h2 id="qa">Task report</h2>' in job_page and job_page.count('<span class="muted">oracle</span>') == 2
    assert api["kind"] == "qa" and len(api["runs"]) == 8
    assert [(t["task_id"], t["done"], t["flags"]) for t in api["qa"]["tasks"]] == [
        ("testenv/echo-a", False, []), ("testenv/echo-b", False, []),
    ]
    assert '<span class="badge kind">task QA</span>' in jobs_page
    assert oracle_qa.status_code == 422 and "choose a Claude model" in oracle_qa.text


async def test_task_report_flags_and_reviews(app, pool):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=["testenv/echo-a"], agent="claude-code", model="claude-sonnet-5",
            attempts=3, kind="qa",
        )
        judged_wrong = {"failed": ["looks_right"], "ungraded": [], "missing": [], "error": None, "judged": ["looks_right"]}
        for outcome in (
            Outcome(RunStatus.COMPLETED, reward=0.5, grade=judged_wrong),  # the oracle is claimed first
            *[Outcome(RunStatus.COMPLETED, reward=1.0, agent_sec=30.0)] * 3,
        ):
            run = await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
            await queue.finish_run(conn, run, outcome, max_infra_retries=0, retry_backoff_sec=0)
        eval_job, _ = await queue.create_job(
            conn, created_by=USER, task_ids=["testenv/echo-a"], agent="oracle", model=None, attempts=1
        )

    review = {"task_id": "testenv/echo-a", "code": "oracle_failed", "verdict": "false_positive",
              "note": "judge misread the summary"}
    async with client(app, USER) as c:
        page = (await c.get(f"/jobs/{job_id}")).text
        saved = await c.post(f"/api/jobs/{job_id}/flag-reviews", json=review)
        by_form = await c.post(f"/jobs/{job_id}/flag-reviews",
                               data={"task_id": "testenv/echo-a", "code": "no_signal", "verdict": "valid"})
        api = (await c.get(f"/api/jobs/{job_id}")).json()
        reviewed_page = (await c.get(f"/jobs/{job_id}")).text
        not_raised = await c.post(f"/api/jobs/{job_id}/flag-reviews", json={**review, "code": "timeouts"})
        bad_verdict = await c.post(f"/jobs/{job_id}/flag-reviews",
                                   data={"task_id": "testenv/echo-a", "code": "no_signal", "verdict": "maybe"})
        not_qa = await c.post(f"/api/jobs/{eval_job}/flag-reviews", json=review)

    assert "<b>1 of 1 task\n  flagged</b>, 0 of 2 flags reviewed." in page
    assert "<b>oracle failed:</b> the reference solution scored 0.50" in page and "retry the oracle" in page
    assert "<b>no signal:</b> all 3 graded runs got full marks" in page
    assert '<span class="badge s-failed">failed</span>' in page  # the oracle's verdict on the grader
    # Enter in a note submits with the form's first button: that must not be a verdict.
    form = page.split(f'action="/jobs/{job_id}/flag-reviews">')[1].split("</form>")[0]
    assert "disabled" in form[form.index("<button"):].split(">")[0]

    assert saved.status_code == 200 and saved.json()["reviewed_by"] == USER
    assert by_form.status_code == 303 and by_form.headers["location"] == f"/jobs/{job_id}#qa"
    [task] = api["qa"]["tasks"]
    assert {f["code"]: (f["review"]["verdict"], f["review"]["note"]) for f in task["flags"]} == {
        "oracle_failed": ("false_positive", "judge misread the summary"), "no_signal": ("valid", ""),
    }
    assert api["pass_rate"] == 1.0 and api["qa"]["reviewed_flags"] == 2
    assert "Marked <b>false positive</b> by" in reviewed_page and "“judge misread the summary”" in reviewed_page
    assert not_raised.status_code == 409 and "has no timeouts flag" in not_raised.json()["detail"]
    assert bad_verdict.status_code == 422 and "Choose valid or false positive." in bad_verdict.text
    assert not_qa.status_code == 409


async def test_a_partial_credit_task_says_why_it_isnt_flagged_no_signal(app, pool):
    async with pool.connection() as conn:
        job_id, _ = await queue.create_job(
            conn, created_by=USER, task_ids=["testenv/echo-a"], agent="claude-code", model="claude-sonnet-5",
            attempts=3, kind="qa",
        )
        for reward in (0.62, 0.92, 0.92, 0.23):  # the oracle is claimed first
            run = await queue.claim_run(conn, worker_id="t", lease_ttl_sec=60, max_running=10)
            outcome = Outcome(RunStatus.COMPLETED, reward=reward, agent_sec=30.0)
            await queue.finish_run(conn, run, outcome, max_infra_retries=0, retry_backoff_sec=0)
    async with client(app, USER) as c:
        page = (await c.get(f"/jobs/{job_id}")).text
    assert '<span class="badge flag">oracle failed</span>' in page
    assert '<span class="badge flag">no signal</span>' not in page
    assert "0% pass rate, but rewards ranged 0.23–0.92: partial credit still gives signal" in page


async def test_task_qa_through_the_api_and_how_to_rerun_it(app, pool):
    async with client(app, USER) as c:
        created = await c.post("/api/jobs", json={"task_ids": ["testenv/echo-a"], "kind": "qa", "attempts": 4})
        too_few = await c.post("/api/jobs", json={"task_ids": ["testenv/echo-a"], "kind": "qa", "attempts": 2})
        page = (await c.get(f"/jobs/{created.json()['id']}")).text
    job = created.json()
    assert created.status_code == 201 and (job["kind"], len(job["runs"])) == ("qa", 5)
    assert too_few.status_code == 422 and "at least 3 model runs" in too_few.json()["detail"]
    assert job["config"]["reproduce"]["api"]["kind"] == "qa"
    assert "rollout submit testenv/echo-a --agent claude-code --model claude-sonnet-5 --qa --attempts 4" in page
