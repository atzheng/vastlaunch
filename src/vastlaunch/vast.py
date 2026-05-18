"""Thin wrapper around the `vastai` CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from vastlaunch.config import Resources, parse_accelerator


class VastError(RuntimeError):
    pass


def _check_cli() -> None:
    if shutil.which("vastai") is None:
        raise VastError(
            "`vastai` not found on PATH. Install with `pip install vastai` and "
            "authenticate with `vastai set api-key <KEY>`."
        )


def _run(args: list[str], check: bool = True, timeout: int = 60) -> str:
    _check_cli()
    try:
        proc = subprocess.run(
            ["vastai", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise VastError(f"vastai {' '.join(args)} timed out after {timeout}s")
    if check and proc.returncode != 0:
        raise VastError(
            f"vastai {' '.join(args)} failed (rc={proc.returncode}):\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc.stdout


def _parse_json(out: str) -> Any:
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError as e:
        raise VastError(f"could not parse vastai JSON output: {e}\nraw: {out[:500]}")


# ---- query construction ----------------------------------------------------

def build_query(r: Resources) -> str:
    """Translate Resources -> vast.ai search-offers query string."""
    gpu_name, gpu_count = parse_accelerator(r.accelerators)
    parts = [
        f"gpu_name={gpu_name}",
        f"num_gpus={gpu_count}",
        f"reliability>{r.reliability}",
        f"inet_down>{r.inet_down}",
        f"disk_space>={r.disk_size}",
        "rentable=true",
        "rented=false",
    ]
    if r.cuda_version:
        v = r.cuda_version.rstrip("+")
        parts.append(f"cuda_max_good>={v}")
    if r.cpus:
        v = r.cpus.rstrip("+")
        parts.append(f"cpu_cores>={v}")
    if r.memory:
        v = r.memory.rstrip("+")
        # vast reports cpu_ram in GB
        parts.append(f"cpu_ram>={v}")
    if r.region:
        parts.append(f"geolocation in {r.region}")
    return " ".join(parts)


# ---- operations ------------------------------------------------------------

def search_offers(query: str, limit: int = 20) -> list[dict]:
    out = _run(["search", "offers", query, "--raw", "-o", "dph+", "--limit", str(limit)])
    data = _parse_json(out)
    return data if isinstance(data, list) else []


def create_instance(
    offer_id: int | str,
    image: str,
    disk_gb: int,
    onstart_cmd: str | None = None,
    label: str | None = None,
    use_spot: bool = False,
    bid_price: float | None = None,
) -> int:
    """Create an instance; return the contract/instance ID."""
    args = [
        "create", "instance", str(offer_id),
        "--image", image,
        "--disk", str(disk_gb),
        "--ssh",
        "--direct",
        "--raw",
    ]
    if onstart_cmd:
        args.extend(["--onstart-cmd", onstart_cmd])
    if label:
        args.extend(["--label", label])
    if use_spot:
        # vast uses --bid_price for interruptible; require a bid
        if bid_price is None:
            raise VastError("use_spot=True requires bid_price (max $/hr)")
        args.extend(["--bid_price", str(bid_price)])

    out = _run(args)
    data = _parse_json(out)
    if not isinstance(data, dict):
        raise VastError(f"create instance failed: {data}")
    instance_id = data.get("new_contract") or data.get("id")
    if not data.get("success", False):
        # vast sometimes returns success=False with a contract ID (partial creation).
        # Return the ID so the caller can destroy it; status polling will surface the error.
        if instance_id is not None:
            return int(instance_id)
        raise VastError(f"create instance failed (no contract id): {data}")
    if instance_id is None:
        raise VastError(f"no instance id in create response: {data}")
    return int(instance_id)


def show_instance(instance_id: int | str) -> dict:
    out = _run(["show", "instance", str(instance_id), "--raw"])
    data = _parse_json(out)
    if not isinstance(data, dict):
        raise VastError(f"unexpected show-instance response: {data}")
    return data


def list_instances() -> list[dict]:
    out = _run(["show", "instances", "--raw"])
    data = _parse_json(out)
    return data if isinstance(data, list) else []


def destroy_instance(instance_id: int | str) -> None:
    _run(["destroy", "instance", str(instance_id), "-y"], check=False)


def stop_instance(instance_id: int | str) -> None:
    _run(["stop", "instance", str(instance_id), "-y"], check=False)


def get_ssh_info(info: dict) -> tuple[str | None, int | None]:
    """Extract (host, port) for SSH. Tries direct first, then proxy."""
    # Direct mode: public_ipaddr + port mapping for container's port 22
    ports = info.get("ports") or {}
    host = info.get("public_ipaddr")
    port = None
    if ports and host:
        mapping = ports.get("22/tcp")
        if mapping and isinstance(mapping, list) and mapping:
            try:
                port = int(mapping[0].get("HostPort"))
            except (TypeError, ValueError):
                port = None
    # Fallback: proxy SSH
    if port is None:
        host = info.get("ssh_host") or host
        port = info.get("ssh_port")
    if port is not None:
        port = int(port)
    return host, port
