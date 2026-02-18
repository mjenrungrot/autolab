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
python -m pip install git+https://github.com/mjenrungrot/autolab.git@v1.0.0
```

After upgrading the package from GitHub, refresh local workflow defaults:

```bash
autolab sync-scaffold --force
```

After install, invoke with:

```bash
autolab --help
python -m autolab --help
```

## Agent runner

Autolab supports multiple agent runners via the `runner` field in `.autolab/verifier_policy.yaml`:

```yaml
agent_runner:
  enabled: true
  runner: claude  # Options: codex, claude, custom
```

- **codex** (default): Uses `codex exec` with sandboxed `--add-dir` flags.
- **claude**: Uses Claude Code in non-interactive mode (`claude -p`). Operates from the repo root.
- **custom**: Set `runner: custom` and provide your own `command:` template.

When `runner` is set, the `command` field is auto-populated from the preset. You can still override `command` explicitly for any runner.

## Source layout

- `src/autolab/`: Python package modules (`__main__`, `todo_sync`, `slurm_job_list`)
- `src/autolab/scaffold/.autolab/`: Shared default scaffold assets (prompts, schemas, verifier helpers, defaults)

## Scaffold lifecycle

Autolab uses this stage graph for each iteration:

`hypothesis -> design -> implementation -> implementation_review -> launch -> extract_results -> update_docs -> decide_repeat`

| Stage | Primary outputs | Exit behavior |
| --- | --- | --- |
| `hypothesis` | `hypothesis.md` | advances to `design` when non-empty |
| `design` | `design.yaml` | advances to `implementation` when required keys are present |
| `implementation` | `implementation_plan.md` and code changes | advances to `implementation_review` |
| `implementation_review` | `implementation_review.md`, `review_result.json` | `pass` → `launch`; `needs_retry` → `implementation`; `failed` → `human_review` |
| `launch` | `launch/run_local.sh` or `run_slurm.sbatch`, `runs/<run_id>/run_manifest.json` | advances to `extract_results` |
| `extract_results` | `runs/<run_id>/metrics.json`, `analysis/summary.md` | advances to `update_docs` |
| `update_docs` | `docs_update.md` | advances to `decide_repeat` |
| `decide_repeat` | no artifacts | decides next iteration / next state action |
| `human_review` | none (manual intervention) | terminal |
| `stop` | none (complete) | terminal |

### Failure and retry behavior

- Any stage verifier failure increments `state.stage_attempt` and marks the run as `needs_retry` when below `state.max_stage_attempts`.
- When the stage attempt budget is exhausted, the workflow escalates to `human_review` and marks failure.
- `implementation_review` can explicitly return:
  - `status: pass` (continue)
  - `status: needs_retry` (return to implementation)
  - `status: failed` (escalate `human_review`)

## Key state and backlog contracts

### `.autolab/state.json`

Required fields in state are:
`iteration_id`, `stage`, `stage_attempt`, `last_run_id`, `sync_status`, `max_stage_attempts`, `max_total_iterations`.

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
- `implementation`: `implementation_plan.md`
- `implementation_review`: `implementation_review.md`, `review_result.json`
- `launch`: `launch/run_local.sh` or `launch/run_slurm.sbatch`, `runs/<run_id>/run_manifest.json`
- `extract_results`: `runs/<run_id>/metrics.json`, `analysis/summary.md`
- `update_docs`: `docs_update.md`, plus configured paper target files from `.autolab/state.json`

## Syncing into a repo

From this package checkout:

```bash
autolab sync-scaffold --force
```

This writes/refreshes `.autolab/` in the current repository from the canonical scaffold assets.
