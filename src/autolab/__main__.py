"""Autolab CLI entry point — delegates to submodules."""
from __future__ import annotations

# Re-export public API for backward compatibility.
# Tests and external consumers import symbols from autolab.__main__,
# so every name that was previously defined here must remain importable.

from autolab.constants import (  # noqa: F401
    ACTIVE_STAGES,
    AGENT_RUNNER_CLAUDE_DANGEROUS_PRESET,
    AGENT_RUNNER_EDIT_SCOPE_MODES,
    AGENT_RUNNER_PRESETS,
    ALL_STAGES,
    ASSISTANT_CONTROL_COMMIT_PATHS,
    ASSISTANT_CYCLE_STAGES,
    AUTO_COMMIT_MODES,
    BACKLOG_COMPLETED_STATUSES,
    DECISION_STAGES,
    DEFAULT_AGENT_RUNNER_COMMAND,
    DEFAULT_AGENT_RUNNER_CORE_DIRS,
    DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE,
    DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR,
    DEFAULT_AGENT_RUNNER_NAME,
    DEFAULT_AGENT_RUNNER_STAGES,
    DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
    DEFAULT_AUTO_COMMIT_MODE,
    DEFAULT_BACKLOG_TEMPLATE,
    DEFAULT_EXPERIMENT_TYPE,
    DEFAULT_MAX_HOURS,
    DEFAULT_MEANINGFUL_EXCLUDE_PATHS,
    DEFAULT_VERIFIER_POLICY,
    EXPERIMENT_LOCKED_TYPES,
    EXPERIMENT_TYPES,
    HOST_MODE_COMMAND_TIMEOUT_SECONDS,
    ITERATION_ID_SAFE_PATTERN,
    LOCK_STALE_SECONDS,
    PACKAGE_SCAFFOLD_DIR,
    PROMPT_LITERAL_TOKENS,
    PROMPT_REQUIRED_TOKENS_BY_STAGE,
    PROMPT_SHARED_INCLUDE_PATTERN,
    PROMPT_TOKEN_PATTERN,
    REVIEW_RESULT_CHECK_STATUSES,
    REVIEW_RESULT_REQUIRED_CHECKS,
    RUN_ID_TIMESTAMP_PATTERN,
    RUNNER_ELIGIBLE_STAGES,
    SLURM_JOB_LIST_PATH,
    STAGE_PROMPT_FILES,
    TERMINAL_STAGES,
    VERIFIER_COMMAND_TIMEOUT_SECONDS,
)

from autolab.models import (  # noqa: F401
    AgentRunnerConfig,
    AgentRunnerEditScopeConfig,
    AutoCommitConfig,
    EvalResult,
    GuardrailConfig,
    MeaningfulChangeConfig,
    RenderedPromptBundle,
    RunOutcome,
    StageCheckError,
    StateError,
    _coerce_bool,
    _coerce_float,
    _coerce_positive_int,
)

from autolab.utils import (  # noqa: F401
    _append_log,
    _assistant_commit_paths,
    _build_auto_commit_message,
    _detect_host_mode_with_probe,
    _detect_priority_host_mode,
    _collect_change_snapshot,
    _collect_changed_paths,
    _collect_git_status_entries,
    _collect_staged_paths,
    _compact_json,
    _compact_log_text,
    _ensure_json_file,
    _ensure_text_file,
    _evaluate_meaningful_change,
    _extract_log_snippet,
    _extract_matching_lines,
    _has_open_stage_todo_task,
    _infer_auto_commit_type,
    _is_backlog_status_completed,
    _is_docs_path,
    _is_experiment_type_locked,
    _is_git_worktree,
    _load_json_if_exists,
    _meaningful_progress_detail,
    _normalize_backlog_status,
    _normalize_experiment_type,
    _normalize_space,
    _outcome_payload,
    _parse_run_id_timestamp,
    _parse_utc,
    _path_fingerprint,
    _path_matches_any,
    _persist_agent_result,
    _prepare_standard_commit_outcome,
    _read_json,
    _run_git,
    _safe_read_text,
    _safe_todo_post_sync,
    _safe_todo_pre_sync,
    _snapshot_delta_paths,
    _summarize_commit_paths,
    _summarize_git_changes_for_prompt,
    _todo_open_count,
    _try_auto_commit,
    _utc_now,
    _write_json,
    _append_todo_message,
    _manifest_timestamp,
)

from autolab.state import (  # noqa: F401
    _acquire_lock,
    _bootstrap_iteration_id,
    _default_agent_result,
    _default_state,
    _ensure_iteration_skeleton,
    _find_backlog_experiment_entry,
    _heartbeat_lock,
    _infer_unique_experiment_id_from_backlog,
    _is_active_experiment_completed,
    _load_backlog_yaml,
    _load_state,
    _mark_backlog_experiment_completed,
    _normalize_state,
    _parse_iteration_from_backlog,
    _release_lock,
    _resolve_autolab_dir,
    _resolve_experiment_type_from_backlog,
    _resolve_iteration_directory,
    _resolve_repo_root,
    _resolve_scaffold_source,
    _sync_scaffold_bundle,
    _write_backlog_yaml,
)

from autolab.config import (  # noqa: F401
    _load_agent_runner_config,
    _load_auto_commit_config,
    _load_guardrail_config,
    _load_meaningful_change_config,
    _load_verifier_policy,
    _resolve_policy_command,
    _resolve_policy_python_bin,
    _resolve_run_agent_mode,
    _resolve_stage_requirements,
)

from autolab.validators import (  # noqa: F401
    _load_dict_json,
    _require_non_empty,
    _run_verification_step,
    _validate_design,
    _validate_extract,
    _validate_launch,
    _validate_review_result,
    _validate_update_docs,
)

from autolab.evaluate import (  # noqa: F401
    _evaluate_stage,
)

from autolab.prompts import (  # noqa: F401
    _build_prompt_context,
    _build_runtime_stage_context_block,
    _context_token_values,
    _default_stage_prompt_text,
    _render_prompt_includes,
    _render_stage_prompt,
    _resolve_hypothesis_id,
    _resolve_prompt_run_id,
    _resolve_prompt_shared_path,
    _resolve_stage_prompt_path,
)

from autolab.runners import (  # noqa: F401
    _invoke_agent_runner,
)

from autolab.run_standard import (  # noqa: F401
    _handle_stage_failure,
    _run_once_standard,
)

from autolab.run_assistant import (  # noqa: F401
    _assistant_target_stage,
    _run_once_assistant,
)

from autolab.commands import (  # noqa: F401
    _build_parser,
    _cmd_init,
    _cmd_loop,
    _cmd_reset,
    _cmd_run,
    _cmd_slurm_job_list,
    _cmd_status,
    _cmd_sync_scaffold,
    _cmd_review,
    _run_once,
    _write_overnight_summary,
    main,
)

if __name__ == "__main__":
    raise SystemExit(main())
