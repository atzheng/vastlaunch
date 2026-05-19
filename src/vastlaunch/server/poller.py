"""State machine poller for server-submitted jobs.

Called every ~2 minutes by the background task in app.py.
Only processes jobs where config_yaml IS NOT NULL (server path).
CLI-launched jobs are managed by the CLI process itself.

State transitions:
  queued -> launching   (create_instance succeeds)
  launching -> connecting  (instance running + SSH info available)
  connecting -> running    (SSH ready, workdir synced, tmux started)
  running -> success/failed  (exit code file present)
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

from vastlaunch import config, ssh, state, vast
from vastlaunch.server import r2
from vastlaunch.runner import (
    REMOTE_EXIT_CODE,
    REMOTE_LOG,
    TMUX_SESSION,
    _TERMINAL_ERROR_STATUSES,
    _onstart_script,
    _push_run_script,
    _start_detached,
    _status_msg_is_error,
    _sync_workdir,
    find_offer,
)


_SSH_KEY = os.environ.get("VASTLAUNCH_SSH_KEY")

_CONNECTING_TIMEOUT = 600  # seconds before giving up on SSH becoming ready


def log(msg: str) -> None:
    print(f"[vastlaunch.poller] {msg}", file=sys.stderr, flush=True)


def poll_all() -> None:
    """Advance the state machine for every active server-submitted job."""
    jobs = state.all_active_jobs()
    if not jobs:
        return
    log(f"polling {len(jobs)} active job(s)")
    for job in jobs:
        job_id = job["job_id"]
        try:
            _advance(job)
        except Exception as e:
            log(f"{job_id}: unhandled error — {e}")


# ---------------------------------------------------------------------------
# state machine
# ---------------------------------------------------------------------------

def _advance(job: dict) -> None:
    status = job["status"]
    job_id = job["job_id"]
    log(f"{job_id}: status={status}")
    if status == "queued":
        _do_launch(job)
    elif status == "launching":
        _check_launching(job)
    elif status == "connecting":
        _check_connecting(job)
    elif status == "running":
        _check_running(job)


def _do_launch(job: dict) -> None:
    """queued → launching: pick an offer and create the instance."""
    job_id = job["job_id"]
    job_config = _load_config(job)
    if job_config is None:
        return

    skip_ids = set(state.blacklist_get())
    try:
        offer = find_offer(job_config, skip_ids=skip_ids)
    except Exception as e:
        log(f"{job_id}: no offer found — {e}")
        state.update_by_job_id(job_id, status="failed")
        return

    onstart = _onstart_script(job_config, job_id)
    bid = None
    if job_config.resources.use_spot:
        bid = job_config.resources.max_price or float(offer.get("min_bid", 0.0)) * 1.1

    try:
        instance_id = vast.create_instance(
            offer_id=offer["id"],
            image=job_config.image,
            disk_gb=job_config.resources.disk_size,
            onstart_cmd=onstart,
            label=job_config.name,
            use_spot=job_config.resources.use_spot,
            bid_price=bid,
        )
    except vast.VastError as e:
        log(f"{job_id}: create_instance failed — {e}")
        state.update_by_job_id(job_id, status="failed")
        return

    state.update_by_job_id(job_id, instance_id=instance_id, status="launching")
    log(f"{job_id}: instance {instance_id} created — config:\n{job.get('config_yaml', '').strip()}")


def _check_launching(job: dict) -> None:
    """launching → connecting: wait for instance to be running with SSH info."""
    job_id = job["job_id"]
    instance_id = job.get("instance_id")
    if not instance_id:
        return

    try:
        info = vast.show_instance(instance_id)
    except vast.VastError as e:
        log(f"{job_id}: show_instance error (transient) — {e}")
        return

    status = info.get("actual_status") or info.get("intended_status") or "?"
    msg = info.get("status_msg") or ""

    if status == "running":
        host, port = vast.get_ssh_info(info)
        if host and port:
            state.update_by_job_id(job_id, status="connecting", host=host, port=port)
            log(f"{job_id}: instance ready at {host}:{port}")
    elif status in _TERMINAL_ERROR_STATUSES:
        log(f"{job_id}: instance failed ({status}) — {msg}")
        _destroy_and_fail(job)
    elif msg and _status_msg_is_error(msg):
        log(f"{job_id}: startup error — {msg}")
        _destroy_and_fail(job)


def _check_connecting(job: dict) -> None:
    """connecting → running: probe SSH, sync workdir, start tmux."""
    job_id = job["job_id"]
    host, port = job.get("host"), job.get("port")
    if not host or not port:
        return

    # Timeout guard — if we've been connecting too long something is wrong
    since = time.time() - (job.get("updated_at") or job.get("started_at") or 0)
    if since > _CONNECTING_TIMEOUT:
        log(f"{job_id}: SSH connect timeout after {int(since)}s")
        _destroy_and_fail(job)
        return

    # Single non-blocking SSH probe
    from vastlaunch.ssh import _ssh_cmd  # noqa: PLC0415
    cmd = _ssh_cmd(host, port, key=_SSH_KEY, connect_timeout=8) + ["echo ok"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=12)
    except subprocess.TimeoutExpired:
        log(f"{job_id}: SSH probe timed out (key={_SSH_KEY})")
        return
    if proc.returncode != 0 or "ok" not in proc.stdout:
        log(f"{job_id}: SSH probe failed rc={proc.returncode} stderr={proc.stderr.strip()!r} (key={_SSH_KEY})")
        return  # not ready yet, retry next tick

    # SSH is ready — sync workdir (if any) and start the job in tmux
    job_config = _load_config(job)
    if job_config is None:
        return

    workdir_key = job.get("workdir_key")
    if workdir_key:
        try:
            data = r2.download(workdir_key)
            with tempfile.TemporaryDirectory() as tmpdir:
                with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
                    tf.extractall(tmpdir)
                _sync_workdir(host, port, Path(tmpdir), ssh_key=_SSH_KEY)
        except Exception as e:
            log(f"{job_id}: workdir download/rsync failed — {e}")
            _destroy_and_fail(job)
            return
    else:
        config_path = job.get("config_path")
        if config_path:
            workdir = Path(config_path)
        elif job_config.workdir:
            workdir = Path(job_config.workdir).resolve()
        else:
            workdir = None

        if workdir and workdir.exists():
            try:
                _sync_workdir(host, port, workdir, ssh_key=_SSH_KEY)
            except Exception as e:
                log(f"{job_id}: rsync failed — {e}")
                _destroy_and_fail(job)
                return

    try:
        _push_run_script(host, port, job_config, ssh_key=_SSH_KEY)
        _start_detached(host, port, ssh_key=_SSH_KEY)
    except Exception as e:
        log(f"{job_id}: failed to start job — {e}")
        _destroy_and_fail(job)
        return

    state.update_by_job_id(job_id, status="running")
    log(f"{job_id}: job started in tmux")


def _check_running(job: dict) -> None:
    """running → success/failed: check for exit-code marker via SSH."""
    job_id = job["job_id"]
    host, port = job.get("host"), job.get("port")
    if not host or not port:
        return

    rc, out, _ = ssh.exec_remote(
        host, port,
        f"cat {REMOTE_EXIT_CODE} 2>/dev/null",
        key=_SSH_KEY, stream=False, capture=True,
    )
    if rc != 0 or not out.strip().isdigit():
        # Also check that the instance is still alive
        instance_id = job.get("instance_id")
        if instance_id:
            try:
                info = vast.show_instance(instance_id)
                actual = info.get("actual_status")
                if actual in _TERMINAL_ERROR_STATUSES:
                    log(f"{job_id}: instance went {actual} without exit code")
                    state.update_by_job_id(job_id, status="failed")
            except vast.VastError:
                pass
        return

    code = int(out.strip())
    new_status = "success" if code == 0 else "failed"

    # Capture full log before potentially destroying the instance
    full_log = ""
    try:
        _, full_log, _ = ssh.exec_remote(
            host, port,
            f"cat {REMOTE_LOG} 2>/dev/null",
            key=_SSH_KEY, stream=False, capture=True,
        )
    except Exception as e:
        log(f"{job_id}: could not capture logs — {e}")

    state.update_by_job_id(job_id, status=new_status, exit_code=code, logs=full_log)
    log(f"{job_id}: finished — status={new_status} exit_code={code}")

    job_config = _load_config(job)
    auto_destroy = job_config.auto_destroy if job_config else True
    instance_id = job.get("instance_id")
    if auto_destroy and instance_id:
        try:
            vast.destroy_instance(instance_id)
            log(f"{job_id}: instance {instance_id} destroyed")
        except Exception as e:
            log(f"{job_id}: destroy failed — {e}")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_config(job: dict) -> config.Job | None:
    config_yaml = job.get("config_yaml")
    if not config_yaml:
        log(f"{job['job_id']}: no config_yaml, marking failed")
        state.update_by_job_id(job["job_id"], status="failed")
        return None
    try:
        return config.load_from_string(config_yaml)
    except Exception as e:
        log(f"{job['job_id']}: failed to parse config — {e}")
        state.update_by_job_id(job["job_id"], status="failed")
        return None


def _destroy_and_fail(job: dict) -> None:
    job_id = job["job_id"]
    instance_id = job.get("instance_id")
    if instance_id:
        try:
            vast.destroy_instance(instance_id)
        except Exception as e:
            log(f"{job_id}: destroy failed — {e}")
    state.update_by_job_id(job_id, status="failed")
