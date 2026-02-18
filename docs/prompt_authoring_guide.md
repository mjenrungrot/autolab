# Prompt Authoring Guide

This guide describes how to author scaffold stage prompts under `src/autolab/scaffold/.autolab/prompts/`.

## Core conventions

- Stage files use `stage_<name>.md` (for example `stage_design.md`).
- Shared includes live under `prompts/shared/` and are referenced with:
  - `{{shared:guardrails.md}}`
  - `{{shared:repo_scope.md}}`
  - `{{shared:runtime_context.md}}`
- Prompt token placeholders are rendered by Autolab before runner execution.

## Supported tokens

Common tokens available in prompts:

- `{{iteration_id}}`
- `{{iteration_path}}`
- `{{run_id}}`
- `{{hypothesis_id}}`
- `{{stage}}`
- `{{review_feedback}}`
- `{{verifier_errors}}`
- `{{verifier_outputs}}`
- `{{dry_run_output}}`
- `{{launch_mode}}`
- `{{metrics_summary}}`
- `{{target_comparison}}`
- `{{decision_suggestion}}`
- `{{stage_context}}`

Do not leave unresolved placeholders in required outputs.

## Rendered prompt outputs

Autolab writes rendered artifacts to:

- `.autolab/prompts/rendered/<stage>.md`
- `.autolab/prompts/rendered/<stage>.context.json`

These files are the exact payload passed to agent runners.

## Stage prompt structure

Use a consistent top block:

1. `# Stage: <name>`
2. `## ROLE`
3. `## PRIMARY OBJECTIVE`

Then include:

- strict output file list
- required inputs
- missing-input fallback behavior
- explicit verifier command (`python .autolab/verifiers/template_fill.py --stage <stage>`)
- failure/retry note

## Adding a new stage prompt

1. Add `src/autolab/scaffold/.autolab/prompts/stage_<name>.md`.
2. Add the file to `STAGE_PROMPT_FILES` in `src/autolab/__main__.py`.
3. Ensure required prompt tokens for the stage are covered in `PROMPT_REQUIRED_TOKENS_BY_STAGE`.
4. If the stage has hard contracts, add schema/verifier checks under `.autolab/verifiers/` and `.autolab/schemas/`.
5. Run tests and `autolab sync-scaffold --force` in downstream repos.

## Verifier alignment

Each mandatory checklist item in stage prompts should map to a verifier or schema check.
Avoid aspirational items that cannot be audited.
