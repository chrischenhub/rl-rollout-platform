"""FastAPI app: JSON API, HTML pages and Google sign-in."""

from __future__ import annotations

import shlex
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Request
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from rollout import outcomes
from rollout.api import routes_api, routes_ui, timefmt
from rollout.api.auth import LoginRequired, auth_router, make_oauth, require_user, ui_user
from rollout.config import Settings
from rollout.db import make_pool, migrate
from rollout.store import LocalStore

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"


def _reward(value) -> str:
    return "—" if value is None else f"{float(value):.2f}"


def make_templates(display_tz: ZoneInfo) -> Jinja2Templates:
    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters.update(
        duration=timefmt.duration, reward=_reward, when=lambda v: timefmt.when(v, display_tz), short=lambda v: str(v)[:8],
        shell=shlex.join,
    )
    templates.env.globals.update(OUTCOMES=outcomes.ORDER, OUTCOME_LABELS=outcomes.LABELS)
    return templates


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await migrate(settings.database_url)
        pool = make_pool(settings.database_url)
        await pool.open(wait=True)
        app.state.pool = pool
        try:
            yield
        finally:
            await pool.close()

    # The API docs are served below, behind sign-in like everything else.
    app = FastAPI(title="Rollout platform", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.store = LocalStore(settings.store_dir)
    app.state.display_tz = ZoneInfo(settings.display_timezone)
    app.state.templates = make_templates(app.state.display_tz)
    app.state.oauth = make_oauth(settings)
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.session_secret,
        session_cookie="rollout_session",
        max_age=7 * 24 * 3600,
        same_site="lax",  # cookies are not sent on cross-site POSTs, which covers CSRF for the forms
        https_only=settings.base_url.startswith("https://"),
    )
    app.include_router(routes_api.router)
    app.include_router(auth_router)
    app.include_router(routes_ui.router)

    @app.exception_handler(LoginRequired)
    async def login_required(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/login?next={quote(exc.next_path)}", 303)

    @app.get("/openapi.json", include_in_schema=False)
    async def openapi(user: str = Depends(require_user)):
        return app.openapi()

    @app.get("/docs", include_in_schema=False)
    async def docs(user: str = Depends(ui_user)):
        return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} API")

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        async with app.state.pool.connection() as conn:
            await conn.execute("SELECT 1")
        return {"ok": True}

    return app
