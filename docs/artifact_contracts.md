# Artifact Contracts

This document describes each artifact produced during an Autolab iteration: its path pattern, required fields, governing schema, and which stages produce/consume it.

See `examples/golden_iteration/` for canonical examples of all artifacts.

## hypothesis.md

- **Path**: `experiments/<type>/<iteration_id>/hypothesis.md`
- **Format**: Markdown
- **Required sections**: Hypothesis Statement, Primary Metric, Scope In, Scope Out
- **Key constraint**: Exactly one `PrimaryMetric:` line matching:
  `PrimaryMetric: metric_name; Unit: unit_name; Success: baseline +abs_delta or +relative%`
- **Producing stage**: hypothesis
- **Consuming stages**: design, implementation
- **Schema**: None (verified by template_fill.py)
- **Line limit**: Configured in `experiment_file_line_limits.yaml`

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

- **Path**: `experiments/<type>/<iteration_id>/runs/<run_id>/run_manifest.json`
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

- **Path**: `experiments/<type>/<iteration_id>/runs/<run_id>/metrics.json`
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

## Pattern Paths vs Runtime Paths

Throughout this document and in `workflow.yaml`, paths containing angle-bracket tokens like `<RUN_ID>`, `<ITERATION_ID>`, or `<type>` are **pattern paths** -- templates that are resolved at runtime.

**Pattern path**: `experiments/<type>/<iteration_id>/runs/<run_id>/metrics.json`

**Runtime path**: `experiments/plan/h1-focal-loss/runs/20260218T160045Z/metrics.json`

Resolution rules:

- `<type>` -> experiment type from backlog (`plan`, `in_progress`, `done`)
- `<iteration_id>` -> active iteration ID from `.autolab/state.json`
- `<run_id>` -> current run ID from `state.last_run_id`

Never create filesystem paths containing unresolved angle-bracket tokens. If you see `runs/<RUN_ID>/...` in a schema or config file, it means "the path after substitution", not a literal directory named `<RUN_ID>`.
