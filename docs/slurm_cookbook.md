# SLURM Cookbook

Consolidated guide for running Autolab experiments on SLURM clusters.

## SLURM Detection

Autolab detects SLURM availability by checking:
1. `design.yaml` `compute.location` value (`"slurm"`)
2. Environment probes (`squeue`, `sinfo`, `sbatch` availability)

When `compute.location: slurm`, the launch stage produces `run_slurm.sbatch` instead of `run_local.sh`.

## Policy Configuration

Apply the SLURM preset to configure verification and runner settings:

```bash
autolab policy apply preset slurm
```

Or manually set in `.autolab/verifier_policy.yaml`:

```yaml
# Customize the dry-run command for SLURM environments
dry_run_command: "{{python_bin}} -m myproject.dry_run --config path/to/config.yaml"
```

Key policy knobs for SLURM:
- `requirements_by_stage.launch.env_smoke: true` -- validates run health after submission
- `agent_runner.edit_scope.mode: "iteration_plus_core"` -- allows runner to update SLURM scripts
- `autorun.todo_fallback.slurm.stage: "hypothesis"` -- fallback behavior when no tasks remain

## Launch Lifecycle

SLURM launches follow an async lifecycle:

```
submitted -> running -> synced -> completed
                    \-> failed
```

**Status transitions** (in `run_manifest.json`):

- `submitted`: Job accepted by scheduler. `completed_at` not required.
- `running`: Job executing on compute nodes. `completed_at` not required.
- `synced`: Remote artifacts copied to local. `completed_at` not required.
- `completed`: Run finished, artifacts available. `completed_at` required.
- `failed`: Run terminated with error. `completed_at` required.

The launch stage sets the initial status (typically `submitted` for SLURM) and does not block waiting for completion. The `extract_results` stage handles async pickup.

## SLURM Job Ledger

All SLURM launches are tracked in `docs/slurm_job_list.md`:

```bash
# Append a new entry
autolab slurm-job-list append \
  --manifest experiments/plan/iter1/runs/20260218T160045Z/run_manifest.json \
  --doc docs/slurm_job_list.md

# Verify an entry exists
autolab slurm-job-list verify \
  --manifest experiments/plan/iter1/runs/20260218T160045Z/run_manifest.json \
  --doc docs/slurm_job_list.md
```

The ledger contains one entry per run with `run_id`, `job_id`, and status.

## Async Extraction Contract

When `extract_results` encounters an in-progress SLURM manifest:

1. Read `docs/slurm_job_list.md` entry for the run
2. Query scheduler state (`squeue -u $USER`, `sinfo`)
3. If job completed and artifacts synced -> proceed with extraction
4. If job still running -> produce `status: partial` with explicit waiting note
5. If sync never reaches success-like state -> produce `status: failed`

The `artifact_sync_to_local.status` field must be success-like (`ok`, `completed`, `success`, `passed`) before full metric extraction.

## Common Failure Recovery

### Job submission fails
```bash
# Check SLURM scheduler
squeue -u $USER
sinfo

# Fix the sbatch script and re-run
autolab run
```

### Artifacts not synced
```bash
# Manually sync artifacts
rsync -avz remote:path/to/outputs local/path/

# Update run_manifest.json artifact_sync_to_local.status to "ok"
# Then re-run extraction
autolab run
```

### Ledger missing entry
```bash
autolab slurm-job-list append \
  --manifest <path-to-manifest> \
  --doc docs/slurm_job_list.md
```

### Verifier fails on SLURM run health
```bash
# Inspect the specific failure
autolab verify --stage launch

# Common fixes:
# - Ensure job_id is present in manifest
# - Ensure docs/slurm_job_list.md exists with run entry
# - Ensure artifact_sync_to_local.status is set
```

### Stuck at extract_results waiting for SLURM
```bash
# Check job status
squeue -j <job_id>

# If completed, ensure local artifacts exist and sync status is updated
# If failed, update manifest status to "failed" and re-run extract
autolab run
```
