# vastlaunch

One-command GPU jobs on [vast.ai](https://vast.ai). Spiritually a SkyPilot for a
single provider — a YAML describes the job, `vastlaunch launch` provisions the
GPU, syncs your code, runs the command, streams logs back, and tears the
instance down when done.

Designed to be a drop-in cluster backend for Snakemake's `cluster-generic`
executor.

## Install

```bash
git clone <this repo> && cd vastlaunch
pip install -e .

# vast.ai CLI auth (one time)
pip install vastai
vastai set api-key <YOUR_KEY>
```

You also need `rsync` and `ssh` on PATH locally (both standard on Linux/macOS).

## Quickstart

Write a `vastlaunch.yaml` (see `example_job.yaml` for all fields):

```yaml
name: hello
resources:
  accelerators: RTX_4090:1
  disk_size: 60
  max_price: 0.40
image: pytorch/pytorch:2.4.0-cuda12.4-cudnn9-devel
workdir: .
setup: |
  pip install -r requirements.txt
run: |
  python train.py
```

Then:

```bash
vastlaunch launch                   # blocks, streams logs, auto-destroys
vastlaunch search                   # show top matching offers, don't launch
vastlaunch launch --dry-run         # see what would happen
vastlaunch launch --gpu A100:1      # override gpu (also --disk, --max-price, --image, --spot)
```

## Subcommands

| Command | Purpose |
|---|---|
| `launch [CONFIG]` | Provision, sync, run, stream logs, destroy on exit. |
| `submit [CONFIG]` | Same flow but detach after starting. Prints instance ID. |
| `submit-script SCRIPT --config CFG` | Run an arbitrary script as the job. For Snakemake. |
| `status <id>` | Print one of: `pending` / `running` / `success` / `failed`. |
| `logs <id> [-f]` | Tail the remote run log. |
| `sync <id>` | Re-rsync local workdir to the running instance. |
| `ssh <id>` | Interactive shell on the instance. |
| `stop <id> [<id>...]` | Stop (pause) instances. |
| `destroy <id> [<id>...]` | Destroy instances. |
| `list [--json]` | Show managed instances and their state. |
| `search [CONFIG]` | List matching offers without launching. |

State lives in `~/.vastlaunch/` (override with `$VASTLAUNCH_STATE_DIR`).

## Snakemake integration

vastlaunch's `submit-script` / `status` / `destroy` triple plugs straight into
Snakemake's `cluster-generic` executor:

```bash
snakemake \
  --executor cluster-generic \
  --cluster-generic-submit-cmd \
    "vastlaunch submit-script --config vastlaunch.yaml \
     --gpu {resources.gpu_model}:{resources.gpu_count} \
     --disk {resources.disk_gb} {script}" \
  --cluster-generic-status-cmd "vastlaunch status {jobid}" \
  --cluster-generic-cancel-cmd "vastlaunch destroy {jobid}" \
  --jobs 8 --retries 2
```

Rules declare GPU requirements via `resources:` and write outputs to S3 (or any
storage Snakemake can see), since vast instances are ephemeral. See
`Snakefile.example`.

## Optuna integration (hyperparameter sweeps)

Use vastlaunch as the executor inside an Optuna objective function:

```python
import optuna, json, subprocess, tempfile, yaml

def objective(trial):
    cfg = {
        "lr": trial.suggest_float("lr", 1e-5, 1e-2, log=True),
        "batch_size": trial.suggest_categorical("batch_size", [32, 64, 128]),
    }
    # Write a job spec for this trial
    job = {
        "name": f"sweep-trial-{trial.number}",
        "resources": {"accelerators": "RTX_4090:1", "disk_size": 60},
        "image": "myorg/train:v3",
        "workdir": ".",
        "envs": {"WANDB_API_KEY": "$WANDB_API_KEY",
                 "OPTUNA_TRIAL": str(trial.number)},
        "setup": "pip install -r requirements.txt",
        "run": f"python train.py --lr {cfg['lr']} --bs {cfg['batch_size']} "
               f"--out s3://bucket/sweeps/{trial.number}/",
    }
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        yaml.safe_dump(job, f); path = f.name
    # Blocking launch; auto-destroys on completion
    subprocess.run(["vastlaunch", "launch", path], check=True)
    # Fetch the metric written to S3 by train.py
    import boto3
    obj = boto3.client("s3").get_object(Bucket="bucket",
        Key=f"sweeps/{trial.number}/metric.json")
    return json.loads(obj["Body"].read())["val_loss"]

study = optuna.create_study(
    storage="sqlite:///sweep.db",
    sampler=optuna.samplers.TPESampler(),
    pruner=optuna.pruners.HyperbandPruner(),
    load_if_exists=True,
    study_name="sweep1",
)
study.optimize(objective, n_trials=100, n_jobs=8)
```

`n_jobs=8` runs eight trials in parallel; each holds one vast instance.

## How it works

1. `vastai search offers` with a query built from your `resources:` block,
   sorted by `$/hr`. The cheapest matching offer is selected (subject to
   `max_price`).
2. `vastai create instance` with your image and disk size. An onstart script
   installs `rsync`/`tmux` (some images lack them), writes your env vars to
   `/workspace/.envrc`, and signals readiness.
3. Poll `vastai show instance` until the host reports `running`, then
   retry-with-backoff on SSH until the daemon answers.
4. `rsync` your `workdir` to `/workspace/` (default excludes + `.vastignore`).
5. Upload and run `/workspace/.vastlaunch_run.sh` which sources the envrc,
   runs `setup:`, runs `run:`, and writes the exit code to
   `/workspace/.exit_code`. With `submit`, this runs inside a `tmux` session.
6. On `success` (or non-spot `failed`), destroy the instance if
   `auto_destroy: true`. On error during launch/sync, leave the instance up so
   you can SSH in to debug.

## Practical notes

- **Bake your environment into a Docker image.** The biggest single iteration-time
  win. Re-pip-installing on every launch is brutal.
- **Spot/interruptible.** Set `use_spot: true` and `max_price: <bid>`. Combine
  with Snakemake `--retries` and checkpoint to S3 every few minutes — your
  job will resubmit and resume if evicted.
- **First SSH after launch often fails.** vastlaunch retries with backoff for
  up to 5 minutes; the host's SSH daemon takes 10–60s after status flips to
  `running`.
- **Outputs in S3/B2.** Anything not on shared storage disappears when the
  instance is destroyed. Push checkpoints/results during training, not at the
  end.
- **No idle autostop.** vast has no native auto-stop on idle. If you need it,
  set a cron inside the container that destroys the contract, or run a local
  watchdog that polls `vastlaunch status`.
- **`VASTLAUNCH_DEBUG=1`** re-raises exceptions with full tracebacks.

## File layout

```
vastlaunch.yaml      # your job config
.vastignore          # extra rsync excludes
~/.vastlaunch/       # state DB + logs (instance_id -> metadata)
```
