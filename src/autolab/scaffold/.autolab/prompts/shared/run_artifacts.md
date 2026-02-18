## RUN ARTIFACT REFERENCE

Standard run artifact paths and their expected contents:

- `artifact`: Run manifest; `path`: `{{iteration_path}}/runs/{{run_id}}/run_manifest.json`; `key_fields`: `run_id`, `iteration_id`, `host_mode`, `command`, `resource_request`, `timestamps`, `artifact_sync_to_local`; `producing_stage`: launch.
- `artifact`: Metrics; `path`: `{{iteration_path}}/runs/{{run_id}}/metrics.json`; `key_fields`: `iteration_id`, `run_id`, `status`, `primary_metric.{name,value,delta_vs_baseline}`; `producing_stage`: extract_results.
- `artifact`: Analysis summary; `path`: `{{iteration_path}}/analysis/summary.md`; `key_fields`: Interpretation, caveats, missing evidence accounting; `producing_stage`: extract_results.
- `artifact`: Review result; `path`: `{{iteration_path}}/review_result.json`; `key_fields`: `status`, `blocking_findings`, `required_checks`; `producing_stage`: implementation_review.
- `artifact`: Decision result; `path`: `{{iteration_path}}/decision_result.json`; `key_fields`: `decision`, `rationale`, `evidence[]`, `risks[]`; `producing_stage`: decide_repeat.

When referencing run artifacts, always use the concrete `run_id` from state -- never use literal `{{run_id}}` or `<run_id>` in output files.
