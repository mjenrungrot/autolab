## FILE LENGTH BUDGET
- Apply limits from `.autolab/experiment_file_line_limits.yaml` and the active policy.
- Enforce line, character, and byte budgets for configured files.
- For run manifests, enforce dynamic cap settings under `line_limits.run_manifest_dynamic`.
- `min_cap_lines`/`max_cap_lines` are cap bounds, not required minimum output length.
- Dynamic cap item counts come from configured `count_paths`; each counted item increases cap by `per_item_lines` up to `max_cap_lines`.
- Pattern keys like `runs/{run_id}/metrics.json` are path templates in policy files, not literal filesystem paths. Replace `{run_id}` with the actual run identifier when resolving.
