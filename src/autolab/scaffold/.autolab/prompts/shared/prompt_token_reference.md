## Prompt Token Reference

### Shared tokens (available in all stages)

- `{{python_bin}}` -- Python interpreter path
- `{{stage}}` -- Current stage name
- `{{stage_context}}` -- Runtime stage context block

### Stage-specific tokens

- **hypothesis**: `{{iteration_id}}`, `{{iteration_path}}`, `{{hypothesis_id}}`
- **design**: `{{iteration_id}}`, `{{iteration_path}}`, `{{hypothesis_id}}`
- **implementation**: `{{iteration_id}}`, `{{iteration_path}}`
- **implementation_review**: `{{iteration_id}}`, `{{iteration_path}}`
- **launch**: `{{iteration_id}}`, `{{iteration_path}}`, `{{run_id}}`, `{{launch_mode}}`
- **slurm_monitor**: `{{iteration_id}}`, `{{iteration_path}}`, `{{run_id}}`
- **extract_results**: `{{iteration_id}}`, `{{iteration_path}}`, `{{run_id}}`
- **update_docs**: `{{iteration_id}}`, `{{iteration_path}}`, `{{run_id}}`, `{{paper_targets}}`
- **decide_repeat**: `{{iteration_id}}`, `{{iteration_path}}`

### Runtime-injected tokens (populated by the engine, not in registry)

- `{{experiment_id}}` -- Current experiment identifier
- `{{review_feedback}}` -- Human review feedback text
- `{{verifier_errors}}` -- Verifier error output
- `{{verifier_outputs}}` -- Full verifier JSON output
- `{{dry_run_output}}` -- Dry-run command stdout/stderr
- `{{metrics_summary}}` -- Metrics summary text
- `{{target_comparison}}` -- Target comparison text
- `{{decision_suggestion}}` -- Auto-suggested decision
- `{{auto_metrics_evidence}}` -- Metrics evidence for auto-decision
- `{{diff_summary}}` -- Git diff summary
- `{{recommended_memory_estimate}}` -- Suggested memory for compute
- `{{available_memory_gb}}` -- Available system memory in GB

### Shared includes

- `{{shared:guardrails.md}}` -- Hard guardrails
- `{{shared:repo_scope.md}}` -- Repository scope rules
- `{{shared:runtime_context.md}}` -- Runtime context template
- `{{shared:verification_ritual.md}}` -- Verification checklist
- `{{shared:checklist.md}}` -- File checklist template

Use `autolab explain stage <stage>` to see the effective token list for any stage.
