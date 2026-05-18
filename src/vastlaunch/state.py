"""Local state DB for managed jobs.

Stored as JSON at $VASTLAUNCH_STATE_DIR (default: ~/.vastlaunch).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def state_dir() -> Path:
    p = Path(os.environ.get("VASTLAUNCH_STATE_DIR", "~/.vastlaunch")).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _db_path() -> Path:
    return state_dir() / "jobs.json"


def _read_all() -> dict[str, dict]:
    p = _db_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError:
        return {}


def _write_all(data: dict[str, dict]) -> None:
    p = _db_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True))
    tmp.replace(p)


def add(instance_id: int | str, *, name: str, config_path: str | None = None,
        host: str | None = None, port: int | None = None) -> None:
    data = _read_all()
    data[str(instance_id)] = {
        "instance_id": int(instance_id),
        "name": name,
        "config_path": config_path,
        "host": host,
        "port": port,
        "started_at": time.time(),
        "status": "launching",
    }
    _write_all(data)


def update(instance_id: int | str, **fields: Any) -> None:
    data = _read_all()
    key = str(instance_id)
    if key in data:
        data[key].update(fields)
        data[key]["updated_at"] = time.time()
        _write_all(data)


def get(instance_id: int | str) -> dict | None:
    return _read_all().get(str(instance_id))


def remove(instance_id: int | str) -> None:
    data = _read_all()
    data.pop(str(instance_id), None)
    _write_all(data)


def all_jobs() -> dict[str, dict]:
    return _read_all()


def log_path(instance_id: int | str) -> Path:
    return state_dir() / f"{instance_id}.log"
