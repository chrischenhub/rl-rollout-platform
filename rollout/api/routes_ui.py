"""HTML pages, server-rendered from the same queue functions as the JSON API.
Job and run pages re-fetch themselves every few seconds while anything is active."""

from __future__ import annotations

import json
import secrets
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import ValidationError

from rollout import qa, queue
from rollout.api import timefmt
from rollout.api.auth import create_token, safe_next, ui_user
from rollout.api.routes_api import FlagReview, save_flag_review
from rollout.job_config import job_config
from rollout.models import TERMINAL, Agent, JobKind

router = APIRouter(include_in_schema=False)

MAX_SHOWN_CHARS = 4000


def render(request: Request, name: str, status_code: int = 200, **ctx: Any):
    ctx.setdefault("user", request.session.get("email"))
    return request.app.state.templates.TemplateResponse(request, name, ctx, status_code=status_code)


@router.get("/login")
async def login(request: Request, next: str = "/"):
    if request.session.get("email"):
        return RedirectResponse(safe_next(next), 303)
    return render(request, "login.html", next=safe_next(next), google=request.app.state.settings.google_enabled)


@router.get("/")
async def home(request: Request, user: str = Depends(ui_user)):
    async with request.app.state.pool.connection() as conn:
        envs = await queue.list_environments(conn)
        jobs = await queue.list_jobs(conn, limit=20)
    return render(request, "home.html", environments=envs, jobs=jobs)


async def _environment_page(request: Request, env_id: str, status_code: int = 200, **extra: Any):
    async with request.app.state.pool.connection() as conn:
        env = await queue.get_environment(conn, env_id)
        if env is None:
            raise HTTPException(404, "environment not found")
        tasks = await queue.list_tasks(conn, env_id)
    return render(
        request,
        "environment.html",
        status_code=status_code,
        env=env,
        tasks=tasks,
        models=request.app.state.settings.models,
        qa_min_runs=qa.QA_MIN_RUNS,
        qa_default_runs=qa.QA_DEFAULT_RUNS,
        form_token=secrets.token_urlsafe(16),
        **extra,
    )


@router.get("/environments/{env_id}")
async def environment(env_id: str, request: Request, user: str = Depends(ui_user)):
    return await _environment_page(request, env_id)


@router.post("/jobs")
async def create_job(request: Request, user: str = Depends(ui_user)):
    form = await request.form()
    env_id = str(form.get("environment_id", ""))
    choice = str(form.get("agent_model", ""))
    if choice == Agent.ORACLE:
        agent, model = Agent.ORACLE, None
    else:
        agent, model = Agent.CLAUDE_CODE, choice.removeprefix(f"{Agent.CLAUDE_CODE}:")
    try:
        if model is not None and model not in request.app.state.settings.models:
            raise queue.JobRequestError(f"unknown model {model}")
        attempts = int(form.get("attempts", 1))
        async with request.app.state.pool.connection() as conn:
            job_id, _ = await queue.create_job(
                conn,
                created_by=user,
                task_ids=[str(t) for t in form.getlist("task_ids")],
                agent=agent,
                model=model,
                attempts=attempts,
                judge=form.get("judge") == "on",
                # One token per rendered form, so a double submit creates one job.
                idempotency_key=f"form:{form.get('form_token')}" if form.get("form_token") else None,
                kind=str(form.get("kind") or JobKind.EVAL),
            )
    except (queue.JobRequestError, ValueError) as e:
        return await _environment_page(request, env_id, status_code=422, error=str(e))
    return RedirectResponse(f"/jobs/{job_id}", 303)


@router.get("/jobs")
async def jobs(request: Request, user: str = Depends(ui_user)):
    async with request.app.state.pool.connection() as conn:
        rows = await queue.list_jobs(conn, limit=200)
    return render(request, "jobs.html", jobs=rows)


@router.get("/jobs/{job_id}")
async def job(job_id: UUID, request: Request, user: str = Depends(ui_user)):
    return await _job_page(request, job_id)


async def _job_page(request: Request, job_id: UUID, status_code: int = 200, **extra: Any):
    async with request.app.state.pool.connection() as conn:
        row = await queue.get_job(conn, job_id)
        if row is None:
            raise HTTPException(404, "job not found")
        runs = await queue.list_runs(conn, job_id)
        tasks = await queue.job_tasks(conn, job_id)
        reviews = await queue.flag_reviews(conn, job_id) if row["kind"] == JobKind.QA else []
    report = qa.report(row, runs, tasks, reviews) if row["kind"] == JobKind.QA else None
    active = row["status"] not in ("finished", "cancelled")
    return render(
        request, "job.html", status_code=status_code, job=row, runs=runs, config=job_config(row, tasks), qa=report,
        live=active, **extra,
    )


@router.post("/jobs/{job_id}/flag-reviews")
async def review_flag(job_id: UUID, request: Request, user: str = Depends(ui_user)):
    form = await request.form()
    try:
        review = FlagReview(**{k: str(form.get(k, "")) for k in ("task_id", "code", "verdict", "note")})
        await save_flag_review(request, job_id, review, user)
    except ValidationError:
        return await _job_page(request, job_id, status_code=422, review_error="Choose valid or false positive.")
    except HTTPException as e:
        if e.status_code == 404:
            raise
        return await _job_page(request, job_id, status_code=e.status_code, review_error=f"Not saved: {e.detail}.")
    return RedirectResponse(f"/jobs/{job_id}#qa", 303)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: UUID, request: Request, user: str = Depends(ui_user)):
    async with request.app.state.pool.connection() as conn:
        if await queue.cancel_job(conn, job_id, by=user) is None:
            raise HTTPException(404, "job not found")
    return RedirectResponse(f"/jobs/{job_id}", 303)


def _clip(text: str | None, limit: int = MAX_SHOWN_CHARS) -> tuple[str, int]:
    """Text to show, and how many characters were cut."""
    text = text or ""
    return (text, 0) if len(text) <= limit else (text[:limit], len(text) - limit)


def _format_call(name: str | None, args: Any) -> str:
    if isinstance(args, dict):
        if "command" in args:
            return f"$ {args['command']}"
        if "file_path" in args and "content" in args:
            return f"{args['file_path']}\n\n{args['content']}"
        if "file_path" in args:
            return str(args["file_path"])
    return json.dumps(args, indent=2, ensure_ascii=False)


def transcript_steps(trajectory: dict, tz: ZoneInfo) -> list[dict]:
    steps = []
    for s in trajectory.get("steps", []):
        message, cut = _clip(s.get("message") if isinstance(s.get("message"), str) else json.dumps(s.get("message")))
        calls = []
        for c in s.get("tool_calls") or []:
            body, call_cut = _clip(_format_call(c.get("function_name"), c.get("arguments")))
            calls.append({"name": c.get("function_name"), "body": body, "cut": call_cut})
        results = []
        for r in (s.get("observation") or {}).get("results") or []:
            content = r.get("content")
            body, res_cut = _clip(content if isinstance(content, str) else json.dumps(content, indent=2))
            results.append({"body": body, "cut": res_cut})
        steps.append(
            {
                "id": s.get("step_id"),
                "source": s.get("source"),
                "time": timefmt.clock(s.get("timestamp"), tz),
                "message": message,
                "message_cut": cut,
                "calls": calls,
                "results": results,
            }
        )
    return steps


@router.post("/runs/{run_id}/retry")
async def retry_run(run_id: UUID, request: Request, user: str = Depends(ui_user)):
    async with request.app.state.pool.connection() as conn:
        try:
            result = await queue.manual_retry(conn, run_id, requested_by=user)
        except queue.RetryRefused as e:
            return await _run_page(request, run_id, status_code=409, retry_error=str(e))
    if result is None:
        raise HTTPException(404, "run not found")
    return RedirectResponse(f"/runs/{result[0]['id']}", 303)


def _read_json(store, key: str) -> Any:
    try:
        return json.loads(store.path(key).read_text())
    except (OSError, ValueError):
        return None


@router.get("/runs/{run_id}")
async def run(run_id: UUID, request: Request, user: str = Depends(ui_user)):
    return await _run_page(request, run_id)


async def _run_page(request: Request, run_id: UUID, status_code: int = 200, **extra: Any):
    async with request.app.state.pool.connection() as conn:
        row = await queue.get_run(conn, run_id)
        if row is None:
            raise HTTPException(404, "run not found")
        events = await queue.run_events(conn, run_id)
    store = request.app.state.store
    ctx: dict[str, Any] = {"run": row, "events": events, "live": row["status"] not in TERMINAL, **extra}
    prefix = row["artifact_prefix"]
    if prefix:
        ctx["files"] = [k.removeprefix(prefix + "/") for k in store.list(prefix)]
        ctx["report"] = _read_json(store, f"{prefix}/trial/verifier/report.json")
        trajectory = _read_json(store, f"{prefix}/trial/agent/trajectory.json")
        if isinstance(trajectory, dict):
            ctx["steps"] = transcript_steps(trajectory, request.app.state.display_tz)
            ctx["final_metrics"] = trajectory.get("final_metrics")
        elif store.exists(f"{prefix}/trial/agent/oracle.txt"):
            text = store.path(f"{prefix}/trial/agent/oracle.txt").read_text(errors="replace")
            ctx["oracle_log"], ctx["oracle_cut"] = text[-20000:], max(0, len(text) - 20000)
    return render(request, "run.html", status_code=status_code, **ctx)


async def _account_page(request: Request, user: str, **extra: Any):
    async with request.app.state.pool.connection() as conn:
        cur = await conn.execute(
            "SELECT id, label, created_at, last_used_at FROM api_tokens"
            " WHERE email = %s AND revoked_at IS NULL ORDER BY created_at DESC",
            (user,),
        )
        tokens = await cur.fetchall()
    return render(request, "account.html", tokens=tokens, base_url=request.app.state.settings.base_url, **extra)


@router.get("/account")
async def account(request: Request, user: str = Depends(ui_user)):
    return await _account_page(request, user)


@router.post("/account/tokens")
async def new_token(request: Request, user: str = Depends(ui_user)):
    form = await request.form()
    async with request.app.state.pool.connection() as conn:
        token = await create_token(conn, user, str(form.get("label", ""))[:100])
    return await _account_page(request, user, new_token=token)


@router.post("/account/tokens/{token_id}/revoke")
async def revoke_token(token_id: UUID, request: Request, user: str = Depends(ui_user)):
    async with request.app.state.pool.connection() as conn:
        await conn.execute(
            "UPDATE api_tokens SET revoked_at = now() WHERE id = %s AND email = %s AND revoked_at IS NULL",
            (token_id, user),
        )
    return RedirectResponse("/account", 303)
