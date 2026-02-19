# Prompt Token Reference

Source of truth for allowed prompt tokens is:
`src/autolab/scaffold/.autolab/verifiers/prompt_lint.py` (`ALLOWED_TOKENS`).

Token records:

- `token`: `{{iteration_id}}`; `meaning`: Active iteration identifier from state/context; `typical_stages`: all non-terminal stages.
- `token`: `{{iteration_path}}`; `meaning`: Resolved iteration directory path; `typical_stages`: all non-terminal stages.
- `token`: `{{experiment_id}}`; `meaning`: Active experiment ID (may be empty if unresolved); `typical_stages`: design, decide_repeat.
- `token`: `{{paper_targets}}`; `meaning`: Configured documentation target list from state; `typical_stages`: update_docs.
- `token`: `{{python_bin}}`; `meaning`: Python executable selected by policy; `typical_stages`: all stages.
- `token`: `{{recommended_memory_estimate}}`; `meaning`: Memory recommendation derived from host context/policy; `typical_stages`: design, launch.
- `token`: `{{available_memory_gb}}`; `meaning`: Detected host memory (GB) for planning context; `typical_stages`: design, launch.
- `token`: `{{stage}}`; `meaning`: Current stage name; `typical_stages`: all stages.
- `token`: `{{stage_context}}`; `meaning`: Rendered runtime context block (scope, host mode, snapshots); `typical_stages`: all stages.
- `token`: `{{run_id}}`; `meaning`: Resolved run identifier for run-scoped stages; `typical_stages`: launch, slurm_monitor, extract_results, update_docs, decide_repeat.
- `token`: `{{hypothesis_id}}`; `meaning`: Resolved hypothesis ID mapped from backlog/state; `typical_stages`: hypothesis, design.
- `token`: `{{review_feedback}}`; `meaning`: Review feedback text context for retries/remediation; `typical_stages`: implementation.
- `token`: `{{verifier_errors}}`; `meaning`: Aggregated verifier error text from prior runs; `typical_stages`: implementation, review.
- `token`: `{{verifier_outputs}}`; `meaning`: Compacted verifier output summary context; `typical_stages`: review, decide_repeat.
- `token`: `{{dry_run_output}}`; `meaning`: Output from configured dry-run command; `typical_stages`: implementation, review.
- `token`: `{{launch_mode}}`; `meaning`: Resolved launch host mode (`local` or `slurm`); `typical_stages`: launch.
- `token`: `{{metrics_summary}}`; `meaning`: Compacted metrics summary for decision/docs stages; `typical_stages`: update_docs, decide_repeat.
- `token`: `{{target_comparison}}`; `meaning`: Computed target-vs-observed comparison text; `typical_stages`: update_docs, decide_repeat.
- `token`: `{{decision_suggestion}}`; `meaning`: Autolab suggested next-stage decision; `typical_stages`: decide_repeat.
- `token`: `{{auto_metrics_evidence}}`; `meaning`: Structured evidence payload behind auto metrics suggestion; `typical_stages`: decide_repeat.
- `token`: `{{diff_summary}}`; `meaning`: Git diff summary and changed path context; `typical_stages`: implementation_review.
- `token`: `{{run_group}}`; `meaning`: JSON list of replicate run IDs for multi-run iterations (empty list for single runs); `typical_stages`: launch, extract_results.
- `token`: `{{replicate_count}}`; `meaning`: Number of replicates configured in design.yaml (1 for single runs); `typical_stages`: launch, extract_results.

Notes:

- Stage-required tokens are defined in `src/autolab/scaffold/.autolab/workflow.yaml`.
- Unsupported tokens fail `prompt_lint`.
- Required unresolved tokens fail prompt rendering before runner execution.
