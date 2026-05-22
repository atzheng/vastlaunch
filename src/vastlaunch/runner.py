"""End-to-end orchestration: find offer -> launch -> wait -> sync -> run -> teardown."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import coolname

from vastlaunch import ssh, state, vast
from vastlaunch.config import Job

REMOTE_WORKDIR = "/workspace"
REMOTE_RUN_SCRIPT = f"{REMOTE_WORKDIR}/.vastlaunch_run.sh"
REMOTE_LOG = f"{REMOTE_WORKDIR}/run.log"
REMOTE_EXIT_CODE = f"{REMOTE_WORKDIR}/.exit_code"
REMOTE_ENVRC = f"{REMOTE_WORKDIR}/.envrc"
REMOTE_DONE_MARKER = f"{REMOTE_WORKDIR}/.vastlaunch_done"
TMUX_SESSION = "vastlaunch"
MAX_LAUNCH_ATTEMPTS = 5


class _StartupFailed(Exception):
    """Instance failed before SSH was available — safe to blacklist and retry."""
    def __init__(self, offer_id: int, msg: str) -> None:
        super().__init__(msg)
        self.offer_id = offer_id


def log(msg: str) -> None:
    print(f"[vastlaunch] {msg}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# offer selection
# ---------------------------------------------------------------------------

def find_offer(job: Job, skip_ids: set[int] | None = None) -> dict:
    query = vast.build_query(job.resources)
    log(f"searching offers: {query}")
    offers = vast.search_offers(query, limit=30)
    if not offers:
        raise RuntimeError(f"no offers match query: {query}")
    if job.resources.max_price is not None:
        cap = job.resources.max_price
        offers = [o for o in offers if (o.get("dph_total") or 1e9) <= cap]
        if not offers:
            raise RuntimeError(f"no offers under ${cap:.3f}/hr")
    if skip_ids:
        offers = [o for o in offers if o.get("id") not in skip_ids]
        if not offers:
            raise RuntimeError("no offers remaining after excluding blacklisted hosts")
    chosen = offers[0]
    log(
        f"selected offer {chosen.get('id')}: "
        f"${chosen.get('dph_total', 0):.3f}/hr  "
        f"{chosen.get('gpu_name', '?')}x{chosen.get('num_gpus', '?')}  "
        f"reliability {chosen.get('reliability2', 0):.3f}  "
        f"CUDA {chosen.get('cuda_max_good', '?')}  "
        f"{chosen.get('geolocation', '?')}"
    )
    return chosen


# ---------------------------------------------------------------------------
# scripts injected on the remote
# ---------------------------------------------------------------------------

def _onstart_script(job: Job, job_id: str) -> str:
    lines = [
        "#!/bin/bash",
        "set -e",
        # Some vast images don't have rsync/tmux preinstalled.
        # Do NOT install openssh-server — vast.ai manages sshd and its authorized_keys.
        "(apt-get update -qq && apt-get install -y -qq rsync tmux) "
        ">/var/log/vastlaunch-onstart.log 2>&1 || true",
        f"mkdir -p {REMOTE_WORKDIR}",
        f"cat > {REMOTE_ENVRC} <<'__VL_EOF__'",
    ]
    for k, v in job.envs.items():
        if v == "":
            continue
        lines.append(f"export {k}={shlex.quote(str(v))}")
    for k, v in job.secrets.items():
        lines.append(f"export {k}={shlex.quote(str(v))}")
    lines.append(f"export VASTLAUNCH_JOB={shlex.quote(job.name)}")
    lines.append(f"export VASTLAUNCH_JOB_ID={shlex.quote(job_id)}")
    lines.append("__VL_EOF__")
    lines.append(f"chmod 600 {REMOTE_ENVRC}")
    return "\n".join(lines)


def _run_script(job: Job) -> str:
    """The script that does setup + run + writes exit code."""
    setup_block = job.setup.strip() or "true"
    run_block = job.run.strip() or "true"
    return (
        "#!/bin/bash\n"
        f"cd {REMOTE_WORKDIR}\n"
        f"[ -f {REMOTE_ENVRC} ] && source {REMOTE_ENVRC}\n"
        "{\n"
        "echo '[vastlaunch] === setup ==='\n"
        f"( set -e -o pipefail; {setup_block} )\n"
        "RC=$?\n"
        f"if [ $RC -ne 0 ]; then echo $RC > {REMOTE_EXIT_CODE}; touch {REMOTE_DONE_MARKER}; echo \"[vastlaunch] setup failed: $RC\"; exit $RC; fi\n"
        "echo '[vastlaunch] === run ==='\n"
        f"{run_block}\n"
        "RC=$?\n"
        f"echo $RC > {REMOTE_EXIT_CODE}\n"
        f"touch {REMOTE_DONE_MARKER}\n"
        "echo \"[vastlaunch] exit code: $RC\"\n"
        "exit $RC\n"
        "} 2>&1\n"
    )


# ---------------------------------------------------------------------------
# wait/poll helpers
# ---------------------------------------------------------------------------

_TERMINAL_ERROR_STATUSES = {"offline", "exited", "failed", "error"}

_ERROR_MSG_PATTERNS = (
    "error response from daemon",
    "oci runtime",
    "failed to create",
    "failed to start",
    "failed to inject",
    "unresolvable cdi",
    "cannot allocate",
)


def _status_msg_is_error(msg: str) -> bool:
    low = msg.lower()
    return any(p in low for p in _ERROR_MSG_PATTERNS)


def wait_for_running(instance_id: int, timeout: int = 900) -> dict:
    """Poll until vast reports the instance running. Returns the info dict."""
    deadline = time.time() + timeout
    last_status: str | None = None
    last_msg: str | None = None
    consecutive_api_errors = 0
    while time.time() < deadline:
        try:
            info = vast.show_instance(instance_id)
        except vast.VastError as e:
            consecutive_api_errors += 1
            if consecutive_api_errors >= 5:
                raise RuntimeError(f"show_instance failed {consecutive_api_errors} times: {e}") from e
            log(f"show_instance transient error ({consecutive_api_errors}/5): {e}")
            time.sleep(10)
            continue
        consecutive_api_errors = 0
        status = info.get("actual_status") or info.get("intended_status") or "?"
        msg = info.get("status_msg") or ""
        if status != last_status or msg != last_msg:
            log(f"instance {instance_id} status: {status}" + (f" — {msg}" if msg else ""))
            last_status = status
            last_msg = msg
        if status == "running":
            host, port = vast.get_ssh_info(info)
            if host and port:
                return info
        elif status in _TERMINAL_ERROR_STATUSES:
            reason = msg or "no detail"
            raise RuntimeError(f"instance went to '{status}': {reason}")
        elif msg and _status_msg_is_error(msg):
            raise RuntimeError(f"instance startup error (status={status}): {msg}")
        time.sleep(10)
    raise TimeoutError(f"instance {instance_id} never became ready (timeout {timeout}s)")


# ---------------------------------------------------------------------------
# main launch flow
# ---------------------------------------------------------------------------

def launch(
    job: Job,
    *,
    detach: bool = False,
    ssh_key: str | None = None,
    extra_workdir: str | None = None,
    dry_run: bool = False,
) -> int:
    """Full flow. Returns the instance ID.

    detach=False (default): block, stream logs, optionally auto-destroy on completion.
    detach=True: start the job in a tmux session, return the instance ID.

    On startup failure (instance goes offline/exited before SSH is ready), the
    offer is blacklisted and the next cheapest offer is tried automatically, up
    to MAX_LAUNCH_ATTEMPTS times. On any failure the instance is destroyed.
    """
    if dry_run:
        query = vast.build_query(job.resources)
        log(f"DRY RUN — would search: {query}")
        offer = find_offer(job)
        log(f"DRY RUN — would launch image={job.image} disk={job.resources.disk_size}GB")
        log(f"DRY RUN — setup script:\n{job.setup}")
        log(f"DRY RUN — run script:\n{job.run}")
        return -1

    skip_ids: set[int] = set(state.blacklist_get())

    for attempt in range(1, MAX_LAUNCH_ATTEMPTS + 1):
        try:
            return _attempt_launch(
                job,
                skip_ids=skip_ids,
                detach=detach,
                ssh_key=ssh_key,
                extra_workdir=extra_workdir,
            )
        except _StartupFailed as e:
            log(f"startup failed (attempt {attempt}/{MAX_LAUNCH_ATTEMPTS}): {e}")
            log(f"blacklisting offer {e.offer_id}")
            state.blacklist_add(e.offer_id)
            skip_ids.add(e.offer_id)
            if attempt == MAX_LAUNCH_ATTEMPTS:
                raise RuntimeError(
                    f"all {MAX_LAUNCH_ATTEMPTS} launch attempts failed"
                ) from e
            log("retrying with next available offer...")

    raise RuntimeError("unreachable")


def _destroy_best_effort(instance_id: int) -> None:
    """Destroy an instance, logging but not raising on failure."""
    try:
        vast.destroy_instance(instance_id)
    except Exception as e:
        log(f"warning: destroy instance {instance_id} failed: {e}")
    state.remove(instance_id)


def _attempt_launch(
    job: Job,
    *,
    skip_ids: set[int],
    detach: bool,
    ssh_key: str | None,
    extra_workdir: str | None,
) -> int:
    offer = find_offer(job, skip_ids=skip_ids)
    job_id = coolname.generate_slug()
    onstart = _onstart_script(job, job_id)

    bid = None
    if job.resources.use_spot:
        bid = job.resources.max_price or float(offer.get("min_bid", 0.0)) * 1.1
        log(f"spot bid: ${bid:.4f}/hr")

    log(f"creating instance with image {job.image}, disk {job.resources.disk_size}GB...")
    try:
        instance_id = vast.create_instance(
            offer_id=offer["id"],
            image=job.image,
            disk_gb=job.resources.disk_size,
            onstart_cmd=onstart,
            label=job.name,
            use_spot=job.resources.use_spot,
            bid_price=bid,
        )
    except vast.VastError as e:
        raise _StartupFailed(offer["id"], f"create_instance failed: {e}") from e
    log(f"instance {instance_id} created")

    workdir = Path(extra_workdir or job.workdir or ".").resolve() if (extra_workdir or job.workdir) else None
    state.add(instance_id, job_id=job_id, name=job.name, config_path=str(workdir) if workdir else None)
    log(f"job ID: {job_id}")

    try:
        try:
            info = wait_for_running(instance_id)
        except (RuntimeError, TimeoutError) as e:
            # Instance never became healthy — destroy it and signal retry
            log(f"instance {instance_id} failed to start, destroying...")
            _destroy_best_effort(instance_id)
            raise _StartupFailed(offer["id"], str(e)) from e

        host, port = vast.get_ssh_info(info)
        assert host and port
        state.update(instance_id, host=host, port=port, status="connecting")
        log(f"instance ready at {host}:{port}")

        try:
            ssh.wait_for_ssh(host, port, key=ssh_key, timeout=300)
        except TimeoutError as e:
            log(f"SSH never became ready on instance {instance_id}, destroying...")
            _destroy_best_effort(instance_id)
            raise _StartupFailed(offer["id"], str(e)) from e

        log("SSH ready")

        if workdir is not None:
            _sync_workdir(host, port, workdir, ssh_key=ssh_key)

        _push_run_script(host, port, job, ssh_key=ssh_key)
        state.update(instance_id, status="running")

        if detach:
            _start_detached(host, port, ssh_key=ssh_key)
            log(f"job started in tmux session '{TMUX_SESSION}' on instance {instance_id}")
            return instance_id

        rc = _run_foreground(host, port, ssh_key=ssh_key)
        if rc == 0:
            state.update(instance_id, status="success")
            log("job completed successfully")
        else:
            state.update(instance_id, status="failed", exit_code=rc)
            log(f"job failed with exit code {rc}")

        if job.auto_destroy:
            log(f"destroying instance {instance_id}")
            vast.destroy_instance(instance_id)
            state.remove(instance_id)
        return instance_id

    except _StartupFailed:
        raise
    except Exception:
        state.update(instance_id, status="failed")
        log(f"error after launch, destroying instance {instance_id}...")
        _destroy_best_effort(instance_id)
        raise


def _sync_workdir(host: str, port: int, workdir: Path, *, ssh_key: str | None) -> None:
    log(f"syncing {workdir} -> {host}:{REMOTE_WORKDIR}/")
    excludes = list(ssh.DEFAULT_EXCLUDES)
    ignore = workdir / ".vastignore"
    if ignore.exists():
        excludes.extend(
            line.strip() for line in ignore.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        )
    rc = ssh.rsync_to(host, port, f"{workdir}/", f"{REMOTE_WORKDIR}/",
                      key=ssh_key, excludes=excludes)
    if rc != 0:
        raise RuntimeError(f"rsync failed (rc={rc})")


def _push_run_script(host: str, port: int, job: Job, *, ssh_key: str | None) -> None:
    script = _run_script(job)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as f:
        f.write(script)
        local_path = f.name
    try:
        rc = ssh.rsync_to(host, port, local_path, REMOTE_RUN_SCRIPT, key=ssh_key)
        if rc != 0:
            raise RuntimeError("failed to upload run script")
        ssh.exec_remote(host, port, f"chmod +x {REMOTE_RUN_SCRIPT}",
                        key=ssh_key, stream=False, capture=True)
    finally:
        os.unlink(local_path)


def _start_detached(host: str, port: int, *, ssh_key: str | None) -> None:
    cmd = (
        f"tmux kill-session -t {TMUX_SESSION} 2>/dev/null; "
        f"tmux new-session -d -s {TMUX_SESSION} "
        f"'{REMOTE_RUN_SCRIPT} 2>&1 | tee {REMOTE_LOG}'"
    )
    rc, _, err = ssh.exec_remote(host, port, cmd, key=ssh_key, stream=False, capture=True)
    if rc != 0:
        raise RuntimeError(f"failed to start tmux: {err}")


def _run_foreground(host: str, port: int, *, ssh_key: str | None) -> int:
    cmd = f"{REMOTE_RUN_SCRIPT} 2>&1 | tee {REMOTE_LOG}"
    rc, _, _ = ssh.exec_remote(host, port, cmd, key=ssh_key, stream=True)
    return rc


# ---------------------------------------------------------------------------
# status / logs / sync / destroy (post-launch)
# ---------------------------------------------------------------------------

def status(instance_id: int) -> str:
    """Return one of: pending | running | success | failed.

    Designed to be Snakemake-cluster-generic compatible.
    """
    local = state.get(instance_id) or {}
    if local.get("status") in ("success", "failed"):
        return local["status"]

    try:
        info = vast.show_instance(instance_id)
    except vast.VastError:
        return "failed"

    actual = info.get("actual_status")
    if actual in (None, "loading", "created", "scheduling"):
        return "running"  # snakemake treats "pending" as running for queueing
    if actual == "running":
        host, port = vast.get_ssh_info(info)
        if not host or not port:
            return "running"
        # Has the run script written its exit code?
        rc, out, _ = ssh.exec_remote(
            host, port,
            f"cat {REMOTE_EXIT_CODE} 2>/dev/null",
            stream=False, capture=True,
        )
        if rc == 0 and out.strip().isdigit():
            code = int(out.strip())
            new_status = "success" if code == 0 else "failed"
            state.update(instance_id, status=new_status, exit_code=code)
            return new_status
        return "running"
    if actual in ("exited", "offline"):
        # Container exited. If we have an exit-code marker, trust it; otherwise failed.
        state.update(instance_id, status="failed")
        return "failed"
    return "running"


def stream_logs(instance_id: int, follow: bool = False, ssh_key: str | None = None) -> int:
    info = vast.show_instance(instance_id)
    host, port = vast.get_ssh_info(info)
    if not host or not port:
        raise RuntimeError("no SSH info available for this instance")
    cmd = f"tail -n 1000 {'-f' if follow else ''} {REMOTE_LOG}".strip()
    rc, _, _ = ssh.exec_remote(host, port, cmd, key=ssh_key, stream=True)
    return rc


def resync(instance_id: int, workdir: str | Path, ssh_key: str | None = None) -> None:
    info = vast.show_instance(instance_id)
    host, port = vast.get_ssh_info(info)
    if not host or not port:
        raise RuntimeError("no SSH info available")
    _sync_workdir(host, port, Path(workdir).resolve(), ssh_key=ssh_key)


def ssh_into(instance_id: int, ssh_key: str | None = None) -> int:
    info = vast.show_instance(instance_id)
    host, port = vast.get_ssh_info(info)
    if not host or not port:
        raise RuntimeError("no SSH info available")
    return ssh.interactive_ssh(host, port, key=ssh_key)


def destroy(instance_id: int) -> None:
    vast.destroy_instance(instance_id)
    state.remove(instance_id)


def stop(instance_id: int) -> None:
    vast.stop_instance(instance_id)
    state.update(instance_id, status="stopped")


# ---------------------------------------------------------------------------
# local mode — run in /tmp for testing
# ---------------------------------------------------------------------------

def launch_local(job: Job, *, workdir_override: str | None = None) -> int:
    """Run a job locally under /tmp for testing.

    Syncs workdir, writes envrc + run script, executes in a temp workspace.
    Returns the process exit code.
    """
    job_id = coolname.generate_slug()
    local_workspace = Path(tempfile.mkdtemp(prefix="vastlaunch-local-"))
    log(f"local mode: workspace {local_workspace}")
    log(f"job ID: {job_id}")

    # Write .envrc
    envrc = local_workspace / ".envrc"
    lines = []
    for k, v in job.envs.items():
        if v == "":
            continue
        lines.append(f"export {k}={shlex.quote(str(v))}")
    for k, v in job.secrets.items():
        lines.append(f"export {k}={shlex.quote(str(v))}")
    lines.append(f"export VASTLAUNCH_JOB={shlex.quote(job.name)}")
    lines.append(f"export VASTLAUNCH_JOB_ID={shlex.quote(job_id)}")
    envrc.write_text("\n".join(lines) + "\n")
    envrc.chmod(0o600)

    # Sync workdir
    workdir = Path(workdir_override or job.workdir or ".").resolve() if (workdir_override or job.workdir) else None
    if workdir is not None and workdir.exists():
        log(f"syncing {workdir} -> {local_workspace}/")
        excludes = list(ssh.DEFAULT_EXCLUDES)
        ignore = workdir / ".vastignore"
        if ignore.exists():
            excludes.extend(
                line.strip() for line in ignore.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            )
        exclude_args = []
        for pat in excludes:
            exclude_args.extend(["--exclude", pat])
        rc = subprocess.call(
            ["rsync", "-a", f"{workdir}/", f"{local_workspace}/"] + exclude_args
        )
        if rc != 0:
            raise RuntimeError(f"local rsync failed (rc={rc})")

    # Write run script
    script = _run_script(job)
    # Rewrite the script to use our local workspace instead of /workspace
    script = script.replace(REMOTE_WORKDIR, str(local_workspace))
    run_script = local_workspace / ".vastlaunch_run.sh"
    run_script.write_text(script)
    run_script.chmod(0o755)

    # Run in a clean environment to simulate a fresh container.
    # Only pass through essential system vars; job vars come from .envrc.
    clean_env = {}
    for var in ("PATH", "HOME", "USER", "TERM", "SHELL", "LANG"):
        if var in os.environ:
            clean_env[var] = os.environ[var]

    log("running job locally (clean environment)...")
    rc = subprocess.call(["bash", str(run_script)], env=clean_env)

    if rc == 0:
        log("local job completed successfully")
    else:
        log(f"local job failed with exit code {rc}")

    log(f"workspace preserved at {local_workspace}")
    return rc
