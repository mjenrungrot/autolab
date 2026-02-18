## FILE LENGTH BUDGET
- Apply limits from `.autolab/experiment_file_line_limits.yaml` and the active policy.
- Enforce line, character, and byte budgets for configured files.
- For run manifests, enforce dynamic cap settings under `line_limits.run_manifest_dynamic`.
- `min_cap_lines`/`max_cap_lines` are cap bounds, not required minimum output length.
