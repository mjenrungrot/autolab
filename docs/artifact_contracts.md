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
- **Required fields**: `schema_version`, `id`, `iteration_id`, `hypothesis_id`, `entrypoint`, `compute`, `metrics`, `baselines`, `implementation_requirements`, `extract_parser`
- **Key constraints**:
  - `schema_version` must be string `"1.0"`
  - `compute.location` must be `"local"` or `"slurm"`
  - `baselines` must be non-empty
  - `implementation_requirements` must be non-empty and include `scope_kind`
  - `implementation_requirements[].context_refs[]` may reference `project_map`, `context_delta`, project-wide sidecar items, active experiment sidecar items, or `promoted:<requirement_id>:<item_id>`
  - `project_wide` requirements may not reference experiment sidecars or `context_delta` directly
  - `implementation_requirements[].promoted_constraints[]` are only valid on `project_wide` requirements, and each `source_ref` must target an experiment sidecar item
  - project-wide task execution uses `scope_roots.project_wide_root` from `.autolab/verifier_policy.yaml`
  - `extract_parser` is required (kind `python` or `command`)
  - parser capability contract (when capability artifacts are present): parser kind and primary metric must align with `parser_capabilities.json`
  - `walltime_estimate` format: `HH:MM:SS`
  - `memory_estimate` format: `<number>[KMGT]B` (e.g. `64GB`)
- **Producing stage**: design
- **Consuming stages**: implementation, implementation_review, launch, extract_results

## design_context_quality.json

- **Path**: `experiments/<type>/<iteration_id>/design_context_quality.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/design_context_quality.schema.json`
- **Required fields**: `schema_version`, `generated_at`, `iteration_id`, `experiment_id`, `context_mode`, `available`, `uptake`, `score`, `diagnostics`
- **Purpose**: advisory uptake report for discuss/research context usage in `design.yaml`
- **Produced by**: `autolab verify --stage design` via `.autolab/verifiers/design_context_quality.py`
- **Consumed by**: human review of design-context quality; non-blocking advisory output

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
- **Ownership note**: `slurm_monitor` mutates manifest status/sync state and monitor logs, while `launch` owns SLURM ledger append behavior.

## docs/slurm_job_list.md

- **Path**: `docs/slurm_job_list.md`
- **Format**: Markdown
- **Content**: durable canonical run/job ledger entries for SLURM launches (`run_id`, `job_id`, `iteration_id`, submission date, status at append time)
- **Producing stage**:
  - `launch`: append idempotent canonical entry (including adopted/existing manifests)
- **Consuming checks**:
  - launch/update_docs validation requires `run_id` presence in ledger for SLURM manifests
  - `autolab slurm-job-list verify` can audit canonical line presence
- **Ownership note**: `slurm_monitor` does not rewrite ledger rows; it advances `run_manifest.json` status/sync fields.

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
- **Contract**:
  - parser-first: `design.yaml.extract_parser` writes summary directly
  - fallback: configure `.autolab/verifier_policy.yaml -> extract_results.summary.llm_command` when using `mode: llm_on_demand`
- **Producing stage**: extract_results
- **Consuming stages**: update_docs, decide_repeat

## parser_capabilities.json

- **Path**: `experiments/<type>/<iteration_id>/parser_capabilities.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/parser_capabilities.schema.json`
- **Required fields**: `schema_version`, `iteration_id`, `parser`, `supported_metrics`, `output_contract`, `generated_at`
- **Key constraints**:
  - `parser.kind` must align with `design.yaml.extract_parser.kind`
  - `supported_metrics` must include `design.metrics.primary.name`
- **Produced by**: `autolab parser init` (or maintained manually)
- **Consumed by**: `autolab parser test`, `schema_checks.py` (design+downstream stages when artifacts are present)

## .autolab/parser_capabilities.json

- **Path**: `.autolab/parser_capabilities.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/parser_capabilities_index.schema.json`
- **Content**: global iteration-to-manifest index (`iterations[<iteration_id>] -> manifest_path/parser_kind/supported_metrics/updated_at`)
- **Produced by**: `autolab parser init` (upsert)
- **Consumed by**: `autolab parser test`, `schema_checks.py`

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

## traceability_coverage.json

- **Path**: `experiments/<type>/<iteration_id>/traceability_coverage.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/traceability_coverage.schema.json`
- **Required top-level fields**:
  - `schema_version`, `generated_at`, `iteration_id`
  - `experiment_id`, `run_id`
  - `claim` (canonical v1 claim record)
  - `decision` (iteration-level decision snapshot: status/value/rationale/pointer/evidence_count)
  - `links[]` (joined `requirement_id -> task_id -> verification -> measurement -> decision` rows)
  - `summary` (coverage/failure rollups)
  - `pointers` (artifact pointers used for reconstruction)
  - `diagnostics` (non-blocking gaps/missing artifact notes)
- **Primary implementation evidence source**:
  - `pointers.plan_execution_summary_path` points to `plan_execution_summary.json` for per-task execution/verification evidence.
  - `plan_execution_state.json` is a scheduler-resume artifact and is not the primary row evidence source.
- **Coverage status vocabulary**:
  - `covered`, `untested`, `failed`
- **Failure classification vocabulary**:
  - `design`, `execution`, `measurement` (plus `none` for fully covered rows)
- **Producing stage**:
  - auto-refresh on `decide_repeat` decision application (best effort; non-blocking)
  - manual regeneration via `autolab trace [--iteration-id <id>] [--json]`
- **Consuming stages**: observability/handoff/debug workflows (advisory, not stage-gating)
- **Verifier note**: schema checks can validate traceability artifacts when present; this does not change stage-transition semantics.

## .autolab/traceability_latest.json

- **Path**: `.autolab/traceability_latest.json`
- **Format**: JSON
- **Content**:
  - latest `iteration_id`/`experiment_id` trace pointer
  - compact traceability summary counters
  - decision snapshot for the same trace refresh
- **Produced when**:
  - `traceability_coverage.json` is refreshed (auto or manual `autolab trace`)
- **Consumed by**:
  - quick machine lookup workflows (without walking iteration paths)

## run_context.json

- **Path**: `.autolab/run_context.json`
- **Format**: JSON
- **Fields**: `schema_version`, `generated_at`, `iteration_id`, `experiment_id`, `stage`, `run_id`
- **Produced when**: launch stage begins (system-owned run id allocation)
- **Consumed by**: prompt rendering / launch stage runner context

## plan_check_result.json

- **Path**: `.autolab/plan_check_result.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/plan_check_result.schema.json`
- **Required fields**:
  - base checker output: `schema_version`, `generated_at`, `stage`, `iteration_id`, `passed`, `error_count`, `warning_count`, `errors`, `warnings`, `rule_results`, `artifacts`
  - plan identity: `plan_hash`
  - promotion safety: `promotion_checks` (per promoted requirement coverage/consumption rows)
  - approval risk: `approval_risk` (`requires_approval`, `trigger_reasons`, `counts`, `policy`, `risk_fingerprint`)
- **Produced when**: implementation plan contract validation runs
- **Consumed by**: implementation execution gating, generated docs/state views, and `plan_approval.json`

## plan_graph.json

- **Path**: `.autolab/plan_graph.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/plan_graph.schema.json`
- **Fields**: dependency nodes/edges plus wave bins
- **Produced when**: implementation plan contract validation runs
- **Consumed by**: implementation execution scheduler, wave observability, docs/TUI views, and `plan_approval.json`

## plan_execution_state.json

- **Path**: `experiments/<type>/<iteration_id>/plan_execution_state.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/plan_execution_state.schema.json`
- **Required fields**: contract hash/path, wave cursor/retry state, per-task status/attempt/error/file tracking, per-task timing/reason/verification state, per-wave attempt history, retry reasons, and out-of-contract edit paths
- **Produced when**: implementation executes contract-driven waves
- **Consumed by**: implementation scheduler resume/retry logic, `plan_execution_summary.json` projection, handoff summaries, and generated docs/TUI observability views

## plan_execution_summary.json

- **Path**: `experiments/<type>/<iteration_id>/plan_execution_summary.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/plan_execution_summary.schema.json`
- **Required fields**:
  - execution identity: `schema_version`, `generated_at`, `stage`, `iteration_id`, `plan_file`, `contract_hash`, `run_unit`
  - counters: `tasks_total`, `tasks_completed`, `tasks_failed`, `tasks_blocked`, `tasks_pending`, `tasks_skipped`, `tasks_deferred`, `waves_total`, `waves_executed`
  - observability projections: `wave_details`, `task_details`, `critical_path`, `file_conflicts`, `diagnostics`, `observability_summary`
- **Produced when**: implementation executes or resumes contract-driven waves
- **Consumed by**: traceability coverage, handoff generation, `autolab progress`, generated `project|state|sidecar` views, and the TUI Waves view
- **Consumer behavior note**: generated docs and progress surfaces should treat stale or mismatched observability payloads as diagnostics when possible instead of crashing the whole view.

## plan_approval.json

- **Path**: `experiments/<type>/<iteration_id>/plan_approval.json`
- **Format**: JSON
- **Schema**: `.autolab/schemas/plan_approval.schema.json`
- **Required fields**:
  - identity: `schema_version`, `generated_at`, `iteration_id`
  - approval state: `status`, `requires_approval`, `plan_hash`, `risk_fingerprint`
  - risk context: `trigger_reasons`, `counts`, `source_paths`
  - review metadata: `reviewed_by`, `reviewed_at`, `notes`
- **Produced when**: implementation planning/checkpoint logic runs, or `autolab approve-plan` records a decision
- **Consumed by**: `autolab run --plan-only`, `autolab run`, `autolab run --execute-approved-plan`, `autolab approve-plan`, handoff/status surfaces, and generated `project|state|sidecar` views
- **Status model**:
  - `not_required`: risk policy does not require approval for the current plan
  - `pending`: approval required before execution
  - `approved`: current plan/risk fingerprint approved for execution
  - `retry`: operator requested replanning before execution
  - `stop`: operator ended the experiment from the checkpoint

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
  - `wave`, `task_status` (including `skipped` and `deferred`)
  - `latest_verifier_summary`, `blocking_failures`, `pending_human_decisions`
  - `files_changed_since_last_green_point`
  - `recommended_next_command`, `safe_resume_point`
  - `continuation_packet` (nested scope-root continuation/export envelope)
  - optional `plan_approval` snapshot when an iteration-scoped approval artifact exists
  - `wave_observability` (`wave_summary`, `task_summary`, `summary`, `critical_path`, `file_conflicts`, `waves`, `tasks`, `diagnostics`, `source_paths`)
  - `last_green_at`, `baseline_snapshot`
  - `handoff_json_path`, `handoff_markdown_path`
- **Produced by**: `autolab progress`, `autolab handoff`, auto-refresh on verifier/run-loop/stage-steering exits
- **Consumed by**: `autolab resume`, `autolab oracle`, `autolab oracle apply`, `autolab tui` Home and Waves panels, takeover automation, and generated docs state/sidecar views
- **Presentation note**: top-level handoff fields remain the concise summary surface; CLI/docs consumers render wave retry/block/deferred/skipped detail, critical-path timing context, and observability diagnostics from `wave_observability`, while richer continuation exports read `continuation_packet`.

## continuation_packet

- **Path**: `.autolab/handoff.json -> continuation_packet`
- **Format**: Nested JSON object
- **Content**: Scope-root continuation envelope derived from the active handoff snapshot. It carries the current continuation context plus the artifact references used to build richer takeover exports without changing the top-level handoff summary contract.
- **Scope-root resolution**:
  - `project_wide` -> configured `scope_roots.project_wide_root` (default `.`)
  - `experiment` -> active iteration directory (`experiments/<type>/<iteration_id>/`)
- **Produced by**: `autolab progress`, `autolab handoff`, auto-refresh on verifier/run-loop/stage-steering exits
- **Consumed by**: `autolab oracle`, `autolab oracle apply`, and continuation-oriented takeover tooling
- **Export note**: `autolab oracle` resolves `continuation_packet` into `<scope-root>/oracle.md`, inlining current artifact content at export time. `autolab oracle apply` consumes expert notes separately and does not mutate the export artifact.

## handoff.md

- **Path**: `<scope-root>/handoff.md`
- **Format**: Markdown
- **Content**: Concise human-readable handoff summary (scope, stage, optional plan approval status/triggers, wave/task status, critical path, per-wave timings/retries, blocked/deferred/skipped tasks, file conflicts, per-task evidence, verifier summary, blockers, pending decisions, changed files, recommended next command, safe resume status)
- **Scope-root resolution**:
  - `project_wide` -> configured `scope_roots.project_wide_root` (default `.`)
  - `experiment` -> active iteration directory (`experiments/<type>/<iteration_id>/`)
- **Produced by**: `autolab progress`, `autolab handoff`, auto-refresh on verifier/run-loop/stage-steering exits
- **Consumed by**: human takeover workflows and incident handoff review
- **Relationship to `oracle.md`**: `handoff.md` stays pointer-oriented and concise; use `autolab oracle` when you want the expanded inlined continuation export.

## oracle.md

- **Path**: `<scope-root>/oracle.md`
- **Format**: Markdown
- **Content**: On-demand expanded continuation export derived from `handoff.json.continuation_packet`, combining the current handoff state with inlined artifact content from the active scope root.
- **Scope-root resolution**:
  - `project_wide` -> configured `scope_roots.project_wide_root` (default `.`)
  - `experiment` -> active iteration directory (`experiments/<type>/<iteration_id>/`)
- **Produced by**: `autolab oracle` and `autolab oracle roundtrip --auto` (on demand only; not auto-refreshed by run/verify/handoff)
- **Consumed by**: rich human/agent takeover workflows that need a single inlined scope-root export
- **Relationship to `handoff.md`**: `oracle.md` is the dense, inlined companion to the concise `handoff.md` summary.

## oracle_state.json

- **Path**: `.autolab/oracle_state.json`
- **Format**: JSON
- **Content**: Canonical Oracle automation state for the current Oracle epoch, including browser-only auto eligibility, attempt status, failure reason, reply path, advisory verdict, suggested next action, human-review recommendation flag, and any temporarily disfavored family.
- **Produced by**: `autolab oracle roundtrip --auto` and `autolab oracle apply`
- **Consumed by**: `autolab progress`, `autolab handoff`, `autolab resume`, `autolab tui`, and unattended campaign/run governance

## oracle_last_response.md

- **Path**: `.autolab/oracle_last_response.md`
- **Format**: Markdown
- **Content**: Last captured Oracle browser reply, whether or not apply succeeded
- **Produced by**: `autolab oracle roundtrip --auto`
- **Consumed by**: manual inspection and follow-up `autolab oracle apply` retries

## results.tsv

- **Path**: `<scope-root>/results.tsv`
- **Format**: TSV
- **Columns**: `revision_label`, `run_id`, `primary_metric`, `memory_gb`, `status`, `summary`
- **Status vocabulary**: `keep`, `discard`, `crash`, `partial`
- **Scope-root resolution**:
  - `project_wide` -> configured `scope_roots.project_wide_root` (default `.`)
  - `experiment` -> active iteration directory (`experiments/<type>/<iteration_id>/`)
- **Produced by**: campaign-mode results regeneration on campaign start/continue, challenger promotion-discard decisions, and campaign stop/rethink/error exits
- **Consumed by**: unattended-campaign scan/review workflows and status surfaces
- **Authorship rule**: generated from canonical campaign/run artifacts; not user-authored

## results.md

- **Path**: `<scope-root>/results.md`
- **Format**: Markdown
- **Content**: human-readable campaign results ledger with campaign metadata, champion summary, keep/discard/crash/partial totals, a compact idea-journal summary, and the current TSV-equivalent results table
- **Scope-root resolution**:
  - `project_wide` -> configured `scope_roots.project_wide_root` (default `.`)
  - `experiment` -> active iteration directory (`experiments/<type>/<iteration_id>/`)
- **Produced by**: campaign-mode results regeneration on campaign start/continue, challenger promotion-discard decisions, and campaign stop/rethink/error exits
- **Consumed by**: handoff/oracle expert review and wake-up scan workflows
- **Relationship to `results.tsv`**: `results.md` is the readable companion to the canonical generated table in `results.tsv`

## campaign.json

- **Path**: `.autolab/campaign.json`
- **Format**: JSON
- **Content**: Canonical campaign control-plane state for unattended experiment search, including objective binding, champion metadata, lock contract, crash/improvement streaks, oracle export timestamp, optional oracle feedback history, and bounded novelty memory in `idea_journal`.
- **Produced by**: campaign start/continue/stop flows, challenger promotion-discard decisions, `autolab oracle`, `autolab oracle roundtrip --auto`, and `autolab oracle apply`
- **Consumed by**: campaign runtime, status/handoff surfaces, and oracle round-trip tooling
- **Idea journal extension**:
  - `idea_journal.active_entry_id` points to the current challenger idea span when present
  - `idea_journal.entries[]` records one bounded entry per idea span with thesis, family label/key, touched surfaces, attempts, run IDs, and keep/discard/crash outcome
  - `idea_journal.family_stats{}` keeps aggregate keep/discard/crash and near-miss counts per family even after old entries are trimmed
- **Oracle feedback extension**:
  - `oracle_feedback[]` is optional and append-only
  - each entry records `applied_at`, `source`, `summary`, `detail`, and `signal`
  - `signal` vocabulary: `none`, `stop`, `rethink`
  - `autolab oracle apply` may also update campaign `status` to `stopped` or `needs_rethink` when verdict mapping carries those signals

## context sidecars (`discuss.json` / `research.json`)

- **Paths**:
  - `.autolab/context/sidecars/project_wide/discuss.json`
  - `.autolab/context/sidecars/project_wide/research.json`
  - `experiments/<type>/<iteration_id>/context/sidecars/discuss.json`
  - `experiments/<type>/<iteration_id>/context/sidecars/research.json`
- **Format**: JSON
- **Schemas**:
  - discuss: `.autolab/schemas/discuss_sidecar.schema.json`
  - research: `.autolab/schemas/research_sidecar.schema.json`
- **Required shared metadata**:
  - `schema_version` (`"1.0"`), `sidecar_kind`, `scope_kind`, `scope_root`, `generated_at`
  - project-wide sidecars must omit `iteration_id` and `experiment_id`
  - experiment-scoped sidecars must carry `iteration_id` and `experiment_id`
- **Optional provenance metadata**:
  - `derived_from[]` and `stale_if[]` dependency refs
  - each dependency ref records `path`, `fingerprint`, and optional `reason`
- **Required collection arrays**:
  - discuss: `locked_decisions[]`, `preferences[]`, `constraints[]`, `open_questions[]`, `promotion_candidates[]`
  - research: `questions[]`, `findings[]`, `recommendations[]`, `sources[]`
- **Oracle apply note**: `autolab oracle apply` writes only to existing discuss collections plus mirrored `research.questions[]`; it does not synthesize findings or recommendations.
- **Collection item contract**:
  - every entry is an object with `id`, `summary`, and optional `detail`
  - discuss `promotion_candidates[]` may also carry `target_scope_kind`, `requirement_hint`, and `rationale`
  - research findings/recommendations carry explicit `question_ids[]`, `finding_ids[]`, and `source_ids[]` linkage
  - research `sources[]` may carry `kind`, `path`, and `fingerprint`
- **Produced by**:
  - `autolab discuss --scope ...`
  - `autolab research --scope ...`
- **Consumed by**: `autolab render --view context`, compact design/implementation prompt context, and advisory design-context-quality scoring
- **Verifier note**: `schema_checks.py` validates these sidecars when present; missing sidecars remain non-fatal. It checks `scope_root` identity, research-linkage IDs, and source path/fingerprint integrity when `sources[].path` is populated.

### render context resolution (`context_resolution`)

- **Path**: inline inside `autolab render --view context` JSON output
- **Format**: JSON object
- **Required top-level fields**:
  - `scope_kind`, `scope_root`, `component_order`, `components`, `effective_discuss`, `effective_research`, `compact_render`, `diagnostics`
- **Component row contract**:
  - `component_id`, `artifact_kind`, `scope_kind`, `path`, `status`, `selected`, `selection_reason`, `precedence_index`, `fingerprint`, `derived_from`, `stale_if`, `stale`, `stale_reasons`
- **Selection order**:
  - project-wide render: `project_map`, `project_wide_discuss`, `project_wide_research`
  - experiment render: `project_map`, `project_wide_discuss`, `project_wide_research`, `context_delta`, `experiment_discuss`, `experiment_research`
- **Resolver rule**:
  - project-wide render never loads experiment-local sidecars
  - experiment render may load shared project-wide sidecars plus the active iteration overlay
  - sidecars from any other iteration/experiment are ignored and surfaced as diagnostics when bundle pointers are stale or mismatched
  - runner prompts do not inline raw sidecars; design/implementation consume compact discuss/research summaries and explicit context refs instead

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
