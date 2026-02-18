# Autolab shared package

This repository provides the shared `autolab` workflow package used across projects.

## Install

```bash
# Editable (local development)
python -m pip install -e .

# From GitHub (shared usage)
python -m pip install git+https://github.com/mjenrungrot/autolab.git@main

# Pinned release (CI / stable)
python -m pip install git+https://github.com/mjenrungrot/autolab.git@v1.1.8
```

After upgrading from GitHub, refresh local workflow defaults:

```bash
autolab sync-scaffold --force
```

Enable auto version bump on each commit:

```bash
./scripts/install-hooks.sh
```

This also syncs the pinned release tag in `README.md` and keeps only the latest 10
`vX.Y.Z` tags on GitHub (`origin`) after each commit.
These local hooks are optional; CI workflows under `.github/workflows/` are authoritative.

After install, invoke with `autolab --help` or `python -m autolab --help`.

## What it does

Autolab drives a repeatable experiment lifecycle through an eight-stage pipeline:

`hypothesis -> design -> implementation -> implementation_review -> launch -> extract_results -> update_docs -> decide_repeat`

Two terminal stages (`human_review`, `stop`) handle escalation and completion.

**Operating modes**

- **Manual** (`autolab run`) -- single stage transition, ideal for debugging verifiers and manual checkpoints.
- **Agent runner** (`autolab loop --auto`) -- bounded or unattended multi-step execution with guardrails and lock management.
- **Assistant** (`autolab run --assistant`) -- task-driven delivery from backlog (`select -> implement -> verify -> review`).

See `docs/workflow_modes.md` for detailed responsibility contracts per mode.

## Configuration

**Run mode.** `autolab run` executes a single transition; `autolab loop --max-iterations N` runs bounded multi-step; `autolab loop --auto --max-hours H` enables unattended operation. Add `--verify` to run policy-driven verification before evaluation. Use `--decision <stage>` to force a human choice at `decide_repeat`, or `--auto-decision` to let Autolab choose from the backlog. See `docs/workflow_modes.md`.

**Agent runner.** Controlled via `agent_runner` in `.autolab/verifier_policy.yaml`. Runners: `codex` (sandboxed, default preset), `claude` (non-interactive `claude -p`), or `custom` (your own command template). Toggle per-run with `--run-agent` / `--no-run-agent`. Edit scope defaults to `iteration_plus_core`; set `iteration_only` for strict isolation. See `docs/runner_reference.md`.

**Commit and quality gates.** `auto_commit.mode` controls commit behavior (`meaningful_only` default, `always`, `disabled`). `meaningful_change` settings gate implementation progress, verification success, and git-based checks. Override with `--no-strict-implementation-progress` for experiments. See `docs/runner_reference.md`.

**Guardrails.** `autorun.guardrails` caps same-decision streaks, no-progress cycles, update-docs churn, and generated todo count. Breach action defaults to `human_review`. Fallback tasks are configurable per host mode (`local` / `slurm`). See `docs/workflow_modes.md`.

**Policy presets.** Apply bundled policy overlays with:
`autolab policy apply preset <local_dev|ci_strict|slurm>`.

## Source layout

- `src/autolab/` -- Python package modules (`__main__`, `todo_sync`, `slurm_job_list`)
- `src/autolab/scaffold/.autolab/` -- shared default scaffold assets (prompts, schemas, verifier helpers, defaults)
- `.autolab/` in user repos is materialized from scaffold via `autolab init` or `autolab sync-scaffold`

## Stage lifecycle and artifacts

Each stage produces specific artifacts and has defined exit behavior:

- **hypothesis** -- `hypothesis.md`; advances when metric/target/criteria fields are present.
- **design** -- `design.yaml`; advances when required keys are present.
- **implementation** -- `implementation_plan.md` + code changes; advances to review (requires Dry Run section when `dry_run: true`).
- **implementation_review** -- `implementation_review.md`, `review_result.json`; `pass` -> launch, `needs_retry` -> implementation, `failed` -> human_review.
- **launch** -- `launch/run_local.sh` or `run_slurm.sbatch`, `runs/<run_id>/run_manifest.json`; advances to extract_results.
- **extract_results** -- `runs/<run_id>/metrics.json`, `analysis/summary.md`; advances to update_docs.
- **update_docs** -- `docs_update.md`; advances when run evidence references are present.
- **decide_repeat** -- `decision_result.json`; decides next iteration or terminal action.
- assistant audit trail: `.autolab/task_history.jsonl`

**Failure and retry.** Verifier failure increments `state.stage_attempt` and marks `needs_retry` while below `max_stage_attempts`. When the budget is exhausted the workflow escalates to `human_review`. `implementation_review` can explicitly return `pass`, `needs_retry`, or `failed`.

**State ownership.** `.autolab/state.json` is orchestration-owned. Stage agents emit artifacts; Autolab applies transition, retry, and escalation logic. Agents should never manually advance stages by editing state.

## State and backlog contracts

### `.autolab/state.json`

Required fields: `iteration_id`, `stage`, `stage_attempt`, `max_stage_attempts`, `max_total_iterations`.
Optional: `last_run_id`, `sync_status`, `history` (recent transition records with verifier summary and timestamps).

```json
{
  "iteration_id": "e1",
  "stage": "implementation",
  "stage_attempt": 0,
  "max_stage_attempts": 3,
  "max_total_iterations": 20
}
```

### `.autolab/backlog.yaml`

Workflow bootstrap expects `hypotheses` and `experiments` lists with `id`, `status`, `title`/`hypothesis_id`, and `iteration_id` fields. Terminal statuses: `done`, `completed`, `closed`, `resolved`. See `docs/artifact_contracts.md` for the full schema and examples.

## Verifiers

- `template_fill.py` -- placeholder cleanup and artifact budget checks per stage.
- `prompt_lint.py` -- stage prompt structure and token contract enforcement.
- `schema_checks.py` -- JSON Schema validation for stage artifacts, `state.json`, and `backlog.yaml`.
- `registry_consistency.py` -- ensures policy requirements are supported by workflow registry capabilities.
- `consistency_checks.py` -- validates cross-artifact consistency (design/manifest/metrics/review).
- Canonical command: `autolab verify --stage <stage>`.
- Latest result persisted to `.autolab/verification_result.json`.
- Verifier commands are policy-driven; `python_bin` (default `python3`) controls interpreter portability.
- `dry_run_command` should be non-empty when any stage sets `dry_run: true` (scaffold provides a stub).

## Skill install

```bash
autolab install-skill codex
```

Installs to `<project-root>/.codex/skills/autolab/SKILL.md`.

Target a different project:

```bash
autolab install-skill codex --project-root /path/to/project
```

## Scaffold management

Sync scaffold assets into a repo (also useful after upgrading):

```bash
autolab sync-scaffold --force
```

Bootstrap a new workspace and configure policy defaults interactively:

```bash
autolab init --interactive
```

Reset `.autolab/` to packaged defaults and clear workflow state:

```bash
autolab reset
```

Use `--state-file` to target a different state path if needed:

```bash
autolab reset --state-file .autolab/state.json
```

## Further reading

- `docs/workflow_modes.md` -- manual, agent-runner, and assistant mode contracts
- `docs/runner_reference.md` -- agent runner YAML reference and runner presets
- `docs/artifact_contracts.md` -- full artifact schemas, backlog format, and state contract
- `docs/prompt_authoring_guide.md` -- scaffold prompt conventions and stage-prompt wiring
- `docs/quickstart.md` -- getting started walkthrough
- `examples/golden_iteration/` -- complete stage-by-stage artifact example
