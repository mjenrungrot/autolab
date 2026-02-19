## RUN ARTIFACT REFERENCE

Standard run artifact paths and their expected contents:

- **Run manifest**
  - `path`: `{{iteration_path}}/runs/{{run_id}}/run_manifest.json`
  - `key_fields`: `run_id`, `iteration_id`, `host_mode`, `command`, `resource_request`, `timestamps`, `artifact_sync_to_local`
  - `producing_stage`: `launch`
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

## SLURM ASYNC NOTE

- In SLURM mode, `launch` may represent either:
  - **submitted/in-progress**: job submitted and tracking metadata captured
  - **completed**: job finished and artifact sync completed
- `launch` should always update `docs/slurm_job_list.md` so downstream stages have a durable run/job ledger.
- `extract_results` should use `docs/slurm_job_list.md` plus scheduler probes (`squeue`, `sinfo`) to decide whether to wait or extract.
- `extract_results` should only compute metrics from artifacts that are already synced locally.

When referencing run artifacts, always use the concrete `run_id` from state -- never use literal `{{run_id}}` or `<run_id>` in output files.
