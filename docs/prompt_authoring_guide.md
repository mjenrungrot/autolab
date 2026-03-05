# Prompt Authoring Guide

This guide describes how to author scaffold stage prompts under `src/autolab/scaffold/.autolab/prompts/`.

## Core conventions

- Stage prompts are audience-specific:
  - `stage_<name>.runner.md`
  - `stage_<name>.audit.md`
  - `stage_<name>.brief.md`
  - `stage_<name>.human.md`
- Stage metadata is canonical in `.autolab/workflow.yaml` (prompt file mapping, required tokens, verifier capabilities).
- Every stage should map runner/audit/brief/human prompt files in `.autolab/workflow.yaml`.
- `stage_<name>.runner.md` is the primary execution payload; audit/brief/human/context outputs are companion artifacts.
- `required_outputs` entries in `.autolab/workflow.yaml` should be concrete relative paths; use `<RUN_ID>` token for run-scoped artifacts (for example `runs/<RUN_ID>/run_manifest.json`).
- Registry/policy output paths must use angle-bracket pattern tokens (for example `<RUN_ID>`). Prompt-style mustache tokens (for example `{{run_id}}`) are reserved for prompt rendering only.
- Shared includes live under `prompts/shared/` and are referenced with:
  - `{{shared:guardrails.md}}`
  - `{{shared:repo_scope.md}}`
- Prompt token placeholders are rendered by Autolab before runner execution.

## Runner hard rules

Runner prompts must stay task-solving only. Keep policy/audit payloads out of runner text.

- Never add a manual `## STATUS VOCABULARY` section in runner prompts.
- Only mutator runner stages (`launch`, `slurm_monitor`, `extract_results`) may include `{{shared:status_vocabulary.md}}`; all other runner stages must omit status vocabulary.
- Do not include `## FILE LENGTH BUDGET`, `## VERIFICATION RITUAL`, `## EVIDENCE RECORD FORMAT`, `## EVIDENCE POINTERS`, `## RUN ARTIFACTS`, `## FILE CHECKLIST`, or `## CHECKLIST`.
- Do not include `{{shared:verification_ritual.md}}`, `{{shared:verifier_common.md}}`, or `{{shared:runtime_context.md}}` in runner prompts.
- Do not include raw blob tokens in runner prompts: `{{diff_summary}}`, `{{verifier_outputs}}`, `{{verifier_errors}}`, `{{dry_run_output}}`.
- If a runner uses optional stage tokens, include a `## MISSING-INPUT FALLBACKS` section.
- Runner prompts must not render sentinel values such as `unavailable:`, `unknown`, or `none`.
- Runner headings must be unique (no duplicate `## ...` headings).

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
- `{{launch_execute}}`
- `{{metrics_summary}}`
- `{{target_comparison}}`
- `{{decision_suggestion}}`
- `{{auto_metrics_evidence}}`
- `{{run_group}}`
- `{{replicate_count}}`
- `{{task_context}}`

Do not leave unresolved placeholders in required outputs.
For per-token descriptions and stage guidance, see `docs/prompt_token_reference.md`.

## Rendered prompt outputs

Autolab writes rendered artifacts to:

- `.autolab/prompts/rendered/<stage>.runner.md`
- `.autolab/prompts/rendered/<stage>.audit.md`
- `.autolab/prompts/rendered/<stage>.brief.md`
- `.autolab/prompts/rendered/<stage>.human.md`
- `.autolab/prompts/rendered/<stage>.context.json`

`<stage>.runner.md` is the primary payload passed to the runner process. `<stage>.audit.md`, `<stage>.brief.md`, `<stage>.human.md`, and `<stage>.context.json` are companion artifacts exposed for policy, review/handoff, and tooling.

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

For runner prompts, keep this minimal: mission, strict outputs, required inputs, stop conditions, and a short non-negotiables block.

## Methodology-rich hypothesis prompts

When authoring `stage_hypothesis.md`, optimize for onboarding quality while keeping
stage boundaries clear:

- Make the artifact standalone for orientation: a new reader should understand the
  hypothesis rationale, workflow, measurement logic, and handoff constraints.
- Keep implementation grounding explicit but non-committal:
  - expected modules/files and dependency assumptions are allowed
  - executable protocol details still belong to `design.yaml`
  - execution proof still belongs to `implementation_plan.md`
- Require a concise methodology workflow in numbered
  `input -> action -> output artifact` form.
- Require a measurement plan that states:
  - primary metric definition (`PrimaryMetric:` strict line)
  - aggregation rule
  - baseline comparison rule
  - success-threshold interpretation
- Require reproducibility commitments (seed strategy, config provenance,
  data/version assumptions).
- Keep narrative bounded and scannable: prefer short sections and bullets over
  long prose blocks.

## Adding a new stage prompt

1. Add `stage_<name>.runner.md`, `.audit.md`, `.brief.md`, and `.human.md` under `src/autolab/scaffold/.autolab/prompts/`.
1. Register the stage in `.autolab/workflow.yaml` (`runner_prompt_file`, `prompt_file`, `brief_prompt_file`, `human_prompt_file`, `required_tokens`, `required_outputs`, `verifier_categories`, `classifications`).
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
Do not use `{{run_id}}` in workflow output contracts.

## Token changes end-to-end

When adding a new prompt token:

1. Add token resolution logic in `src/autolab/prompts.py`.
1. Add/adjust stage required/optional token metadata in `.autolab/workflow.yaml` (`required_tokens`, `optional_tokens`); `prompt_lint.py` derives allowed tokens from workflow/registry metadata at runtime.
1. If the token is runtime-injected and not represented in stage token contracts, update `_resolve_allowed_tokens()` supplemental tokens in `src/autolab/scaffold/.autolab/verifiers/prompt_lint.py` and keep `_FALLBACK_ALLOWED_TOKENS` in sync for fallback mode.
1. Update `docs/prompt_token_reference.md` and add render/lint tests that prove affected stages have no unresolved or unsupported tokens.
