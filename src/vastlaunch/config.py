"""Job configuration: YAML schema, env expansion, CLI overrides."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class Resources:
    accelerators: str = "RTX_4090:1"  # "GPU_NAME:COUNT"
    cpus: Optional[str] = None         # e.g. "8" or "8+"
    memory: Optional[str] = None       # GB, e.g. "32" or "32+"
    disk_size: int = 60                # GB
    cuda_version: str = "12.0+"        # min CUDA driver version
    reliability: float = 0.98          # min host reliability (0-1)
    inet_down: int = 200               # min Mbps download
    use_spot: bool = False             # interruptible
    max_price: Optional[float] = None  # ceiling on $/hr
    region: Optional[str] = None       # vast geolocation filter, e.g. "[US,CA]"


@dataclass
class Job:
    name: str = "vastlaunch-job"
    resources: Resources = field(default_factory=Resources)
    image: str = "pytorch/pytorch:2.4.0-cuda12.4-cudnn9-devel"
    workdir: Optional[str] = "."       # local dir to rsync; None to skip
    envs: dict = field(default_factory=dict)
    setup: str = ""
    run: str = ""
    auto_destroy: bool = True


def parse_accelerator(s: str) -> tuple[str, int]:
    """Parse 'A100:2' -> ('A100', 2). 'RTX_4090' -> ('RTX_4090', 1)."""
    if ":" in s:
        gpu, n = s.split(":", 1)
        return gpu.strip(), int(n)
    return s.strip(), 1


def expand_envs(envs: dict) -> dict:
    """Expand $VAR and ${VAR} from local environment in string values."""
    out = {}
    for k, v in envs.items():
        if isinstance(v, str):
            v = os.path.expandvars(v)
            if v.startswith("$"):
                # Unresolved: leave empty rather than literal $FOO
                v = ""
        out[k] = v
    return out


def load(path: str | Path) -> Job:
    """Load a job YAML file."""
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    res_data = data.pop("resources", {}) or {}
    resources = Resources(**res_data)
    job = Job(resources=resources, **data)
    job.envs = expand_envs(job.envs)
    return job


def apply_overrides(job: Job, overrides: dict[str, Any]) -> Job:
    """Apply CLI overrides like --gpu, --disk, --image to a loaded job."""
    res = job.resources
    res_updates: dict[str, Any] = {}
    job_updates: dict[str, Any] = {}

    if v := overrides.get("gpu"):
        res_updates["accelerators"] = v
    if (v := overrides.get("disk")) is not None:
        res_updates["disk_size"] = int(v)
    if (v := overrides.get("max_price")) is not None:
        res_updates["max_price"] = float(v)
    if overrides.get("spot"):
        res_updates["use_spot"] = True
    if v := overrides.get("region"):
        res_updates["region"] = v
    if v := overrides.get("image"):
        job_updates["image"] = v
    if v := overrides.get("name"):
        job_updates["name"] = v
    if (v := overrides.get("no_auto_destroy")) is not None:
        job_updates["auto_destroy"] = not v

    if res_updates:
        job_updates["resources"] = replace(res, **res_updates)
    return replace(job, **job_updates) if job_updates else job


def empty_job() -> Job:
    """A default job for cases where no YAML is provided (e.g. submit-script)."""
    return Job()
