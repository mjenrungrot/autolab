# Prompt Authoring Guide

This guide describes how to author scaffold stage prompts under `src/autolab/scaffold/.autolab/prompts/`.

## Core conventions

- Stage files use `stage_<name>.md` (for example `stage_design.md`).
- Stage metadata is canonical in `.autolab/workflow.yaml` (prompt file mapping, required tokens, verifier capabilities).
- `required_outputs` entries in `.autolab/workflow.yaml` should be concrete relative paths; use `<RUN_ID>` token for run-scoped artifacts (for example `runs/<RUN_ID>/run_manifest.json`).
- Shared includes live under `prompts/shared/` and are referenced with:
  - `{{shared:guardrails.md}}`
  - `{{shared:repo_scope.md}}`
  - `{{shared:runtime_context.md}}`
- Prompt token placeholders are rendered by Autolab before runner execution.

## Supported tokens

Common tokens available in prompts:

- `{{iteration_id}}`
- `{{iteration_path}}`
- `{{experiment_id}}`
- `{{paper_targets}}`
- `{{python_bin}}`
- `{{recommended_memory_estimate}}`
- `{{available_memory_gb}}`
- `{{run_id}}`
- `{{hypothesis_id}}`
- `{{stage}}`
- `{{stage_context}}`
- `{{review_feedback}}`
- `{{verifier_errors}}`
- `{{diff_summary}}`
- `{{verifier_outputs}}`
- `{{dry_run_output}}`
- `{{launch_mode}}`
- `{{metrics_summary}}`
- `{{target_comparison}}`
- `{{decision_suggestion}}`
- `{{auto_metrics_evidence}}`

Do not leave unresolved placeholders in required outputs.
For per-token descriptions and stage guidance, see `docs/prompt_token_reference.md`.

## Rendered prompt outputs

Autolab writes rendered artifacts to:

- `.autolab/prompts/rendered/<stage>.md`
- `.autolab/prompts/rendered/<stage>.context.json`

These files are the exact payload passed to agent runners.

## Stage prompt structure

Use a consistent top block:

1. `# Stage: <name>`
1. `## ROLE`
1. `## PRIMARY OBJECTIVE`

Then include:

- strict output file list
- required inputs
- missing-input fallback behavior
- explicit verifier command (`autolab verify --stage <stage>`)
- failure/retry note

## Adding a new stage prompt

1. Add `src/autolab/scaffold/.autolab/prompts/stage_<name>.md`.
1. Register the stage in `.autolab/workflow.yaml` (`prompt_file`, `required_tokens`, `required_outputs`, `verifier_categories`, `classifications`).
1. If the stage has hard contracts, add schema/verifier checks under `.autolab/verifiers/` and `.autolab/schemas/`.
1. Run tests and `autolab sync-scaffold --force` in downstream repos.

## Verifier alignment

Each mandatory checklist item in stage prompts should map to a verifier or schema check.
Avoid aspirational items that cannot be audited.

## Run-scoped output patterns

When a stage output is run-scoped, use `<RUN_ID>` in `workflow.yaml` output paths:

- `runs/<RUN_ID>/run_manifest.json`
- `runs/<RUN_ID>/metrics.json`

This keeps registry contracts explicit and avoids stage-specific implicit path assumptions.

## Token changes end-to-end

When adding a new prompt token:

1. Add token resolution logic in `src/autolab/prompts.py`.
1. Add the token to `ALLOWED_TOKENS` in `src/autolab/scaffold/.autolab/verifiers/prompt_lint.py`.
1. Add/adjust stage required tokens in `.autolab/workflow.yaml` when the token is mandatory for a stage.
1. Add a render test fixture that asserts rendered prompt output has no unresolved placeholders for affected stage(s).
