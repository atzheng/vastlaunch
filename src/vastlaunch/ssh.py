"""SSH and rsync helpers."""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path


_SSH_BASE = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=/dev/null",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=10",
    "-o", "LogLevel=ERROR",
]


def _ssh_cmd(host: str, port: int, user: str = "root",
             key: str | None = None, connect_timeout: int = 10) -> list[str]:
    cmd = ["ssh", "-p", str(port), "-o", f"ConnectTimeout={connect_timeout}", *_SSH_BASE]
    if key:
        cmd.extend(["-i", key])
    cmd.append(f"{user}@{host}")
    return cmd


def wait_for_ssh(host: str, port: int, *, user: str = "root", key: str | None = None,
                 timeout: int = 300, interval: int = 8) -> None:
    """Block until SSH responds, or raise TimeoutError."""
    deadline = time.time() + timeout
    last_err = ""
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        cmd = _ssh_cmd(host, port, user=user, key=key, connect_timeout=8) + ["echo ok"]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0 and "ok" in proc.stdout:
            return
        last_err = proc.stderr.strip().splitlines()[-1] if proc.stderr else ""
        time.sleep(interval)
    raise TimeoutError(
        f"SSH {host}:{port} not ready after {timeout}s ({attempts} attempts). "
        f"Last error: {last_err}"
    )


def exec_remote(host: str, port: int, command: str, *,
                user: str = "root", key: str | None = None,
                stream: bool = True, capture: bool = False) -> tuple[int, str, str]:
    """Run a command remotely. Returns (returncode, stdout, stderr).
    When stream=True, stdout/stderr go to the local terminal and the returned
    strings are empty."""
    cmd = _ssh_cmd(host, port, user=user, key=key) + [command]
    if stream and not capture:
        rc = subprocess.call(cmd)
        return rc, "", ""
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, proc.stdout, proc.stderr


def rsync_to(host: str, port: int, src: str | Path, dst: str, *,
             user: str = "root", key: str | None = None,
             excludes: list[str] | None = None,
             delete: bool = False) -> int:
    """rsync local -> remote. Returns rsync exit code."""
    if shutil.which("rsync") is None:
        raise RuntimeError("rsync not found on PATH (install rsync locally)")
    ssh_e = "ssh -p {port} {opts}".format(
        port=port,
        opts=" ".join(_SSH_BASE),
    )
    if key:
        ssh_e += f" -i {key}"
    cmd = ["rsync", "-az", "--info=progress2", "-e", ssh_e]
    if delete:
        cmd.append("--delete")
    for pat in excludes or []:
        cmd.extend(["--exclude", pat])
    cmd.extend([str(src), f"{user}@{host}:{dst}"])
    return subprocess.call(cmd)


def rsync_from(host: str, port: int, src: str, dst: str | Path, *,
               user: str = "root", key: str | None = None) -> int:
    """rsync remote -> local."""
    if shutil.which("rsync") is None:
        raise RuntimeError("rsync not found on PATH")
    ssh_e = "ssh -p {port} {opts}".format(port=port, opts=" ".join(_SSH_BASE))
    if key:
        ssh_e += f" -i {key}"
    cmd = ["rsync", "-az", "--info=progress2", "-e", ssh_e,
           f"{user}@{host}:{src}", str(dst)]
    return subprocess.call(cmd)


def interactive_ssh(host: str, port: int, *, user: str = "root",
                    key: str | None = None) -> int:
    """Drop user into an interactive SSH shell."""
    cmd = _ssh_cmd(host, port, user=user, key=key, connect_timeout=10)
    # Replace the silent "echo ok" with no command -> interactive
    return subprocess.call(cmd)


DEFAULT_EXCLUDES = [
    ".git/",
    "__pycache__/",
    "*.pyc",
    ".venv/",
    "venv/",
    "node_modules/",
    ".pytest_cache/",
    ".mypy_cache/",
    "wandb/",
    ".DS_Store",
    "*.egg-info/",
    "outputs/",
    ".vastlaunch/",
]
