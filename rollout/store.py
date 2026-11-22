"""Artifact store: task bundles and synced trial directories.

Keys are relative POSIX paths. The local backend keeps them under one directory
(a Docker volume in deployment). An S3/Spaces backend would implement the same
methods if workers ever run on a different machine from the API.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path, PurePosixPath


class LocalStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        rel = PurePosixPath(key)
        if rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"invalid store key: {key!r}")
        return self.root.joinpath(*rel.parts)

    def exists(self, key: str) -> bool:
        return self.path(key).exists()

    def put_file(self, key: str, src: Path) -> None:
        dest = self.path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.{uuid.uuid4().hex}.tmp")
        shutil.copyfile(src, tmp)
        os.replace(tmp, dest)

    def put_dir(self, prefix: str, src: Path) -> int:
        """Copy a directory tree under `prefix`. Returns the number of files copied."""
        n = 0
        for f in sorted(src.rglob("*")):
            if f.is_file() and not f.is_symlink():
                self.put_file(f"{prefix}/{f.relative_to(src).as_posix()}", f)
                n += 1
        return n

    def list(self, prefix: str) -> list[str]:
        base = self.path(prefix)
        if not base.is_dir():
            return []
        return sorted(f"{prefix}/{f.relative_to(base).as_posix()}" for f in base.rglob("*") if f.is_file())
