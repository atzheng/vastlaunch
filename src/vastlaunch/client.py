"""Thin HTTP client for the vastlaunch server.

Used by the CLI when VASTLAUNCH_SERVER_URL is set.
No extra dependencies — stdlib urllib only.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def server_url() -> str | None:
    """Return the server base URL, or None if not configured."""
    return os.environ.get("VASTLAUNCH_SERVER_URL")


def _request(
    method: str,
    path: str,
    body: bytes | None = None,
    content_type: str = "text/plain",
) -> dict | list | None:
    base = (server_url() or "").rstrip("/")
    url = base + path
    api_key = os.environ.get("VASTLAUNCH_API_KEY")
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if body is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, method=method, headers=headers, data=body)
    try:
        with urllib.request.urlopen(req) as resp:
            data = resp.read()
            return json.loads(data) if data else None
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            detail = json.loads(raw).get("detail", raw)
        except Exception:
            detail = raw
        raise RuntimeError(f"server returned HTTP {e.code}: {detail}") from e


def submit(yaml_text: str) -> dict:
    """POST a job YAML. Returns {job_id, name, status}."""
    return _request("POST", "/jobs", yaml_text.encode())  # type: ignore[return-value]


def list_jobs() -> list[dict]:
    return _request("GET", "/jobs") or []  # type: ignore[return-value]


def get_job(job_id: str) -> dict:
    return _request("GET", f"/jobs/{job_id}")  # type: ignore[return-value]


def get_logs(job_id: str, n: int = 200, since: int = 0) -> str:
    params = f"since={since}" if since > 0 else f"n={n}"
    data = _request("GET", f"/jobs/{job_id}/logs?{params}") or {}
    return data.get("logs", "")  # type: ignore[union-attr]


def upload_workdir(job_id: str, data: bytes) -> None:
    """PUT a gzipped tar of the workdir for a job."""
    _request("PUT", f"/jobs/{job_id}/workdir", data, content_type="application/octet-stream")


def destroy_job(job_id: str) -> None:
    _request("DELETE", f"/jobs/{job_id}")
