"""JSON API: the programmatic entry point. The UI sits on the same queue functions."""

from __future__ import annotations

import mimetypes
from typing import Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from rollout import qa, queue
from rollout.api.auth import require_user
from rollout.job_config import job_config
from rollout.models import Agent, JobKind

router = APIRouter(prefix="/api", dependencies=[Depends(require_user)])

TRANSCRIPT_FILES = ("trial/agent/trajectory.json", "trial/agent/oracle.txt", "trial/agent/claude-code.txt")
INTERNAL_RUN_FIELDS = {"lease_token"}
DETAIL_RUN_FIELDS = {"log_tail", "rewards", "harbor_command"}  # only on the single-run view
# Artifacts are written by the agent. An HTML or SVG file served as-is from our origin
# could run script as the signed-in viewer; a sandbox CSP gives it an opaque origin.
ARTIFACT_HEADERS = {"Content-Security-Policy": "sandbox", "X-Content-Type-Options": "nosniff"}


class JobOptions(BaseModel):
    """Overrides for every task in the job, mainly to demonstrate failure handling.
    Results from a job that sets any are not comparable with ordinary ones."""

    agent_timeout_multiplier: float | None = Field(default=None, gt=0, le=10)
    cpus: float | None = Field(default=None, gt=0)
    memory_mb: int | None = Field(default=None, gt=0)


class JobCreate(BaseModel):
    task_ids: list[str] = Field(min_length=1)
    # "qa" (Task QA) runs each task `attempts` times with the model and once with the
    # oracle, and reports on the tasks; see GET /api/jobs/{id} "qa".
    kind: JobKind = JobKind.EVAL
    agent: Agent = Agent.CLAUDE_CODE
    model: str | None = None
    attempts: int = Field(default=1, ge=1, le=10)
    judge: bool = True
    options: JobOptions = Field(default_factory=JobOptions)


class FlagReview(BaseModel):
    task_id: str
    code: str
    verdict: Literal["valid", "false_positive"]
    note: str = Field(default="", max_length=2000)


def run_view(run: dict[str, Any], *, full: bool = False) -> dict[str, Any]:
    hidden = INTERNAL_RUN_FIELDS if full else INTERNAL_RUN_FIELDS | DETAIL_RUN_FIELDS
    out = {k: v for k, v in run.items() if k not in hidden}
    out["url"] = f"/api/runs/{run['id']}"
    out["transcript_url"] = f"/api/runs/{run['id']}/transcript"
    return out


def resolve_model(settings, agent: Agent, model: str | None) -> str | None:
    if agent == Agent.ORACLE:
        return None
    model = model or settings.models[0]
    if model not in settings.models:
        raise HTTPException(422, f"model must be one of: {', '.join(settings.models)}")
    return model


@router.get("/environments")
async def environments(request: Request):
    async with request.app.state.pool.connection() as conn:
        return await queue.list_environments(conn)


@router.get("/environments/{env_id}/tasks")
async def environment_tasks(env_id: str, request: Request):
    async with request.app.state.pool.connection() as conn:
        if await queue.get_environment(conn, env_id) is None:
            raise HTTPException(404, "environment not found")
        return await queue.list_tasks(conn, env_id)


@router.get("/tasks/{task_id:path}")
async def task(task_id: str, request: Request):
    async with request.app.state.pool.connection() as conn:
        row = await queue.get_task(conn, task_id)
    if row is None:
        raise HTTPException(404, "task not found")
    return row


@router.post("/jobs", status_code=201)
async def create_job(
    body: JobCreate,
    request: Request,
    response: Response,
    user: str = Depends(require_user),
    idempotency_key: str | None = Header(default=None),
):
    model = resolve_model(request.app.state.settings, body.agent, body.model)
    async with request.app.state.pool.connection() as conn:
        try:
            job_id, created = await queue.create_job(
                conn,
                created_by=user,
                task_ids=body.task_ids,
                agent=body.agent,
                model=model,
                attempts=body.attempts,
                judge=body.judge,
                idempotency_key=idempotency_key,
                kind=body.kind,
                options=body.options.model_dump(exclude_none=True),
            )
        except queue.JobRequestError as e:
            raise HTTPException(422, str(e)) from e
    if not created:
        response.status_code = 200
    return await job(job_id, request)


@router.get("/jobs")
async def jobs(request: Request, limit: int = 50):
    async with request.app.state.pool.connection() as conn:
        return await queue.list_jobs(conn, limit=min(limit, 200))


@router.get("/jobs/{job_id}")
async def job(job_id: UUID, request: Request):
    """A job, its runs, and for a Task QA job ("kind": "qa") the per-task report and
    flags under "qa" (null for other jobs)."""
    async with request.app.state.pool.connection() as conn:
        row = await queue.get_job(conn, job_id)
        if row is None:
            raise HTTPException(404, "job not found")
        runs = await queue.list_runs(conn, job_id)
        tasks = await queue.job_tasks(conn, job_id)
        reviews = await queue.flag_reviews(conn, job_id) if row["kind"] == JobKind.QA else []
    report = qa.report(row, runs, tasks, reviews) if row["kind"] == JobKind.QA else None
    return {**row, "config": job_config(row, tasks), "qa": report, "runs": [run_view(r) for r in runs]}


async def save_flag_review(request: Request, job_id: UUID, review: FlagReview, user: str) -> dict:
    """Record a verdict on a flag the job's Task QA report currently raises. Raises 404
    for an unknown job, 409 for a flag the report doesn't raise (any more)."""
    async with request.app.state.pool.connection() as conn:
        row = await queue.get_job(conn, job_id)
        if row is None:
            raise HTTPException(404, "job not found")
        if row["kind"] != JobKind.QA:
            raise HTTPException(409, "only Task QA jobs have flags")
        runs = await queue.list_runs(conn, job_id)
        tasks = await queue.job_tasks(conn, job_id)
        report = qa.report(row, runs, tasks, [])
        raised = {(t["task_id"], f["code"]) for t in report["tasks"] for f in t["flags"]}
        if (review.task_id, review.code) not in raised:
            raise HTTPException(409, f"{review.task_id} has no {review.code} flag in this job")
        return await queue.review_flag(
            conn, job_id, review.task_id, review.code, verdict=review.verdict, note=review.note.strip(), by=user
        )


@router.post("/jobs/{job_id}/flag-reviews")
async def review_flag(job_id: UUID, body: FlagReview, request: Request, user: str = Depends(require_user)):
    """Mark a Task QA flag as valid or a false positive, with an optional note. Reviewing
    the same flag again replaces the verdict."""
    return await save_flag_review(request, job_id, body, user)


@router.post("/jobs/{job_id}/cancel")
async def cancel(job_id: UUID, request: Request, user: str = Depends(require_user)):
    """Idempotent. Queued runs are cancelled now; running ones are stopped, their
    sandboxes destroyed and partial artifacts synced, ending as cancelled. A job with
    nothing left to stop is returned unchanged."""
    async with request.app.state.pool.connection() as conn:
        if await queue.cancel_job(conn, job_id, by=user) is None:
            raise HTTPException(404, "job not found")
    return await job(job_id, request)


@router.get("/runs/{run_id}")
async def run(run_id: UUID, request: Request):
    async with request.app.state.pool.connection() as conn:
        row = await queue.get_run(conn, run_id)
        if row is None:
            raise HTTPException(404, "run not found")
        events = await queue.run_events(conn, run_id)
    out = run_view(row, full=True)
    out["events"] = events
    if row["artifact_prefix"]:
        store = request.app.state.store
        prefix = row["artifact_prefix"]
        out["files"] = [k.removeprefix(prefix + "/") for k in store.list(prefix)]
    return out


@router.post("/runs/{run_id}/retry", status_code=201)
async def retry_run(run_id: UUID, request: Request, response: Response, user: str = Depends(require_user)):
    """Queue another try of a finished run's task attempt, linked to its latest try.
    201 with the new run; 200 with the pending one if a manual retry is already queued;
    409 if the attempt is still being tried, or its job is still being cancelled."""
    async with request.app.state.pool.connection() as conn:
        try:
            result = await queue.manual_retry(conn, run_id, requested_by=user)
        except queue.RetryRefused as e:
            raise HTTPException(409, str(e)) from e
    if result is None:
        raise HTTPException(404, "run not found")
    new, created = result
    if not created:
        response.status_code = 200
    return await run(new["id"], request)


async def _artifact_prefix(request: Request, run_id: UUID) -> str:
    async with request.app.state.pool.connection() as conn:
        row = await queue.get_run(conn, run_id)
    if row is None:
        raise HTTPException(404, "run not found")
    if not row["artifact_prefix"]:
        raise HTTPException(404, f"no artifacts yet (run is {row['status']})")
    return row["artifact_prefix"]


@router.get("/runs/{run_id}/transcript")
async def transcript(run_id: UUID, request: Request):
    prefix = await _artifact_prefix(request, run_id)
    store = request.app.state.store
    for name in TRANSCRIPT_FILES:
        if store.exists(f"{prefix}/{name}"):
            media = "application/json" if name.endswith(".json") else "text/plain; charset=utf-8"
            return FileResponse(store.path(f"{prefix}/{name}"), media_type=media, headers=ARTIFACT_HEADERS)
    raise HTTPException(404, "run finished without a transcript; see /files for harbor.log")


@router.get("/runs/{run_id}/files/{path:path}")
async def run_file(run_id: UUID, path: str, request: Request):
    prefix = await _artifact_prefix(request, run_id)
    store = request.app.state.store
    try:
        local = store.path(f"{prefix}/{path}")
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    if not local.is_file():
        raise HTTPException(404, "file not found")
    media, _ = mimetypes.guess_type(local.name)
    if media is None or local.suffix in {".log", ".txt", ".md", ".sh", ".py", ".toml"}:
        media = "text/plain; charset=utf-8"
    return FileResponse(local, media_type=media, headers=ARTIFACT_HEADERS)
