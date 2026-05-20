"""vastlaunch CLI."""

from __future__ import annotations

import argparse
import fnmatch
import io
import json
import os
import sys
import tarfile
from pathlib import Path
from typing import Any

from vastlaunch import client, config, runner, state, vast


def _add_resource_overrides(p: argparse.ArgumentParser) -> None:
    p.add_argument("--gpu", help="Override resources.accelerators, e.g. 'A100:1'")
    p.add_argument("--disk", type=int, help="Override resources.disk_size (GB)")
    p.add_argument("--max-price", type=float, help="Max $/hr")
    p.add_argument("--spot", action="store_true", help="Use interruptible instance")
    p.add_argument("--region", help="Geolocation filter, e.g. '[US,CA]'")
    p.add_argument("--image", help="Override docker image")
    p.add_argument("--name", help="Override job name (used as instance label)")
    p.add_argument("--no-auto-destroy", action="store_true",
                   help="Keep instance after run completes")
    p.add_argument("--ssh-key", help="Path to SSH private key (default: vastai's)")


def _overrides_from_args(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "gpu": args.gpu,
        "disk": args.disk,
        "max_price": args.max_price,
        "spot": args.spot,
        "region": args.region,
        "image": args.image,
        "name": args.name,
        "no_auto_destroy": args.no_auto_destroy,
    }


def _load_job(args: argparse.Namespace, default_config: str | None = None) -> config.Job:
    cfg_path = getattr(args, "config", None) or default_config
    if cfg_path:
        job = config.load(cfg_path)
    elif Path("vastlaunch.yaml").exists():
        job = config.load("vastlaunch.yaml")
    elif Path("job.yaml").exists():
        job = config.load("job.yaml")
    else:
        job = config.empty_job()
    return config.apply_overrides(job, _overrides_from_args(args))


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_launch(args: argparse.Namespace) -> int:
    if client.server_url() and not args.dry_run:
        return _server_submit(args)
    job = _load_job(args, default_config=args.config)
    instance_id = runner.launch(
        job,
        detach=False,
        ssh_key=args.ssh_key,
        dry_run=args.dry_run,
    )
    return 0 if instance_id > 0 or args.dry_run else 1


def cmd_submit(args: argparse.Namespace) -> int:
    """Like launch --detach. Prints job ID to stdout for orchestrators."""
    if client.server_url():
        return _server_submit(args)
    job = _load_job(args, default_config=args.config)
    instance_id = runner.launch(job, detach=True, ssh_key=args.ssh_key)
    print(instance_id)  # stdout for Snakemake
    return 0


def _make_workdir_tarball(workdir: Path) -> bytes:
    """Create a gzipped tar of workdir, honouring DEFAULT_EXCLUDES and .vastignore."""
    from vastlaunch.ssh import DEFAULT_EXCLUDES  # avoid circular at module level

    excludes = list(DEFAULT_EXCLUDES)
    vastignore = workdir / ".vastignore"
    if vastignore.exists():
        excludes.extend(
            line.strip() for line in vastignore.read_text().splitlines()
            if line.strip() and not line.startswith("#")
        )

    def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
        # info.name is relative to the arcname root (e.g. "./src/foo.py")
        rel = info.name.lstrip("./")
        if not rel:
            return info
        parts = Path(rel).parts
        for pat in excludes:
            pat_clean = pat.rstrip("/")
            if any(fnmatch.fnmatch(p, pat_clean) for p in parts):
                return None
            if fnmatch.fnmatch(rel, pat_clean):
                return None
        return info

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        tf.add(str(workdir), arcname=".", filter=_filter)
    return buf.getvalue()


def _server_submit(args: argparse.Namespace) -> int:
    cfg_path = getattr(args, "config", None)
    if cfg_path:
        yaml_text = Path(cfg_path).read_text()
    elif Path("vastlaunch.yaml").exists():
        yaml_text = Path("vastlaunch.yaml").read_text()
    elif Path("job.yaml").exists():
        yaml_text = Path("job.yaml").read_text()
    else:
        print("[vastlaunch] no job config found", file=sys.stderr)
        return 1
    # Resolve secrets client-side so the server doesn't need local env vars.
    # This raises if any required secret is missing from the local environment.
    yaml_text = config.resolve_secrets_in_yaml(yaml_text)
    result = client.submit(yaml_text)
    job_id = result["job_id"]
    print(f"[vastlaunch] submitted: {job_id} ({result['name']})", file=sys.stderr)

    job_cfg = config.load_from_string(yaml_text)
    if job_cfg.workdir:
        workdir = Path(job_cfg.workdir).resolve()
        if workdir.exists():
            print(f"[vastlaunch] uploading workdir {workdir} ...", file=sys.stderr)
            tarball = _make_workdir_tarball(workdir)
            client.upload_workdir(job_id, tarball)
            print(f"[vastlaunch] workdir uploaded ({len(tarball):,} bytes)", file=sys.stderr)

    print(job_id)
    return 0


def cmd_submit_script(args: argparse.Namespace) -> int:
    """Take a script path, run its contents as the job's `run` block.

    Designed for Snakemake's cluster-generic --cluster-generic-submit-cmd.
    Prints the instance ID on stdout (the only thing on stdout).
    """
    script_path = Path(args.script).resolve()
    if not script_path.exists():
        print(f"script not found: {script_path}", file=sys.stderr)
        return 1
    script_content = script_path.read_text()

    job = _load_job(args, default_config=args.config)
    # Replace the run block with the script's contents
    job.run = script_content

    instance_id = runner.launch(job, detach=True, ssh_key=args.ssh_key)
    print(instance_id)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    if client.server_url():
        job_id = client.resolve_job_id(args.instance_id)
        job = client.get_job(job_id)
        print(job.get("status", "?"))
        return 0
    s = runner.status(int(args.instance_id))
    print(s)
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    if client.server_url():
        job_id = client.resolve_job_id(args.instance_id)
        if args.follow:
            return _server_follow_logs(job_id)
        print(client.get_logs(job_id))
        return 0
    return runner.stream_logs(int(args.instance_id), follow=args.follow, ssh_key=args.ssh_key)


def _server_follow_logs(job_id: str) -> int:
    import time
    seen = 0
    job: dict = {}
    while True:
        chunk = client.get_logs(job_id, since=seen)
        if chunk:
            print(chunk, end="", flush=True)
            seen += chunk.count("\n")
        job = client.get_job(job_id)
        if job.get("status") in ("success", "failed", "stopped"):
            # One final fetch to catch any output written right at exit
            chunk = client.get_logs(job_id, since=seen)
            if chunk:
                print(chunk, end="", flush=True)
            break
        time.sleep(10)
    return 0 if job.get("status") == "success" else 1


def cmd_sync(args: argparse.Namespace) -> int:
    workdir = args.workdir or "."
    runner.resync(args.instance_id, workdir, ssh_key=args.ssh_key)
    return 0


def cmd_ssh(args: argparse.Namespace) -> int:
    return runner.ssh_into(args.instance_id, ssh_key=args.ssh_key)


def cmd_destroy(args: argparse.Namespace) -> int:
    for iid in args.instance_ids:
        if client.server_url():
            iid = client.resolve_job_id(iid)
        print(f"destroying {iid}", file=sys.stderr)
        if client.server_url():
            client.destroy_job(iid)
        else:
            runner.destroy(int(iid))
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    for iid in args.instance_ids:
        runner.stop(int(iid))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    if client.server_url():
        jobs = client.list_jobs()
        if args.json:
            print(json.dumps(jobs, indent=2))
            return 0
        if not jobs:
            print("(no jobs)")
            return 0
        print(f"{'JOB ID':<30} {'STATUS':<12} {'NAME'}")
        for job in jobs:
            print(f"{job.get('job_id', '?'):<30} {job.get('status', '?'):<12} {job.get('name', '?')}")
        return 0
    jobs = state.all_jobs()
    if args.json:
        print(json.dumps(jobs, indent=2))
        return 0
    if not jobs:
        print("(no managed instances)")
        return 0
    print(f"{'ID':<10} {'STATUS':<12} {'NAME':<30} {'HOST'}")
    for iid, info in sorted(jobs.items()):
        host = f"{info.get('host', '?')}:{info.get('port', '?')}"
        print(f"{iid:<10} {info.get('status', '?'):<12} {info.get('name', '?'):<30} {host}")
    return 0


def cmd_blacklist(args: argparse.Namespace) -> int:
    if args.action == "clear":
        state.blacklist_clear()
        print("blacklist cleared", file=sys.stderr)
    else:  # list
        ids = state.blacklist_get()
        if not ids:
            print("(blacklist is empty)")
        else:
            for oid in ids:
                print(oid)
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    """Show top matching offers without launching anything."""
    job = _load_job(args, default_config=args.config)
    query = vast.build_query(job.resources)
    print(f"query: {query}\n", file=sys.stderr)
    offers = vast.search_offers(query, limit=args.limit)
    if args.json:
        print(json.dumps(offers, indent=2))
        return 0
    print(f"{'ID':<10} {'$/HR':<8} {'GPU':<14} {'N':<3} {'CUDA':<6} {'REL':<6} {'GEO'}")
    for o in offers[:args.limit]:
        print(
            f"{o.get('id', '?'):<10} "
            f"{o.get('dph_total', 0):<8.4f} "
            f"{o.get('gpu_name', '?'):<14} "
            f"{o.get('num_gpus', '?'):<3} "
            f"{o.get('cuda_max_good', '?'):<6} "
            f"{o.get('reliability2', 0):<6.3f} "
            f"{o.get('geolocation', '?')}"
        )
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="vastlaunch",
        description="One-command GPU jobs on vast.ai.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    # launch ---
    pl = sub.add_parser("launch", help="Launch a job and stream logs to terminal.")
    pl.add_argument("config", nargs="?", help="Path to job YAML (default: ./vastlaunch.yaml)")
    pl.add_argument("--dry-run", action="store_true", help="Show what would happen, don't launch")
    _add_resource_overrides(pl)
    pl.set_defaults(func=cmd_launch)

    # submit ---
    ps = sub.add_parser("submit", help="Launch a job in the background; print instance ID.")
    ps.add_argument("config", nargs="?", help="Path to job YAML")
    _add_resource_overrides(ps)
    ps.set_defaults(func=cmd_submit)

    # submit-script ---
    pss = sub.add_parser(
        "submit-script",
        help="Run a script as the job's run block (Snakemake-compatible).",
    )
    pss.add_argument("script", help="Path to a script to run remotely as the job")
    pss.add_argument("--config", help="Path to job YAML providing resources/image/etc.")
    _add_resource_overrides(pss)
    pss.set_defaults(func=cmd_submit_script)

    # status ---
    pst = sub.add_parser("status", help="Print one of: pending|running|success|failed.")
    pst.add_argument("instance_id", help="Instance ID (direct) or job ID (server mode)")
    pst.set_defaults(func=cmd_status)

    # logs ---
    pll = sub.add_parser("logs", help="Stream remote run log.")
    pll.add_argument("instance_id", help="Instance ID (direct) or job ID (server mode)")
    pll.add_argument("-f", "--follow", action="store_true")
    pll.add_argument("--ssh-key")
    pll.set_defaults(func=cmd_logs)

    # sync ---
    psy = sub.add_parser("sync", help="Re-sync local workdir to remote /workspace.")
    psy.add_argument("instance_id", type=int)
    psy.add_argument("--workdir", help="Local directory (default: .)")
    psy.add_argument("--ssh-key")
    psy.set_defaults(func=cmd_sync)

    # ssh ---
    psh = sub.add_parser("ssh", help="Open an interactive SSH session to the instance.")
    psh.add_argument("instance_id", type=int)
    psh.add_argument("--ssh-key")
    psh.set_defaults(func=cmd_ssh)

    # destroy ---
    pd = sub.add_parser("destroy", help="Destroy one or more instances.")
    pd.add_argument("instance_ids", nargs="+")
    pd.set_defaults(func=cmd_destroy)

    # stop ---
    pp = sub.add_parser("stop", help="Stop (pause) one or more instances.")
    pp.add_argument("instance_ids", nargs="+")
    pp.set_defaults(func=cmd_stop)

    # list ---
    pli = sub.add_parser("list", help="List vastlaunch-managed instances.")
    pli.add_argument("--json", action="store_true")
    pli.set_defaults(func=cmd_list)

    # search ---
    pse = sub.add_parser("search", help="Show matching offers without launching.")
    pse.add_argument("config", nargs="?", help="Path to job YAML")
    pse.add_argument("--limit", type=int, default=10)
    pse.add_argument("--json", action="store_true")
    _add_resource_overrides(pse)
    pse.set_defaults(func=cmd_search)

    # blacklist ---
    pbl = sub.add_parser("blacklist", help="Manage the offer blacklist.")
    pbl.add_argument("action", choices=["list", "clear"], nargs="?", default="list")
    pbl.set_defaults(func=cmd_blacklist)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n[vastlaunch] interrupted", file=sys.stderr)
        return 130
    except vast.VastError as e:
        print(f"[vastlaunch] vast error: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"[vastlaunch] error: {e}", file=sys.stderr)
        if os.environ.get("VASTLAUNCH_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
