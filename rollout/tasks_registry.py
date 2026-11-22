"""Import Harbor task directories into the platform and unpack them for workers.

A task version is identified by a sha256 digest of its directory contents. The
bundle is stored once per digest; workers unpack it into a local cache keyed by
the same digest, so every run executes exactly the version its job pinned.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

from psycopg import AsyncConnection
from psycopg.types.json import Jsonb

from rollout.store import LocalStore

IGNORED = {".DS_Store", "__pycache__"}


@dataclass(frozen=True)
class TaskSpec:
    id: str
    slug: str
    environment_id: str
    version: str
    description: str
    instruction: str
    config: dict
    path: Path


def _files(root: Path) -> list[Path]:
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in IGNORED)
        out.extend(Path(dirpath) / f for f in sorted(filenames) if f not in IGNORED)
    return out


def digest_dir(root: Path) -> str:
    h = hashlib.sha256()
    for f in _files(root):
        rel = f.relative_to(root).as_posix()
        executable = os.access(f, os.X_OK)
        h.update(f"{rel}\0{int(executable)}\0".encode())
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def read_task(path: Path) -> TaskSpec:
    config = tomllib.loads((path / "task.toml").read_text())
    task = config.get("task", {})
    task_id = task.get("name") or path.name
    env_id = task_id.split("/", 1)[0] if "/" in task_id else "default"
    return TaskSpec(
        id=task_id,
        slug=path.name,
        environment_id=env_id,
        version=str(task.get("version", "0")),
        description=task.get("description", ""),
        instruction=(path / "instruction.md").read_text(),
        config=config,
        path=path,
    )


def discover(root: Path) -> list[Path]:
    if (root / "task.toml").is_file():
        return [root]
    return sorted(p for p in root.iterdir() if (p / "task.toml").is_file())


async def import_tasks(conn: AsyncConnection, store: LocalStore, root: Path) -> list[tuple[str, str, bool]]:
    """Register every task under `root`. Returns (task_id, digest, new_version) per task."""
    results = []
    for path in discover(root):
        spec = read_task(path)
        digest = digest_dir(path)
        bundle_key = f"tasks/{spec.slug}/{digest}.tar"
        if not store.exists(bundle_key):
            with tempfile.TemporaryDirectory() as tmp:
                tar_path = Path(tmp) / "bundle.tar"
                with tarfile.open(tar_path, "w") as tar:
                    for f in _files(path):
                        tar.add(f, arcname=f"{spec.slug}/{f.relative_to(path).as_posix()}")
                store.put_file(bundle_key, tar_path)
        size = store.path(bundle_key).stat().st_size

        async with conn.transaction():
            await conn.execute(
                "INSERT INTO environments (id, name) VALUES (%s, %s) ON CONFLICT (id) DO NOTHING",
                (spec.environment_id, spec.environment_id),
            )
            await conn.execute(
                """
                INSERT INTO tasks (id, environment_id, slug, description, current_digest)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    description = EXCLUDED.description, current_digest = EXCLUDED.current_digest, updated_at = now()
                """,
                (spec.id, spec.environment_id, spec.slug, spec.description, digest),
            )
            cur = await conn.execute(
                """
                INSERT INTO task_versions (task_id, digest, version, instruction, config, bundle_key, size_bytes)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id, digest) DO NOTHING
                """,
                (spec.id, digest, spec.version, spec.instruction, Jsonb(spec.config), bundle_key, size),
            )
            results.append((spec.id, digest, cur.rowcount == 1))
    return results


def materialize(store: LocalStore, cache_dir: Path, slug: str, digest: str, bundle_key: str) -> Path:
    """Return a local task directory for this digest, unpacking the bundle on first use."""
    final = cache_dir / digest
    task_dir = final / slug
    if task_dir.is_dir():
        return task_dir
    cache_dir.mkdir(parents=True, exist_ok=True)
    staging = cache_dir / f".{digest}.{uuid.uuid4().hex}"
    try:
        with tarfile.open(store.path(bundle_key)) as tar:
            tar.extractall(staging, filter="data")
        try:
            os.rename(staging, final)
        except OSError:
            if not task_dir.is_dir():  # not a concurrent unpack of the same digest
                raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return task_dir
