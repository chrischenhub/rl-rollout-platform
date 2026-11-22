"""Settings, read once from the environment (plus optional .env / secrets.env files)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

ENV_FILES = (".env", "secrets.env")


def load_env_files(paths: tuple[str, ...] = ENV_FILES) -> None:
    """Load KEY=VALUE lines (an `export ` prefix is allowed). Real env vars win."""
    for name in paths:
        path = Path(name)
        if not path.is_file():
            continue
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.removeprefix("export ").split("=", 1)
            key, value = key.strip(), value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            os.environ.setdefault(key, value)


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _list(name: str, default: str) -> tuple[str, ...]:
    return tuple(v.strip() for v in os.environ.get(name, default).split(",") if v.strip())


@dataclass(frozen=True)
class Settings:
    database_url: str
    data_dir: Path
    # Distinguishes deployments that share one Daytona account (local dev vs droplet),
    # so one deployment's janitor never deletes another's sandboxes.
    instance: str

    harbor_bin: str
    worker_slots: int
    max_running: int
    lease_ttl_sec: int
    heartbeat_sec: int
    poll_sec: float
    reap_interval_sec: int
    janitor_interval_sec: int
    deadline_margin_sec: int
    kill_grace_sec: int
    max_infra_retries: int
    retry_backoff_sec: int

    models: tuple[str, ...]
    judge_model: str
    display_timezone: str  # for times in the UI; the API stays in UTC
    anthropic_api_key: str | None
    daytona_api_key: str | None
    daytona_api_url: str | None

    base_url: str
    allowed_domains: tuple[str, ...]
    # Individual accounts allowed regardless of domain (e.g. a personal Gmail for testing).
    allowed_emails: tuple[str, ...]
    session_secret: str
    google_client_id: str | None
    google_client_secret: str | None

    @property
    def store_dir(self) -> Path:
        return self.data_dir / "store"

    @property
    def task_cache_dir(self) -> Path:
        return self.data_dir / "task-cache"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"

    @property
    def google_enabled(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ.get
        google_id, google_secret = e("GOOGLE_CLIENT_ID") or None, e("GOOGLE_CLIENT_SECRET") or None
        if not google_id and e("GOOGLE_CLIENT_SECRETS_FILE"):
            # The JSON Google Cloud console downloads for a web client.
            web = json.loads(Path(e("GOOGLE_CLIENT_SECRETS_FILE")).read_text())["web"]
            google_id, google_secret = web["client_id"], web["client_secret"]
        return cls(
            database_url=e("DATABASE_URL", "postgresql://rollout:rollout@localhost:5433/rollout"),
            data_dir=Path(e("DATA_DIR", "./data")).resolve(),
            instance=e("ROLLOUT_INSTANCE", "local"),
            harbor_bin=e("HARBOR_BIN", "harbor"),
            worker_slots=_int("WORKER_SLOTS", 4),
            max_running=_int("MAX_RUNNING", 8),
            lease_ttl_sec=_int("LEASE_TTL_SEC", 60),
            heartbeat_sec=_int("HEARTBEAT_SEC", 15),
            poll_sec=float(e("POLL_SEC", "2")),
            reap_interval_sec=_int("REAP_INTERVAL_SEC", 15),
            janitor_interval_sec=_int("JANITOR_INTERVAL_SEC", 300),
            deadline_margin_sec=_int("DEADLINE_MARGIN_SEC", 600),
            kill_grace_sec=_int("KILL_GRACE_SEC", 45),
            max_infra_retries=_int("MAX_INFRA_RETRIES", 2),
            retry_backoff_sec=_int("RETRY_BACKOFF_SEC", 15),
            models=_list("MODELS", "claude-sonnet-5,claude-sonnet-5-5,claude-opus-5-5,claude-haiku-5-5"),
            judge_model=e("JUDGE_MODEL", "claude-sonnet-5"),
            display_timezone=e("DISPLAY_TIMEZONE", "America/Los_Angeles"),
            anthropic_api_key=e("ANTHROPIC_API_KEY") or None,
            daytona_api_key=e("DAYTONA_API_KEY") or None,
            daytona_api_url=e("DAYTONA_API_URL") or None,
            base_url=e("BASE_URL", "http://localhost:8000").rstrip("/"),
            allowed_domains=_list("ALLOWED_EMAIL_DOMAINS", ""),
            allowed_emails=tuple(e.lower() for e in _list("ALLOWED_EMAILS", "")),
            session_secret=e("SESSION_SECRET", "dev-only-not-secret"),
            google_client_id=google_id,
            google_client_secret=google_secret,
        )
