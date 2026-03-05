# Artifact Contracts

This document describes each artifact produced during an Autolab iteration: its path pattern, required fields, governing schema, and which stages produce/consume it.

See `src/autolab/example_golden_iterations/` for canonical examples of all artifacts.

## hypothesis.md

- **Path**: `experiments/<type>/<iteration_id>/hypothesis.md`
- **Format**: Markdown
- **Required sections**: Hypothesis Statement, Measurement and Analysis Plan, Scope In, Scope Out
- **Onboarding-recommended methodology sections**:
  - `Research Context and Baseline Evidence`
  - `Methodology Workflow` (numbered `input -> action -> output artifact` steps)
  - `Experimental Units and Data Scope`
  - `Intervention and Control`
  - `Reproducibility Commitments`
  - `Implementation Grounding`
  - `Constraints for Design Stage`
- **Key constraint**: Exactly one `PrimaryMetric:` line matching:
  `PrimaryMetric: metric_name; Unit: unit_name; Success: baseline +abs_delta or +relative%`
- **Section note**: the `PrimaryMetric:` line can live inside `Measurement and Analysis Plan`; a dedicated `## Primary Metric` heading is optional.
- **Machine-validated hypothesis metadata**: `metric_mode` (`maximize|minimize`) and signed `target_delta` semantics remain unchanged.
- **Producing stage**: hypothesis
- **Consuming stages**: design, implementation
- **Schema**: None (verified by `template_fill.py` and stage hypothesis checks)
- **Enforcement mode for methodology richness**: prompt/template guidance (no new hard verifier gates in this iteration)
- **Line limit**: Configured in `experiment_file_line_limits.yaml` (scaffold default: 180 lines)

## design.yaml

- **Path**: `experiments/<type>/<iteration_id>/design.yaml`
- **Format**: YAML
- **Schema**: `.autolab/schemas/design.schema.json`
- **Required fields**: `schema_version`, `id`, `iteration_id`, `hypothesis_id`, `entrypoint`, `compute`, `metrics`, `baselines`
- **Key constraints**:
  - `schema_version` must be string `"1.0"`
  - `compute.location` must be `"local"` or `"slurm"`
  - `baselines` must be non-empty
  - `walltime_estimate` format: `HH:MM:SS`
  - `memory_estimate` format: `<number>[KMGT]B` (e.g. `64GB`)
- **Producing stage**: design
- **Consuming stages**: implementation, implementation_review, launch, extract_results

## implementation_plan.md

- **Path**: `experiments/<type>/<iteration_id>/implementation_plan.md`
- **Format**: Markdown
- **Required section**: `## Change Summary`
- **Optional task blocks** (`### T1: ...`) require: `depends_on`, `location`, `description`, `touches`, `scope_ok`, `validation`, `status`
- **Verified by**: `implementation_plan_lint.py`
- **Producing stage**: implementation
- **Consuming stages**: implementation_review

## implementation_review.md

- **Path**: `experiments/<type>/<iteration_id>/implementation_review.md`
- **Format**: Markdown
- **Content**: Review summary, blocking findings, remediation actions
- **Producing stage**: implementation_review
- **Consuming stages**: launch (gating)

## review_result.json

- **Path**: `experiments/<type>/<iteration_id>/review_result.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/review_result.schema.json`
- **Required fields**: `status` (`pass|needs_retry|failed`), `blocking_findings`, `required_checks`
- **Required checks**: `tests`, `dry_run`, `schema`, `env_smoke`, `docs_target_update` (each `pass|skip|fail`)
- **Auto-enforced checks**: categories such as `prompt_lint` and `consistency` are enforced by the verifier pipeline and evidenced in `.autolab/verification_result.json` (not additional keys in `required_checks`)
- **Producing stage**: implementation_review
- **Consuming stages**: launch (must be `pass` to proceed)

## run_manifest.json

- **Path**: `experiments/<type>/<iteration_id>/runs/<RUN_ID>/run_manifest.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/run_manifest.schema.json`
- **Required fields**: `schema_version`, `run_id`, `iteration_id`, `host_mode`, `command`, `resource_request`, `timestamps`, `artifact_sync_to_local`
- **Dynamic line limit**: Scales with `count_paths` in `experiment_file_line_limits.yaml`
- **Producing stages**: launch (create), slurm_monitor (status/sync updates)
- **Consuming stages**: slurm_monitor, extract_results, update_docs

## docs/slurm_job_list.md

- **Path**: `docs/slurm_job_list.md`
- **Format**: Markdown
- **Content**: durable run/job ledger entries for SLURM launches (`run_id`, `job_id`, status notes)
- **Producing stages**:
  - `launch`: append initial entry at submission
  - `slurm_monitor`: update current scheduler/sync status
- **Consuming stages**: slurm_monitor (primary), extract_results (read-only context)

## metrics.json

- **Path**: `experiments/<type>/<iteration_id>/runs/<RUN_ID>/metrics.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/metrics.schema.json`
- **Required fields**: `schema_version`, `iteration_id`, `run_id`, `status`, `primary_metric`
- **Key constraints**:
  - `status` enum: `completed`, `partial`, `failed`
  - `primary_metric.value` and `delta_vs_baseline`: number or null (null for failed runs)
- **Producing stage**: extract_results
- **Consuming stages**: update_docs, decide_repeat

## analysis/summary.md

- **Path**: `experiments/<type>/<iteration_id>/analysis/summary.md`
- **Format**: Markdown
- **Content**: Run interpretation, metric results, missing evidence accounting
- **Producing stage**: extract_results
- **Consuming stages**: update_docs, decide_repeat

## docs_update.md

- **Path**: `experiments/<type>/<iteration_id>/docs_update.md`
- **Format**: Markdown
- **Content**: What changed, run evidence, recommendation, no-change rationale
- **Producing stage**: update_docs
- **Consuming stages**: decide_repeat

## decision_result.json

- **Path**: `experiments/<type>/<iteration_id>/decision_result.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/decision_result.schema.json`
- **Required fields**: `schema_version`, `decision`, `rationale`, `evidence`, `risks`
- **Key constraints**:
  - `decision` enum: `hypothesis`, `design`, `stop`, `human_review`
  - `evidence` must be non-empty array of `{source, pointer, summary}` objects
- **Producing stage**: decide_repeat
- **Consuming stages**: run_standard (decision application)

## run_context.json

- **Path**: `.autolab/run_context.json`
- **Format**: JSON
- **Fields**: `schema_version`, `generated_at`, `iteration_id`, `experiment_id`, `stage`, `run_id`
- **Produced when**: launch stage begins (system-owned run id allocation)
- **Consumed by**: prompt rendering / launch stage runner context

## plan_execution_state.json

- **Path**: `experiments/<type>/<iteration_id>/plan_execution_state.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/plan_execution_state.schema.json`
- **Required fields**: contract hash/path, wave cursor/retry state, per-task status/attempt/error/file tracking
- **Produced when**: implementation executes contract-driven waves
- **Consumed by**: implementation scheduler resume/retry logic and handoff summaries

## auto_decision.json

- **Path**: `.autolab/auto_decision.json`
- **Format**: JSON
- **Fields**: `schema_version`, `generated_at`, `iteration_id`, `experiment_id`, `stage`, `inputs`, `outputs`
- **Produced when**: decide_repeat applies a decision (manual or automated)
- **Consumed by**: unattended-run audits and debugging workflows

## block_reason.json

- **Path**: `.autolab/block_reason.json`
- **Format**: JSON
- **Fields**: `blocked_at`, `reason`, `stage_at_block`, `action_required`
- **Produced when**: Active experiment is completed in backlog
- **Action**: Re-open experiment in backlog to resume

## handoff.json

- **Path**: `.autolab/handoff.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/handoff.schema.json`
- **Required fields**:
  - `schema_version`, `generated_at`, `state_file`
  - `iteration_id`, `experiment_id`, `current_scope`, `scope_root`, `current_stage`
  - `wave`, `task_status`
  - `latest_verifier_summary`, `blocking_failures`, `pending_human_decisions`
  - `files_changed_since_last_green_point`
  - `recommended_next_command`, `safe_resume_point`
  - `last_green_at`, `baseline_snapshot`
  - `handoff_json_path`, `handoff_markdown_path`
- **Produced by**: `autolab progress`, `autolab handoff`, auto-refresh on verifier/run-loop/stage-steering exits
- **Consumed by**: `autolab resume`, `autolab tui` Home handoff panel, takeover automation

## handoff.md

- **Path**: `<scope-root>/handoff.md`
- **Format**: Markdown
- **Content**: Human-readable handoff summary (scope, stage, wave/task status, verifier summary, blockers, pending decisions, changed files, recommended next command, safe resume status)
- **Scope-root resolution**:
  - `project_wide` -> repository root
  - `experiment` -> active iteration directory (`experiments/<type>/<iteration_id>/`)
- **Produced by**: `autolab progress`, `autolab handoff`, auto-refresh on verifier/run-loop/stage-steering exits
- **Consumed by**: human takeover workflows and incident handoff review

## codebase project map (`project_map.json` / `project_map.md`)

- **Paths**:
  - `.autolab/context/project_map.json`
  - `.autolab/context/project_map.md`
- **Format**:
  - JSON (`project_map.json`, schema-validated)
  - Markdown summary (`project_map.md`)
- **Schema**: `.autolab/schemas/codebase_project_map.schema.json`
- **Required content categories**:
  - stack (languages/manifests/toolchains)
  - architecture (top-level dirs, CI workflows, discovered experiments)
  - conventions (testing, lint, formatter, package manager signals)
  - concerns (risk/quality notes with evidence pointers)
- **Produced by**: `autolab init --from-existing`
- **Consumed by**: prompt context rendering (`## Runtime Stage Context` pointers and summaries), human onboarding

## codebase experiment delta map (`context_delta.json` / `context_delta.md`)

- **Paths**:
  - `experiments/<type>/<iteration_id>/context_delta.json`
  - `experiments/<type>/<iteration_id>/context_delta.md`
- **Format**:
  - JSON (`context_delta.json`, schema-validated)
  - Markdown summary (`context_delta.md`)
- **Schema**: `.autolab/schemas/codebase_experiment_delta.schema.json`
- **Required content categories**:
  - inheritance pointer to project map
  - iteration/experiment identity and path
  - experiment-specific additions (available artifacts, assumptions, concerns, latest run metadata)
- **Produced by**: `autolab init --from-existing` for the selected focus iteration
- **Consumed by**: prompt context rendering, experiment-scoped handoff context

## codebase context bundle (`bundle.json`)

- **Path**: `.autolab/context/bundle.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/codebase_context_bundle.schema.json`
- **Required fields**:
  - project map pointer and summary
  - selected focus iteration/experiment ids
  - selected experiment-delta pointer and summary
  - list of available experiment-delta map entries
- **Produced by**: `autolab init --from-existing`
- **Consumed by**: prompt context assembly (project map + experiment delta pointer injection in runtime context)

## Pattern Paths vs Runtime Paths

Throughout this document and in `workflow.yaml`, paths containing angle-bracket tokens like `<RUN_ID>`, `<ITERATION_ID>`, or `<type>` are **pattern paths** -- templates that are resolved at runtime.

Prompt-style mustache tokens such as `{{run_id}}` are **not** valid in registry/policy path contracts; they are reserved for prompt rendering only.

**Pattern path**: `experiments/<type>/<iteration_id>/runs/<RUN_ID>/metrics.json`

**Runtime path**: `experiments/plan/h1-focal-loss/runs/20260218T160045Z/metrics.json`

Resolution rules:

- `<type>` -> experiment type from backlog (`plan`, `in_progress`, `done`)
- `<iteration_id>` -> active iteration ID from `.autolab/state.json`
- `<RUN_ID>` -> current run ID from `state.last_run_id`

Never create filesystem paths containing unresolved angle-bracket tokens. If you see `runs/<RUN_ID>/...` in a schema or config file, it means "the path after substitution", not a literal directory named `<RUN_ID>`.
