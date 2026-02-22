# Autolab shared package

This repository provides the shared `autolab` workflow package used across projects.

## Install

```bash
# Editable (local development)
python -m pip install -e .

# From GitHub (shared usage)
python -m pip install git+https://github.com/mjenrungrot/autolab.git@main

# Pinned release (CI / stable)
python -m pip install git+https://github.com/mjenrungrot/autolab.git@v1.1.46
```

Upgrade to the latest stable GitHub tag in one step:

```bash
autolab update
```

`autolab update` compares your installed version with the latest `vX.Y.Z` release
tag, upgrades via pip when needed, and then runs `autolab sync-scaffold --force`
automatically when you are inside an Autolab repo. If run outside an Autolab repo,
it upgrades the package and skips scaffold sync with an explicit message.

Manual fallback (equivalent upgrade + scaffold refresh):

```bash
python -m pip install git+https://github.com/mjenrungrot/autolab.git@vX.Y.Z
autolab sync-scaffold --force
```

Enable commit hooks (staged-file formatting + default-branch version bump):

```bash
./scripts/install-hooks.sh
```

Run formatter/style checks locally:

```bash
./scripts/check_style.sh
```

This also syncs the pinned release tag in `README.md` and can sync the current
`vX.Y.Z` tag to GitHub (`origin`) after each commit on the default branch.
By default, hooks only run on the default branch and release-tag pruning is disabled
unless `scripts/sync_release_tags.py --prune` is used explicitly.
These local hooks are optional; CI workflows under `.github/workflows/` are authoritative.

After install, invoke with `autolab --help` or `python -m autolab --help`.

## What it does

Autolab drives a repeatable experiment lifecycle through a nine-stage pipeline:

`hypothesis -> design -> implementation -> implementation_review -> launch -> slurm_monitor -> extract_results -> update_docs -> decide_repeat`

Two terminal stages (`human_review`, `stop`) handle escalation and completion.

### Local vs SLURM stage graph

- Local host mode:
  - `launch -> slurm_monitor (auto-skip/no-op) -> extract_results`
- SLURM host mode:
  - `launch`: submit job, write initial manifest, append ledger entry
  - `slurm_monitor`: poll scheduler, sync artifacts, update manifest/ledger statuses
  - `extract_results`: consume local artifacts, emit `completed|partial|failed` metrics
- Why `slurm_monitor` exists:
  - It keeps async scheduler polling/sync responsibilities out of extraction logic while preserving a single canonical stage graph.

**Operating modes**

- **Manual** (`autolab run`) -- single stage transition, ideal for debugging verifiers and manual checkpoints.
- **Agent runner** (`autolab loop --auto`) -- bounded or unattended multi-step execution with guardrails and lock management.
- **Assistant** (`autolab run --assistant`) -- task-driven delivery from backlog (`select -> implement -> verify -> review`).

See `docs/workflow_modes.md` for detailed responsibility contracts per mode.

## Configuration

**Run mode.** `autolab run` executes a single transition; `autolab loop --max-iterations N` runs bounded multi-step; `autolab loop --auto --max-hours H` enables unattended operation. Add `--verify` to run policy-driven verification before evaluation. Use `--decision <stage>` to force a human choice at `decide_repeat`, or `--auto-decision` to let Autolab choose from the backlog. See `docs/workflow_modes.md`.

**Manual steering commands.**

- `autolab focus --iteration-id <id>` or `autolab focus --experiment-id <id>` retargets state focus with legal checks and a clean handoff reset.
- `autolab todo list|add|done|remove|sync` manages `docs/todo.md` and `.autolab/todo_state.json` for cross-session steering.
- `autolab experiment move --to planned|plan|in_progress|done` updates backlog lifecycle type/status, moves iteration folders across `experiments/<type>/`, and rewrites scoped path references.

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
- **launch** -- executes `launch/run_local.sh` (local) or submits `launch/run_slurm.sbatch` via `sbatch` (SLURM), writes `runs/<run_id>/run_manifest.json`, then advances to slurm_monitor.
- **slurm_monitor** -- updates `runs/<run_id>/run_manifest.json` (and `docs/slurm_job_list.md` for SLURM); local runs auto-skip to extraction.
- **extract_results** -- `runs/<run_id>/metrics.json`, `analysis/summary.md`; assumes local evidence or emits `partial|failed` with explicit missing-evidence accounting.
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
- `run_health.py` / `result_sanity.py` -- env-smoke checks; `run_health.py` runs for env-smoke stages, while `result_sanity.py` is stage-gated to `extract_results`.
- Canonical command: `autolab verify --stage <stage>`.
- Latest result persisted to `.autolab/verification_result.json`.
- Timestamped verification summaries are written to `.autolab/logs/verification_*.json`.
- Verification summary retention is automatic: `autolab verify` keeps only the latest 200 summary files.
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

Preferred upgrade path:

```bash
autolab update
```

Manual scaffold sync into a repo:

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
- `docs/workflow_registry_policy.md` -- workflow capability vs policy requirement model
- `docs/runner_reference.md` -- agent runner YAML reference and runner presets
- `docs/artifact_contracts.md` -- full artifact schemas, backlog format, and state contract
- `docs/skills/README.md` -- skill source/distribution layout and redirect rationale
- `docs/prompt_authoring_guide.md` -- scaffold prompt conventions and stage-prompt wiring
- `docs/quickstart.md` -- getting started walkthrough
- `examples/golden_iteration/` -- complete stage-by-stage artifact example
