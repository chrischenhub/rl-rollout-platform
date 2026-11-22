"""Who is calling: a personal API token (Authorization: Bearer) or a signed-in session.

Access is limited to verified emails in ALLOWED_EMAIL_DOMAINS (plus individual
ALLOWED_EMAILS), checked on every request so removing one from config revokes its
tokens too. Sign-in is Google OpenID Connect, accepting Workspace accounts of an
allowed domain or an individually allowed account.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import warnings
from typing import Any

import authlib.deprecate

# Authlib warns about its own httpx shim on import, and its deprecate module installs
# an "always" filter, so ours has to come after it. Nothing for us to act on.
warnings.filterwarnings("ignore", message="The httpx module is deprecated", category=authlib.deprecate.AuthlibDeprecationWarning)

from authlib.integrations.starlette_client import OAuth, OAuthError  # noqa: E402
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from psycopg import AsyncConnection

log = logging.getLogger(__name__)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def email_allowed(email: str, domains: tuple[str, ...], emails: tuple[str, ...] = ()) -> bool:
    email = email.lower()
    return email in emails or email.rsplit("@", 1)[-1] in {d.lower() for d in domains}


async def upsert_user(conn: AsyncConnection, email: str, name: str | None = None) -> None:
    await conn.execute(
        """
        INSERT INTO users (email, name, last_login_at) VALUES (%s, %s, now())
        ON CONFLICT (email) DO UPDATE SET name = coalesce(EXCLUDED.name, users.name), last_login_at = now()
        """,
        (email.lower(), name),
    )


async def create_token(conn: AsyncConnection, email: str, label: str = "") -> str:
    token = "rp_" + secrets.token_urlsafe(32)
    await upsert_user(conn, email)
    await conn.execute(
        "INSERT INTO api_tokens (email, token_hash, label) VALUES (%s, %s, %s)",
        (email.lower(), hash_token(token), label),
    )
    return token


async def require_user(request: Request) -> str:
    settings = request.app.state.settings
    email: str | None = None
    header = request.headers.get("authorization", "")
    if header.lower().startswith("bearer "):
        async with request.app.state.pool.connection() as conn:
            cur = await conn.execute(
                "UPDATE api_tokens SET last_used_at = now()"
                " WHERE token_hash = %s AND revoked_at IS NULL RETURNING email",
                (hash_token(header[7:].strip()),),
            )
            row = await cur.fetchone()
        if row is None:
            raise HTTPException(401, "invalid or revoked API token")
        email = row["email"]
    elif "session" in request.scope:
        email = request.session.get("email")
    if not email:
        raise HTTPException(401, "sign in, or pass Authorization: Bearer <token>")
    if not email_allowed(email, settings.allowed_domains, settings.allowed_emails):
        raise HTTPException(403, f"{email} is not allowed to use this platform")
    return email


# --- Google sign-in -------------------------------------------------------------------


class LoginRequired(Exception):
    """Raised by page routes; the app turns it into a redirect to /login."""

    def __init__(self, next_path: str = "/"):
        self.next_path = next_path


def safe_next(path: str | None) -> str:
    """Only same-site relative paths, so ?next= can't redirect off-site."""
    if not path or not path.startswith("/") or path.startswith("//") or "\\" in path:
        return "/"
    return path


def identity_error(claims: dict[str, Any], domains: tuple[str, ...], emails: tuple[str, ...] = ()) -> str | None:
    """Why a Google identity may not sign in, or None if it may.

    `hd` is only present for accounts managed by a Google Workspace, so requiring it
    to equal the email's domain rejects personal Google accounts that merely use an
    allowed address. Individually allowed emails skip the domain rule.
    """
    email = (claims.get("email") or "").lower()
    if not email or not claims.get("email_verified"):
        return "Google did not return a verified email address."
    if email in emails:
        return None
    domain = email.rsplit("@", 1)[-1]
    if not email_allowed(email, domains) or (claims.get("hd") or "").lower() != domain:
        allowed = " or ".join(f"@{d}" for d in domains)
        return f"{email} can't use this platform. Sign in with your {allowed} Google Workspace account."
    return None


async def ui_user(request: Request) -> str:
    settings = request.app.state.settings
    email = request.session.get("email")
    if not email or not email_allowed(email, settings.allowed_domains, settings.allowed_emails):
        request.session.pop("email", None)
        raise LoginRequired(request.url.path)
    return email


def make_oauth(settings) -> OAuth:
    oauth = OAuth()
    if settings.google_enabled:
        oauth.register(
            "google",
            client_id=settings.google_client_id,
            client_secret=settings.google_client_secret,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    return oauth


auth_router = APIRouter(include_in_schema=False)


@auth_router.get("/auth/google")
async def google_start(request: Request, next: str = "/"):
    settings = request.app.state.settings
    if not settings.google_enabled:
        raise HTTPException(503, "Google sign-in is not configured")
    request.session["next"] = safe_next(next)
    domains = settings.allowed_domains
    # hd only narrows Google's account chooser (the callback enforces access). Skip it
    # when individual accounts outside the domains are allowed, or they can't pick theirs.
    hint = {} if settings.allowed_emails else {"hd": domains[0] if len(domains) == 1 else "*"}
    return await request.app.state.oauth.google.authorize_redirect(
        request, f"{settings.base_url}/auth/callback", prompt="select_account", **hint
    )


@auth_router.get("/auth/callback")
async def google_callback(request: Request):
    settings = request.app.state.settings
    templates = request.app.state.templates
    try:
        token = await request.app.state.oauth.google.authorize_access_token(request)
    except OAuthError as e:
        log.warning("google sign-in failed: %s", e)
        return templates.TemplateResponse(
            request, "login.html", {"error": f"Google sign-in failed: {e.description or e.error}"}, status_code=400
        )
    claims = token.get("userinfo") or {}
    if error := identity_error(claims, settings.allowed_domains, settings.allowed_emails):
        log.warning("rejected sign-in: %s", claims.get("email"))
        return templates.TemplateResponse(request, "login.html", {"error": error}, status_code=403)
    email = claims["email"].lower()
    async with request.app.state.pool.connection() as conn:
        await upsert_user(conn, email, claims.get("name"))
    next_path = safe_next(request.session.get("next"))
    request.session.clear()  # fresh session on sign-in
    request.session["email"] = email
    log.info("signed in: %s", email)
    return RedirectResponse(next_path, 303)


@auth_router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", 303)
