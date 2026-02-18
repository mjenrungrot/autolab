## RUN ARTIFACT REFERENCE

Standard run artifact paths and their expected contents:

| Artifact | Path | Key Fields | Producing Stage |
|----------|------|------------|-----------------|
| Run manifest | `{{iteration_path}}/runs/{{run_id}}/run_manifest.json` | `run_id`, `iteration_id`, `host_mode`, `command`, `resource_request`, `timestamps`, `artifact_sync_to_local` | launch |
| Metrics | `{{iteration_path}}/runs/{{run_id}}/metrics.json` | `iteration_id`, `run_id`, `status`, `primary_metric.{name,value,delta_vs_baseline}` | extract_results |
| Analysis summary | `{{iteration_path}}/analysis/summary.md` | Interpretation, caveats, missing evidence accounting | extract_results |
| Review result | `{{iteration_path}}/review_result.json` | `status`, `blocking_findings`, `required_checks` | implementation_review |
| Decision result | `{{iteration_path}}/decision_result.json` | `decision`, `rationale`, `evidence[]`, `risks[]` | decide_repeat |

When referencing run artifacts, always use the concrete `run_id` from state -- never use literal `{{run_id}}` or `<run_id>` in output files.
