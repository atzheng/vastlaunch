"""Local-disk storage for vastlaunch workdir tarballs.

Files are stored under WORKDIR_STORE (default: workdir_store/).
The interface mirrors the old R2 module so the rest of the app is unchanged.
"""

from __future__ import annotations

import os
from pathlib import Path


def _store_dir() -> Path:
    d = Path(os.environ.get("WORKDIR_STORE", "workdir_store"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def upload(key: str, data: bytes) -> None:
    path = _store_dir() / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def download(key: str) -> bytes:
    return (_store_dir() / key).read_bytes()


def delete(key: str) -> None:
    path = _store_dir() / key
    path.unlink(missing_ok=True)
