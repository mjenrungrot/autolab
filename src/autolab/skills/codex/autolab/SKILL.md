## name: autolab

## description: Plan, run, and troubleshoot Autolab stage workflows with the right runtime mode, policy settings, and guardrails.

## metadata:

## short-description: Autolab Workflow Operator

# /autolab - Autolab Workflow Operator

Use this skill when the user wants to operate or troubleshoot an Autolab workflow.

## Quick Triage (Top 3 Commands)

1. `autolab status`
1. `autolab verify --stage <stage>`
1. `autolab run`

## Decision Tree

- If verification fails: fix artifacts/verifier contracts first, then rerun `autolab verify --stage <stage>`.
- If stage does not advance but verification passes: inspect `review_result.json` / decision gates for that stage.
- If repeated retries or escalations: inspect guardrails and retry policy in `.autolab/verifier_policy.yaml`.
- If SLURM is involved: validate `run_manifest.json` + `docs/slurm_job_list.md` before retrying orchestration.

## Table Of Contents

- Goal
- Stage flow and contracts
- Command resolution
- Read-first context checklist
- Mode and cadence selection
- Common tasks
- Failure playbooks
- Safe defaults

## Goal

Help the user execute Autolab safely and efficiently by:

- choosing the right runtime mode (`standard` vs `assistant`)
- selecting run cadence (`run` vs `loop --auto`)
- applying policy knobs in `.autolab/verifier_policy.yaml`
- diagnosing no-transition / retry / escalation outcomes

See also: `docs/workflow_modes.md` for mode boundaries.

## Stage Flow And Contracts

`hypothesis -> design -> implementation -> implementation_review -> launch -> slurm_monitor -> extract_results -> update_docs -> decide_repeat`

Verifier categories below are registry capabilities; policy controls actual requirements. Use `autolab explain <stage>` for effective requirements.

- `hypothesis`: `hypothesis.md`; categories `schema`, `prompt_lint`
- `design`: `design.yaml`; categories `schema`, `prompt_lint`
- `implementation`: `implementation_plan.md`; categories `dry_run`, `schema`, `prompt_lint`
- `implementation_review`: `implementation_review.md`, `review_result.json`; categories `dry_run`, `schema`, `prompt_lint`, `consistency`, `env_smoke`, `docs_target_update`
- `launch`: launch script + `runs/<run_id>/run_manifest.json`; categories `schema`, `prompt_lint`, `consistency`, `env_smoke`
- `slurm_monitor`: `runs/<run_id>/run_manifest.json` (+ ledger for SLURM); categories `env_smoke`
- `extract_results`: `runs/<run_id>/metrics.json`, `analysis/summary.md`; categories `schema`, `prompt_lint`, `consistency`, `env_smoke`
- `update_docs`: `docs_update.md`; categories `schema`, `prompt_lint`, `consistency`, `docs_target_update`
- `decide_repeat`: `decision_result.json`; categories `schema`, `prompt_lint`, `consistency`

## Command Resolution

Use this order:

1. `autolab ...`
1. `python -m autolab ...`
1. `PYTHONPATH=src python -m autolab ...`

## Read-First Context Checklist

Inspect before recommending changes:

- `.autolab/state.json`
- `.autolab/backlog.yaml`
- `.autolab/verifier_policy.yaml`
- `docs/todo.md`
- stage artifacts under `experiments/<type>/<iteration_id>/`

Prefer `autolab status` first.

## Mode And Cadence Selection

### Standard mode

Use for deterministic stage-machine control and verifier/debug loops.

- one-step: `autolab run`
- with pre-verify: `autolab run --verify`

### Assistant mode

Use for task-driven cycles (`select -> implement -> verify -> review`).

- one-step: `autolab run --assistant`
- unattended: `autolab loop --assistant --auto --max-hours <h> --max-iterations <n>`

### Decision handling

At `decide_repeat`:

- explicit: `autolab run --decision hypothesis|design|stop|human_review`
- auto: `autolab run --auto-decision`

## Common Tasks

### Stuck stage

1. `autolab status`
1. `autolab verify --stage <stage>`
1. Fix the failing artifact(s) or policy mismatch.
1. Re-run `autolab run`.

### SLURM issues

1. Inspect manifest: `cat experiments/<type>/<iteration_id>/runs/<run_id>/run_manifest.json`
1. Verify ledger: `autolab slurm-job-list verify --manifest <manifest> --doc docs/slurm_job_list.md`
1. Repair ledger if needed: `autolab slurm-job-list append --manifest <manifest> --doc docs/slurm_job_list.md`
1. Re-run `autolab verify --stage launch` or `autolab verify --stage slurm_monitor`.

### Assistant loop issues

1. Check `assistant_mode`, `task_cycle_stage`, and guardrail counters via `autolab status`.
1. Confirm meaningful-change policy (`autorun.meaningful_change.*`).
1. Ensure backlog/todo tasks are actionable.
1. If repeated churn occurs, reduce automation scope and escalate to `human_review`.

### Policy misconfiguration

1. Run `autolab configure --check`.
1. Inspect `requirements_by_stage` vs workflow capabilities.
1. Validate `python_bin`, `dry_run_command`, and retry policies.
1. Apply minimal corrective edits; rerun verification.

## Failure Playbooks

### `prompt_lint` fails

1. `autolab verify --stage <stage>`
1. Inspect `.autolab/prompts/stage_<stage>.md` and `.autolab/workflow.yaml` token contracts.
1. Fix unsupported/missing tokens and rerun verification.

### `schema_checks` fails

1. `autolab verify --stage <stage>`
1. `python .autolab/verifiers/schema_checks.py --stage <stage> --json`
1. Fix required fields/types against `.autolab/schemas/*.schema.json`.

### `docs_targets` fails

Ensure `docs_update.md` includes:

- exact `metrics.json` and `run_manifest.json` artifact paths
- primary metric name, value, and delta
- explicit no-target rationale when `paper_targets` is empty

## Safe Defaults

- Do not manually edit `.autolab/state.json` to force transitions.
- Keep `docs/todo.md` in Markdown format.
- Keep guardrails enabled in unattended mode.
- Prefer bounded loops (`--max-iterations`, `--max-hours`).
- Use `auto_commit.mode: meaningful_only` unless explicitly overridden.

## Review Result Contract

`review_result.json.required_checks` is the fixed 5-key map:

- `tests`
- `dry_run`
- `schema`
- `env_smoke`
- `docs_target_update`

Do not add extra keys; use `.autolab/verification_result.json` for additional verifier evidence.
