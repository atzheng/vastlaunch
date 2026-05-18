"""FastAPI server for vastlaunch.

Endpoints:
  POST   /jobs               Submit a job (YAML body)
  GET    /jobs               List all jobs
  GET    /jobs/{job_id}      Get a single job
  GET    /jobs/{job_id}/logs Tail the remote run log
  DELETE /jobs/{job_id}      Destroy a job

A background task polls all active jobs every POLL_INTERVAL seconds.

Environment variables:
  DATABASE_URL               Postgres connection string (required)
  VASTLAUNCH_API_KEY         If set, all requests must supply  Authorization: Bearer <key>
  VASTLAUNCH_SSH_KEY         Path to SSH private key file (optional)
  VASTLAUNCH_SSH_KEY_CONTENT Raw SSH private key text (alternative to VASTLAUNCH_SSH_KEY)
  POLL_INTERVAL              Seconds between polls (default: 120)
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import tempfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import coolname
from fastapi import Depends, FastAPI, HTTPException, Request, Response, Security
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from vastlaunch.server.ui import HTML

from vastlaunch import config, ssh, state, vast
from vastlaunch.runner import REMOTE_LOG
from vastlaunch.server import poller

logger = logging.getLogger("vastlaunch.server")

POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "120"))
_API_KEY = os.environ.get("VASTLAUNCH_API_KEY")
_SSH_KEY = os.environ.get("VASTLAUNCH_SSH_KEY")
_SSH_KEY_CONTENT = os.environ.get("VASTLAUNCH_SSH_KEY_CONTENT")
_tmp_key_file: tempfile.NamedTemporaryFile | None = None

_executor = ThreadPoolExecutor(max_workers=4)
_bearer = HTTPBearer(auto_error=False)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------

def _check_auth(
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
) -> None:
    if not _API_KEY:
        return
    if credentials is None or credentials.credentials != _API_KEY:
        raise HTTPException(status_code=401, detail="invalid or missing API key")


# ---------------------------------------------------------------------------
# lifespan: migrate DB + start background poller
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _SSH_KEY, _tmp_key_file
    if _SSH_KEY_CONTENT and not _SSH_KEY:
        _tmp_key_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pem")
        _tmp_key_file.write(_SSH_KEY_CONTENT.encode())
        _tmp_key_file.flush()
        _tmp_key_file.close()
        os.chmod(_tmp_key_file.name, stat.S_IRUSR | stat.S_IWUSR)
        _SSH_KEY = _tmp_key_file.name
        logger.info("SSH key written to %s", _SSH_KEY)
    state.migrate()
    logger.info("DB migrated")
    task = asyncio.create_task(_poll_loop())
    yield
    task.cancel()
    if _tmp_key_file:
        os.unlink(_tmp_key_file.name)


async def _poll_loop() -> None:
    while True:
        await asyncio.sleep(POLL_INTERVAL)
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(_executor, poller.poll_all)
        except Exception:
            logger.exception("poll_all error")


# ---------------------------------------------------------------------------
# app
# ---------------------------------------------------------------------------

app = FastAPI(title="vastlaunch", lifespan=lifespan)


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
async def ui() -> HTMLResponse:
    return HTMLResponse(HTML)


@app.post("/jobs", status_code=201, dependencies=[Depends(_check_auth)])
async def submit_job(request: Request) -> dict:
    """Submit a job. Body is a vastlaunch YAML config."""
    body = await request.body()
    yaml_text = body.decode()

    try:
        job_config = config.load_from_string(yaml_text)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"invalid job config: {e}")

    job_id = coolname.generate_slug()
    state.enqueue(job_id=job_id, name=job_config.name, config_yaml=yaml_text)
    logger.info("enqueued job %s (%s)\n%s", job_id, job_config.name, yaml_text.strip())
    return {"job_id": job_id, "name": job_config.name, "status": "queued"}


@app.get("/jobs", dependencies=[Depends(_check_auth)])
async def list_jobs() -> list[dict]:
    return list(state.all_jobs().values())


@app.get("/jobs/{job_id}", dependencies=[Depends(_check_auth)])
async def get_job(job_id: str) -> dict:
    job = state.get_by_job_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    return job


@app.get("/jobs/{job_id}/logs", dependencies=[Depends(_check_auth)])
async def get_logs(job_id: str, n: int = 200, since: int = 0) -> dict:
    """Fetch log lines.

    - since=0 (default): return the last `n` lines.
    - since=K: return all lines after line K (0-indexed). Used by --follow.
    """
    job = state.get_by_job_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")

    # Terminal jobs have logs captured in the DB — no SSH needed
    stored = job.get("logs")
    if stored is not None and job.get("status") in ("success", "failed", "stopped"):
        lines = stored.splitlines(keepends=True)
        if since > 0:
            out = "".join(lines[since:])
        else:
            out = "".join(lines[-n:])
        return {"job_id": job_id, "logs": out}

    host, port = job.get("host"), job.get("port")
    if not host or not port:
        raise HTTPException(status_code=503, detail="instance not yet reachable")
    if since > 0:
        cmd = f"tail -n +{since + 1} {REMOTE_LOG} 2>/dev/null"
    else:
        cmd = f"tail -n {n} {REMOTE_LOG} 2>/dev/null"
    loop = asyncio.get_event_loop()
    _, out, _ = await loop.run_in_executor(
        _executor,
        lambda: ssh.exec_remote(host, port, cmd, key=_SSH_KEY, stream=False, capture=True),
    )
    return {"job_id": job_id, "logs": out}


@app.delete("/jobs/{job_id}", status_code=204, dependencies=[Depends(_check_auth)])
async def destroy_job(job_id: str) -> Response:
    job = state.get_by_job_id(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="job not found")
    instance_id = job.get("instance_id")
    if instance_id:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(
                _executor, lambda: vast.destroy_instance(instance_id)
            )
        except vast.VastError as e:
            logger.warning("destroy instance %s failed: %s", instance_id, e)
    state.update_by_job_id(job_id, status="failed")
    return Response(status_code=204)
