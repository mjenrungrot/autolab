## RUN ARTIFACT REFERENCE

Standard run artifact paths and their expected contents:

- **Run manifest**
  - `path`: `{{iteration_path}}/runs/{{run_id}}/run_manifest.json`
  - `key_fields`: `run_id`, `iteration_id`, `host_mode`, `command`, `resource_request`, `timestamps`, `artifact_sync_to_local`
  - `producing_stage`: `launch` (create) + `slurm_monitor` (status/sync updates)
- **SLURM ledger**
  - `path`: `docs/slurm_job_list.md`
  - `key_fields`: one entry per `run_id` with scheduler job linkage (`job_id`) and current state
  - `producing_stage`: `launch` (append initial entry) + `slurm_monitor` (status updates)
- **Metrics**
  - `path`: `{{iteration_path}}/runs/{{run_id}}/metrics.json`
  - `key_fields`: `iteration_id`, `run_id`, `status`, `primary_metric.{name,value,delta_vs_baseline}`
  - `producing_stage`: `extract_results`
- **Analysis summary**
  - `path`: `{{iteration_path}}/analysis/summary.md`
  - `key_fields`: interpretation, caveats, missing-evidence accounting
  - `producing_stage`: `extract_results`
- **Review result**
  - `path`: `{{iteration_path}}/review_result.json`
  - `key_fields`: `status`, `blocking_findings`, `required_checks`
  - `producing_stage`: `implementation_review`
- **Decision result**
  - `path`: `{{iteration_path}}/decision_result.json`
  - `key_fields`: `decision`, `rationale`, `evidence[]`, `risks[]`
  - `producing_stage`: `decide_repeat`

## SLURM STAGE GRAPH

- Local runs:
  - `launch -> slurm_monitor (auto-skip) -> extract_results`
- SLURM runs:
  - `launch` submits and records initial tracking artifacts
  - `slurm_monitor` polls scheduler, syncs artifacts, updates manifest + ledger
  - `extract_results` consumes local artifacts; if required evidence is missing, it emits `partial` or `failed` with explicit missing-evidence accounting

## SLURM LEDGER OWNERSHIP CONTRACT

- `launch`: create/append `docs/slurm_job_list.md` entry at submission time.
- `slurm_monitor`: update ledger status and manifest sync markers over time.
- `extract_results`: read-only consumer of ledger/manifest state (no polling ownership).

When referencing run artifacts, always use the concrete `run_id` from state -- never use literal `{{run_id}}` or `<run_id>` in output files.
