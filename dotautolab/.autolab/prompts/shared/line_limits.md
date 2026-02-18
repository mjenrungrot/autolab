## FILE LENGTH BUDGET
- Apply limits from `.autolab/experiment_file_line_limits.yaml` and the active policy.
- Enforce line, character, and byte budgets for configured files.
- For run manifest files, also enforce dynamic run-based caps and max char/byte budgets.
- Verifier failures should rely on `.autolab/experiment_file_line_limits.yaml`.
