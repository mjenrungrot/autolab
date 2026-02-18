# Autolab shared package

This repository provides the shared `autolab` workflow package used across projects.

## Install

From this directory (`~/Workspaces/autolab`), editable install:

```bash
python -m pip install -e .
```

From a different location / shared usage:

```bash
python -m pip install git+https://github.com/mjenrungrot/autolab.git@main
```

For stable CI or release installs, pin to a tag:

```bash
python -m pip install git+https://github.com/mjenrungrot/autolab.git@v1.1.2
```

After upgrading the package from GitHub, refresh local workflow defaults:

```bash
autolab sync-scaffold --force
```

Enable auto version bump on each commit:

```bash
./scripts/install-hooks.sh
```

This also syncs the pinned release tag in `README.md` and keeps only the latest 10
`vX.Y.Z` tags on GitHub (`origin`) after each commit.

After install, invoke with:

```bash
autolab --help
python -m autolab --help
```

## Agent runner

Autolab supports multiple agent runners via the `runner` field in `.autolab/verifier_policy.yaml`:

```yaml
agent_runner:
  enabled: false  # default
  runner: claude  # Options: codex, claude, custom
  claude_dangerously_skip_permissions: false
```

- **codex** (default runner preset): Uses `codex exec` with sandboxed `--add-dir` flags.
- **claude**: Uses Claude Code in non-interactive mode (`claude -p`) and operates from repo root.
- **custom**: Set `runner: custom` and provide your own `command:` template.

Agent runner execution is disabled by default (`enabled: false`).
When `runner` is set, the `command` field is auto-populated from the preset. You can still override `command` explicitly for any runner.
`claude_dangerously_skip_permissions` is an explicit opt-in for `--dangerously-skip-permissions` and should only be enabled in trusted automation contexts.

## Configuration use cases

Use these configurations based on how much control vs automation you want:

### Runtime mode (`run` / `loop`)

| Configuration | Use case |
| --- | --- |
| Standard mode (default; no `--assistant`) | Deterministic stage-by-stage orchestration (`hypothesis` -> `design` -> ...). Best for debugging stage verifiers and manual checkpoints. |
| Assistant mode (`--assistant`) | Task-driven delivery from `docs/todo.md` / backlog (`select` -> `implement` -> `verify` -> `review`). Best for autonomous feature completion. |

### Run cadence and decision handling

| Configuration | Use case |
| --- | --- |
| `autolab run` | Single controlled transition while iterating locally. |
| `autolab run --verify` | Run policy-driven verification before stage evaluation during a standard run. |
| `autolab loop --max-iterations N` | Bounded multi-step execution without unattended auto-decisions. |
| `autolab loop --auto --max-hours H` | Unattended operation with lock management, guardrails, and automatic decision handling. |
| `--decision <stage>` (at `decide_repeat`) | Force explicit human choice for the next stage. |
| `--auto-decision` | Let Autolab choose from todo/backlog at `decide_repeat` (useful for semi-automated runs). |

### Agent runner controls

| Configuration | Use case |
| --- | --- |
| `agent_runner.enabled: false` (default) | Keep runs verifier-driven and manual by default. |
| `agent_runner.enabled: true` + default `run_agent_mode=policy` | Enable stage prompt execution by policy for normal automation. |
| `--run-agent` | Force runner invocation for a run/loop even when policy has runner disabled (one-off override). |
| `--no-run-agent` | Temporarily disable runner invocation even if policy enables it. |
| `agent_runner.runner: codex` | Default sandboxed runner preset for Codex CLI workflows. |
| `agent_runner.runner: claude` | Claude Code CLI workflows in non-interactive mode. |
| `agent_runner.runner: custom` | Bring your own command template/integration. |
| `agent_runner.edit_scope.mode: iteration_plus_core` (default) | Allow changes in iteration workspace plus shared code/docs directories; good for real implementation work. |
| `agent_runner.edit_scope.mode: iteration_only` | Restrict edits to iteration workspace; good for strict isolation and lower risk. |

### Commit and quality gates (`.autolab/verifier_policy.yaml`)

| Configuration | Use case |
| --- | --- |
| `autorun.auto_commit.mode: meaningful_only` (default) | Commit only when meaningful files changed; recommended default for most repos. |
| `autorun.auto_commit.mode: always` | Always commit run outputs; useful for fully automated pipelines with external filtering. |
| `autorun.auto_commit.mode: disabled` | Never auto-commit; use when humans curate every commit. |
| `autorun.meaningful_change.require_implementation_progress: true` (default) | Block implementation transitions/commits that do not produce meaningful code/config/docs changes. |
| `autorun.meaningful_change.require_implementation_progress: false` | Useful for early scaffolding/prototyping where strict change gating is too restrictive. |
| `autorun.meaningful_change.require_verification: true` (default) | In assistant review cycle, require verification success before task completion. |
| `autorun.meaningful_change.require_git_for_progress: true` (default) | Enforce git-based progress checks in normal repositories; relax only in non-git/sandbox contexts. |
| `--no-strict-implementation-progress` | CLI override for experiments where strict implementation progress checks should be temporarily bypassed. |

### Guardrails for unattended automation

| Configuration | Use case |
| --- | --- |
| `autorun.guardrails.max_same_decision_streak` | Prevent loops that keep choosing the same next-stage decision. |
| `autorun.guardrails.max_no_progress_decisions` | Escalate when repeated cycles show no open-task reduction or meaningful progress. |
| `autorun.guardrails.max_update_docs_cycles` | Prevent repeated `extract_results`/`update_docs` churn without forward progress. |
| `autorun.guardrails.max_generated_todo_tasks` | Cap auto-generated todo tasks to keep focus lists bounded and actionable. |
| `autorun.guardrails.on_breach: human_review` (default) | Safe escalation target when automation is stuck or quality gates cannot be satisfied. |
| `autorun.todo_fallback.local` / `autorun.todo_fallback.slurm` | Configure generated fallback tasks (stage/text/scope) when no actionable tasks remain for the current host mode. |

## Source layout

- `src/autolab/`: Python package modules (`__main__`, `todo_sync`, `slurm_job_list`)
- `src/autolab/scaffold/.autolab/`: Shared default scaffold assets (prompts, schemas, verifier helpers, defaults)
- `.autolab/` in user repos is materialized from scaffold via `autolab init` or `autolab sync-scaffold`; there is no separate `dotautolab/` mirror.

## Scaffold lifecycle

Autolab uses this stage graph for each iteration:

`hypothesis -> design -> implementation -> implementation_review -> launch -> extract_results -> update_docs -> decide_repeat`

| Stage | Primary outputs | Exit behavior |
| --- | --- | --- |
| `hypothesis` | `hypothesis.md` | advances to `design` when metric/target/criteria contract fields are present |
| `design` | `design.yaml` | advances to `implementation` when required keys are present |
| `implementation` | `implementation_plan.md` and code changes | advances to `implementation_review` (requires Dry Run section when policy sets `dry_run: true`) |
| `implementation_review` | `implementation_review.md`, `review_result.json` | `pass` -> `launch`; `needs_retry` -> `implementation`; `failed` -> `human_review` |
| `launch` | `launch/run_local.sh` or `run_slurm.sbatch`, `runs/<run_id>/run_manifest.json` | advances to `extract_results` |
| `extract_results` | `runs/<run_id>/metrics.json`, `analysis/summary.md` | advances to `update_docs` |
| `update_docs` | `docs_update.md` | advances to `decide_repeat` when run evidence references are present |
| `decide_repeat` | `decision_result.json` | decides next iteration / next state action |
| `human_review` | none (manual intervention) | terminal |
| `stop` | none (complete) | terminal |

### Failure and retry behavior

- Any stage verifier failure increments `state.stage_attempt` and marks the run as `needs_retry` when below `state.max_stage_attempts`.
- When the stage attempt budget is exhausted, the workflow escalates to `human_review` and marks failure.
- `implementation_review` can explicitly return:
  - `status: pass` (continue)
  - `status: needs_retry` (return to implementation)
  - `status: failed` (escalate `human_review`)

### State ownership

- `.autolab/state.json` is orchestration-owned. Stage agents should not manually advance stages by editing state.
- Agents should emit stage artifacts; Autolab applies transition, retry, and escalation logic.

## Key state and backlog contracts

### `.autolab/state.json`

Required fields in state are:
`iteration_id`, `stage`, `stage_attempt`, `last_run_id`, `sync_status`, `max_stage_attempts`, `max_total_iterations`.

Optional but recommended:
`history` (recent stage transition records with verifier summary and timestamps).

Example:

```json
{
  "iteration_id": "e1",
  "stage": "implementation",
  "stage_attempt": 0,
  "last_run_id": "",
  "sync_status": "",
  "max_stage_attempts": 3,
  "max_total_iterations": 20
}
```

### `.autolab/backlog.yaml`

Workflow bootstrap expects hypotheses and iterations to be listed as:

```yaml
hypotheses:
  - id: h1
    status: open
    title: "Bootstrap hypothesis"
    success_metric: "primary_metric"
    target_delta: 0.0

experiments:
  - id: e1
    hypothesis_id: h1
    status: open
    iteration_id: "e1"
```

`done`, `completed`, `closed`, and `resolved` are treated as completed terminal statuses for guardrails.

## Artifact map (per stage)

- `hypothesis`: `hypothesis.md`
- `design`: `design.yaml`
- `implementation`: `implementation_plan.md`, `implementation/` (experiment-specific code/notebooks)
- `implementation_review`: `implementation_review.md`, `review_result.json`
- `launch`: `launch/run_local.sh` or `launch/run_slurm.sbatch`, `runs/<run_id>/run_manifest.json`
- `extract_results`: `runs/<run_id>/metrics.json`, `analysis/summary.md`
- `update_docs`: `docs_update.md`, plus configured paper target files from `.autolab/state.json`
- `decide_repeat`: `decision_result.json`
- assistant audit trail: `.autolab/task_history.jsonl`

## Verifiers and schema checks

- `template_fill.py` enforces placeholder cleanup and artifact budget checks per stage.
- `prompt_lint.py` enforces stage prompt structure/token contracts.
- `schema_checks.py` validates stage artifacts against JSON Schemas (including `.autolab/state.json` and `.autolab/backlog.yaml`).
- Canonical stage command: `autolab verify --stage <stage>`.
- Low-level fallback: `{{python_bin}} .autolab/verifiers/template_fill.py --stage <stage>`.
- Latest verification summary is always persisted to `.autolab/verification_result.json`.
- Verifier commands are policy-driven and can use `python_bin` (default `python3`) for interpreter portability.
- `dry_run_command` should be non-empty whenever any stage sets `dry_run: true` in `requirements_by_stage` (scaffold provides a replace-me stub).
- Dynamic run-manifest caps use `line_limits.run_manifest_dynamic` with `min_cap_lines`/`max_cap_lines` (cap bounds, not required minimum output length).

## Prompt authoring

See `docs/prompt_authoring_guide.md` for scaffold prompt conventions, shared includes, and stage-prompt wiring.
See `docs/workflow_modes.md` for explicit manual vs agent-runner vs assistant responsibility contracts.

## Golden examples

A complete stage-by-stage artifact example lives under `examples/golden_iteration/`.
Use it as a reference for schema-compliant outputs and verifier-friendly formatting.

## Install the Autolab skill template

This repo includes a reusable skill file at `docs/skills/autolab/SKILL.md`.

Preferred project-local install:

```bash
autolab install-skill codex
```

This installs to:
`<project-root>/.codex/skills/autolab/SKILL.md`

You can target a different project directory with:

```bash
autolab install-skill codex --project-root /path/to/project
```

Manual global install fallback:

```bash
mkdir -p ~/.codex/skills/autolab
cp docs/skills/autolab/SKILL.md ~/.codex/skills/autolab/SKILL.md
```

## Syncing into a repo

From this package checkout:

```bash
autolab sync-scaffold --force
```

This writes/refreshes `.autolab/` in the current repository from the canonical scaffold assets.

## Reset autolab workflow state

To restore `.autolab/` files to the current packaged defaults (including after upgrading autolab) and reset the workflow state:

```bash
autolab reset
```

Use `--state-file` to target a different state path if needed:

```bash
autolab reset --state-file .autolab/state.json
```
