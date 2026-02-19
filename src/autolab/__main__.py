"""Autolab CLI entry point — delegates to submodules."""

from __future__ import annotations

import importlib
from typing import Any

# Re-export public API for backward compatibility.
# Tests and external consumers import symbols from autolab.__main__,
# so every historically importable name remains available. Names are loaded
# lazily to avoid import-time cost for CLI startup and tooling.
_EXPORTS_BY_MODULE: dict[str, tuple[str, ...]] = {
    "autolab.constants": (
        "ACTIVE_STAGES",
        "AGENT_RUNNER_CLAUDE_DANGEROUS_PRESET",
        "AGENT_RUNNER_CODEX_DANGEROUS_PRESET",
        "AGENT_RUNNER_EDIT_SCOPE_MODES",
        "AGENT_RUNNER_PRESETS",
        "ALL_STAGES",
        "ASSISTANT_CONTROL_COMMIT_PATHS",
        "ASSISTANT_CYCLE_STAGES",
        "AUTO_COMMIT_MODES",
        "BACKLOG_COMPLETED_STATUSES",
        "DECISION_STAGES",
        "DEFAULT_AGENT_RUNNER_COMMAND",
        "DEFAULT_AGENT_RUNNER_CORE_DIRS",
        "DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE",
        "DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR",
        "DEFAULT_AGENT_RUNNER_NAME",
        "DEFAULT_AGENT_RUNNER_STAGES",
        "DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS",
        "DEFAULT_AUTO_COMMIT_MODE",
        "DEFAULT_BACKLOG_TEMPLATE",
        "DEFAULT_EXPERIMENT_TYPE",
        "DEFAULT_MAX_HOURS",
        "DEFAULT_MEANINGFUL_EXCLUDE_PATHS",
        "DEFAULT_VERIFIER_POLICY",
        "EXPERIMENT_LOCKED_TYPES",
        "EXPERIMENT_TYPES",
        "HOST_MODE_COMMAND_TIMEOUT_SECONDS",
        "ITERATION_ID_SAFE_PATTERN",
        "LOCK_STALE_SECONDS",
        "PACKAGE_SCAFFOLD_DIR",
        "PROMPT_LITERAL_TOKENS",
        "PROMPT_REQUIRED_TOKENS_BY_STAGE",
        "PROMPT_SHARED_INCLUDE_PATTERN",
        "PROMPT_TOKEN_PATTERN",
        "REVIEW_RESULT_CHECK_STATUSES",
        "REVIEW_RESULT_REQUIRED_CHECKS",
        "RUN_ID_TIMESTAMP_PATTERN",
        "RUNNER_ELIGIBLE_STAGES",
        "SLURM_JOB_LIST_PATH",
        "STAGE_PROMPT_FILES",
        "TERMINAL_STAGES",
        "VERIFIER_COMMAND_TIMEOUT_SECONDS",
    ),
    "autolab.models": (
        "AgentRunnerConfig",
        "AgentRunnerEditScopeConfig",
        "AutoCommitConfig",
        "EvalResult",
        "GuardrailConfig",
        "MeaningfulChangeConfig",
        "RenderedPromptBundle",
        "RunOutcome",
        "StageCheckError",
        "StateError",
        "_coerce_bool",
        "_coerce_float",
        "_coerce_positive_int",
    ),
    "autolab.utils": (
        "_append_log",
        "_assistant_commit_paths",
        "_build_auto_commit_message",
        "_detect_host_mode_with_probe",
        "_detect_priority_host_mode",
        "_collect_change_snapshot",
        "_collect_changed_paths",
        "_collect_git_status_entries",
        "_collect_staged_paths",
        "_compact_json",
        "_compact_log_text",
        "_ensure_json_file",
        "_ensure_text_file",
        "_evaluate_meaningful_change",
        "_extract_log_snippet",
        "_extract_matching_lines",
        "_generate_run_id",
        "_has_open_stage_todo_task",
        "_infer_auto_commit_type",
        "_is_backlog_status_completed",
        "_is_docs_path",
        "_is_experiment_type_locked",
        "_is_git_worktree",
        "_load_json_if_exists",
        "_meaningful_progress_detail",
        "_normalize_backlog_status",
        "_normalize_experiment_type",
        "_normalize_space",
        "_outcome_payload",
        "_parse_run_id_timestamp",
        "_parse_utc",
        "_path_fingerprint",
        "_path_matches_any",
        "_persist_agent_result",
        "_prepare_standard_commit_outcome",
        "_read_json",
        "_run_git",
        "_safe_read_text",
        "_safe_todo_post_sync",
        "_safe_todo_pre_sync",
        "_snapshot_delta_paths",
        "_summarize_commit_paths",
        "_summarize_git_changes_for_prompt",
        "_todo_open_count",
        "_try_auto_commit",
        "_utc_now",
        "_write_json",
        "_append_todo_message",
        "_manifest_timestamp",
    ),
    "autolab.state": (
        "_acquire_lock",
        "_append_state_history",
        "_bootstrap_iteration_id",
        "_default_agent_result",
        "_default_state",
        "_ensure_iteration_skeleton",
        "_find_backlog_experiment_entry",
        "_heartbeat_lock",
        "_infer_unique_experiment_id_from_backlog",
        "_is_active_experiment_completed",
        "_load_backlog_yaml",
        "_load_state",
        "_mark_backlog_experiment_completed",
        "_normalize_state",
        "_parse_iteration_from_backlog",
        "_release_lock",
        "_resolve_autolab_dir",
        "_resolve_experiment_type_from_backlog",
        "_resolve_iteration_directory",
        "_resolve_repo_root",
        "_resolve_scaffold_source",
        "_sync_scaffold_bundle",
        "_write_backlog_yaml",
    ),
    "autolab.config": (
        "_load_agent_runner_config",
        "_load_auto_commit_config",
        "_load_guardrail_config",
        "_load_meaningful_change_config",
        "_load_verifier_policy",
        "_resolve_policy_command",
        "_resolve_policy_python_bin",
        "_resolve_run_agent_mode",
        "_resolve_stage_requirements",
    ),
    "autolab.validators": (
        "_load_dict_json",
        "_require_non_empty",
        "_run_verification_step",
        "_run_verification_step_detailed",
        "_validate_stage_readiness",
        "_validate_design",
        "_validate_extract",
        "_validate_launch",
        "_validate_review_result",
        "_validate_update_docs",
    ),
    "autolab.evaluate": ("_evaluate_stage",),
    "autolab.prompts": (
        "_build_prompt_context",
        "_build_runtime_stage_context_block",
        "_context_token_values",
        "_default_stage_prompt_text",
        "_render_prompt_includes",
        "_render_stage_prompt",
        "_resolve_hypothesis_id",
        "_resolve_prompt_run_id",
        "_resolve_prompt_shared_path",
        "_resolve_stage_prompt_path",
    ),
    "autolab.runners": ("_invoke_agent_runner",),
    "autolab.run_standard": ("_handle_stage_failure", "_run_once_standard"),
    "autolab.run_assistant": ("_assistant_target_stage", "_run_once_assistant"),
    "autolab.commands": (
        "_build_parser",
        "_cmd_init",
        "_cmd_install_skill",
        "_cmd_loop",
        "_cmd_policy_apply_preset",
        "_cmd_reset",
        "_cmd_run",
        "_cmd_verify",
        "_cmd_slurm_job_list",
        "_cmd_status",
        "_cmd_sync_scaffold",
        "_cmd_review",
        "_list_bundled_skills",
        "_run_once",
        "_write_overnight_summary",
        "main",
    ),
}

_NAME_TO_MODULE: dict[str, str] = {}
for _module_name, _names in _EXPORTS_BY_MODULE.items():
    for _name in _names:
        _NAME_TO_MODULE[_name] = _module_name

__all__ = tuple(sorted(_NAME_TO_MODULE))


def __getattr__(name: str) -> Any:
    module_name = _NAME_TO_MODULE.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

    module = importlib.import_module(module_name)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))


def main(argv: list[str] | None = None) -> int:
    from autolab.commands import main as commands_main

    return commands_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
