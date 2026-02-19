## name: autolab

## description: Operate and troubleshoot Autolab workflows when using Claude as the primary runner.

## metadata:

## short-description: Autolab Workflow Operator (Claude)

# /autolab - Autolab Workflow Operator (Claude)

Use this skill when running Autolab with `agent_runner.runner: claude`.

## Fast Triage

1. `autolab status`
1. `autolab verify --stage <stage>`
1. `autolab run`

## Claude-Specific Defaults

- Keep `agent_runner.runner: claude` in `.autolab/verifier_policy.yaml`.
- Use explicit timeout settings (`agent_runner.timeout_seconds`).
- Use `iteration_plus_core` edit scope for normal work, `iteration_only` for strict isolation.
- Do not enable `claude_dangerously_skip_permissions` unless execution is fully trusted/non-interactive.

## Stage Flow

`hypothesis -> design -> implementation -> implementation_review -> launch -> slurm_monitor -> extract_results -> update_docs -> decide_repeat`

Use `autolab explain <stage>` to inspect active verifier requirements.

## Common Tasks

### Stuck Stage

1. `autolab status`
1. `autolab verify --stage <stage>`
1. Fix artifacts/policy mismatches.
1. `autolab run`

### SLURM Issues

1. Validate `runs/<run_id>/run_manifest.json`.
1. Validate ledger entry with `autolab slurm-job-list verify --manifest <manifest> --doc docs/slurm_job_list.md`.
1. Re-run stage verification.

### Assistant/Automation Loop Issues

1. Inspect guardrail counters in `autolab status`.
1. Check `autorun.guardrails` and `autorun.meaningful_change` policy blocks.
1. Escalate to `human_review` if retries churn without progress.

### Policy Misconfiguration

1. Run `autolab configure --check`.
1. Verify `python_bin`, `dry_run_command`, and `requirements_by_stage`.
1. Keep requirements as a subset of stage capabilities in `.autolab/workflow.yaml`.

## Safe Defaults

- Never force stage transitions by editing `.autolab/state.json`.
- Keep `docs/todo.md` as Markdown.
- Keep guardrails enabled for unattended runs.
- Prefer bounded loops (`--max-iterations`, `--max-hours`).
