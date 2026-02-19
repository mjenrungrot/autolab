## STATUS VOCABULARY (single source of truth)

### Run Manifest Status (canonical enum)
Valid values for `run_manifest.json` `status` field:
- `pending` -- run created but not yet submitted
- `submitted` -- job submitted to scheduler (SLURM)
- `running` -- execution in progress
- `synced` -- remote artifacts synchronized to local
- `completed` -- run finished successfully
- `failed` -- run terminated with error
- `partial` -- run produced incomplete results

### Success-Like Statuses
Use when checking artifact sync, run completion, or verifier outcomes:
`completed`, `ok`, `success`, `passed`

### Completion-Like Statuses (triggers `completed_at` requirement)
When `run_manifest.status` is one of these, `timestamps.completed_at` is required:
`completed`, `failed`

### In-Progress Statuses
When `run_manifest.status` is one of these, `timestamps.completed_at` may be omitted:
`pending`, `submitted`, `running`, `synced`

### Metrics Status (metrics.json)
Valid values for `metrics.json` `status` field:
- `completed` -- metrics fully extracted
- `partial` -- only partial evidence available
- `failed` -- extraction could not produce trustworthy metrics

### Review Status (review_result.json)
Valid values for `review_result.json` `status` field:
- `pass` -- launch-ready
- `needs_retry` -- remediation required
- `failed` -- escalation required

### Decision Values (decision_result.json)
Valid values for `decision_result.json` `decision` field:
- `hypothesis` -- restart from hypothesis
- `design` -- iterate without new hypothesis
- `stop` -- terminate workflow
- `human_review` -- escalate to human
