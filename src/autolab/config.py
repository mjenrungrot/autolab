from __future__ import annotations
import os
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.constants import (
    ACTIVE_STAGES,
    AGENT_RUNNER_CLAUDE_DANGEROUS_PRESET,
    AGENT_RUNNER_EDIT_SCOPE_MODES,
    AGENT_RUNNER_PRESETS,
    AUTO_COMMIT_MODES,
    DEFAULT_AGENT_RUNNER_COMMAND,
    DEFAULT_AGENT_RUNNER_CORE_DIRS,
    DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE,
    DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR,
    DEFAULT_AGENT_RUNNER_NAME,
    DEFAULT_AGENT_RUNNER_STAGES,
    DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
    DEFAULT_AUTO_COMMIT_MODE,
    DEFAULT_MEANINGFUL_EXCLUDE_PATHS,
    RUNNER_ELIGIBLE_STAGES,
    TERMINAL_STAGES,
)
from autolab.models import (
    AgentRunnerConfig,
    AgentRunnerEditScopeConfig,
    AutoCommitConfig,
    GuardrailConfig,
    MeaningfulChangeConfig,
    StageCheckError,
    StrictModeConfig,
    _coerce_bool,
)


def _load_verifier_policy(repo_root: Path) -> dict[str, Any]:
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    if yaml is None or not policy_path.exists():
        return {}
    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _load_guardrail_config(repo_root: Path) -> GuardrailConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    guardrails = autorun.get("guardrails") if isinstance(autorun, dict) else {}
    if not isinstance(guardrails, dict):
        guardrails = {}
    max_same = int(guardrails.get("max_same_decision_streak", 3) or 3)
    max_no_progress = int(guardrails.get("max_no_progress_decisions", 2) or 2)
    max_update_docs = int(guardrails.get("max_update_docs_cycles", 3) or 3)
    max_generated_todo_tasks = int(guardrails.get("max_generated_todo_tasks", 5) or 5)
    on_breach = str(guardrails.get("on_breach", "human_review")).strip() or "human_review"
    if on_breach not in TERMINAL_STAGES:
        on_breach = "human_review"
    if max_same < 1:
        max_same = 1
    if max_no_progress < 1:
        max_no_progress = 1
    if max_update_docs < 1:
        max_update_docs = 1
    if max_generated_todo_tasks < 1:
        max_generated_todo_tasks = 1
    return GuardrailConfig(
        max_same_decision_streak=max_same,
        max_no_progress_decisions=max_no_progress,
        max_update_docs_cycles=max_update_docs,
        max_generated_todo_tasks=max_generated_todo_tasks,
        on_breach=on_breach,
    )


def _load_meaningful_change_config(repo_root: Path) -> MeaningfulChangeConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    meaningful = autorun.get("meaningful_change") if isinstance(autorun, dict) else {}
    if not isinstance(meaningful, dict):
        meaningful = {}
    require_verification = bool(meaningful.get("require_verification", True))
    require_implementation_progress = bool(
        meaningful.get("require_implementation_progress", True)
    )
    require_git_for_progress = bool(meaningful.get("require_git_for_progress", True))
    on_non_git_behavior = str(
        meaningful.get("on_non_git_behavior", "warn_and_continue")
    ).strip().lower()
    if on_non_git_behavior not in {"warn_and_continue", "fail"}:
        on_non_git_behavior = "warn_and_continue"
    raw_patterns = meaningful.get("exclude_paths", list(DEFAULT_MEANINGFUL_EXCLUDE_PATHS))
    patterns: list[str] = []
    if isinstance(raw_patterns, list):
        for entry in raw_patterns:
            candidate = str(entry).strip()
            if candidate:
                patterns.append(candidate)
    if not patterns:
        patterns = list(DEFAULT_MEANINGFUL_EXCLUDE_PATHS)
    return MeaningfulChangeConfig(
        require_verification=require_verification,
        require_implementation_progress=require_implementation_progress,
        require_git_for_progress=require_git_for_progress,
        on_non_git_behavior=on_non_git_behavior,
        exclude_paths=tuple(patterns),
    )


def _load_strict_mode_config(repo_root: Path) -> StrictModeConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    strict = autorun.get("strict_mode") if isinstance(autorun, dict) else {}
    if not isinstance(strict, dict):
        strict = {}
    return StrictModeConfig(
        forbid_auto_stop=_coerce_bool(strict.get("forbid_auto_stop"), default=False),
        require_human_review_for_stop=_coerce_bool(
            strict.get("require_human_review_for_stop"), default=False
        ),
    )


def _load_assistant_auto_complete_policy(repo_root: Path) -> bool:
    """Return whether assistant mode should auto-complete when no tasks remain.

    Default is True for backwards compatibility.
    """
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    assistant = autorun.get("assistant") if isinstance(autorun, dict) else {}
    if not isinstance(assistant, dict):
        return True
    return bool(assistant.get("auto_complete_backlog", True))


def _load_auto_commit_config(repo_root: Path) -> AutoCommitConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    auto_commit = autorun.get("auto_commit") if isinstance(autorun, dict) else {}
    if not isinstance(auto_commit, dict):
        auto_commit = {}
    mode = str(auto_commit.get("mode", DEFAULT_AUTO_COMMIT_MODE)).strip().lower()
    if mode not in AUTO_COMMIT_MODES:
        mode = DEFAULT_AUTO_COMMIT_MODE
    return AutoCommitConfig(mode=mode)


def _default_agent_runner_edit_scope() -> AgentRunnerEditScopeConfig:
    return AgentRunnerEditScopeConfig(
        mode=DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE,
        core_dirs=DEFAULT_AGENT_RUNNER_CORE_DIRS,
        ensure_iteration_dir=DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR,
    )


def _load_agent_runner_edit_scope(runner: dict[str, Any]) -> AgentRunnerEditScopeConfig:
    raw_edit_scope = runner.get("edit_scope")
    if raw_edit_scope is None:
        return _default_agent_runner_edit_scope()
    if not isinstance(raw_edit_scope, dict):
        raise StageCheckError("agent_runner.edit_scope must be a mapping")

    mode = str(raw_edit_scope.get("mode", DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE)).strip().lower()
    if mode not in AGENT_RUNNER_EDIT_SCOPE_MODES:
        raise StageCheckError(
            f"agent_runner.edit_scope.mode must be one of {', '.join(AGENT_RUNNER_EDIT_SCOPE_MODES)}"
        )

    raw_core_dirs = raw_edit_scope.get("core_dirs", list(DEFAULT_AGENT_RUNNER_CORE_DIRS))
    if raw_core_dirs is None:
        raw_core_dirs = list(DEFAULT_AGENT_RUNNER_CORE_DIRS)
    if not isinstance(raw_core_dirs, list):
        raise StageCheckError("agent_runner.edit_scope.core_dirs must be a list of repo-relative directory paths")
    core_dirs: list[str] = []
    for raw_dir in raw_core_dirs:
        value = str(raw_dir).strip()
        if not value:
            raise StageCheckError("agent_runner.edit_scope.core_dirs entries must be non-empty")
        if value not in core_dirs:
            core_dirs.append(value)

    ensure_iteration_dir = bool(
        raw_edit_scope.get("ensure_iteration_dir", DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR)
    )
    if mode == "iteration_only":
        core_dirs = []

    return AgentRunnerEditScopeConfig(
        mode=mode,
        core_dirs=tuple(core_dirs),
        ensure_iteration_dir=ensure_iteration_dir,
    )


def _load_agent_runner_config(repo_root: Path) -> AgentRunnerConfig:
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    if yaml is None or not policy_path.exists():
        return AgentRunnerConfig(
            runner=DEFAULT_AGENT_RUNNER_NAME,
            enabled=False,
            command=DEFAULT_AGENT_RUNNER_COMMAND,
            stages=DEFAULT_AGENT_RUNNER_STAGES,
            edit_scope=_default_agent_runner_edit_scope(),
            timeout_seconds=DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
            claude_dangerously_skip_permissions=False,
        )

    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(f"agent_runner policy could not be parsed at {policy_path}: {exc}") from exc

    if not isinstance(loaded, dict):
        return AgentRunnerConfig(
            runner=DEFAULT_AGENT_RUNNER_NAME,
            enabled=False,
            command=DEFAULT_AGENT_RUNNER_COMMAND,
            stages=DEFAULT_AGENT_RUNNER_STAGES,
            edit_scope=_default_agent_runner_edit_scope(),
            timeout_seconds=DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
            claude_dangerously_skip_permissions=False,
        )

    runner_section = loaded.get("agent_runner")
    if runner_section is None:
        return AgentRunnerConfig(
            runner=DEFAULT_AGENT_RUNNER_NAME,
            enabled=False,
            command=DEFAULT_AGENT_RUNNER_COMMAND,
            stages=DEFAULT_AGENT_RUNNER_STAGES,
            edit_scope=_default_agent_runner_edit_scope(),
            timeout_seconds=DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
            claude_dangerously_skip_permissions=False,
        )
    if not isinstance(runner_section, dict):
        raise StageCheckError("agent_runner policy must be a mapping")

    runner_name = str(runner_section.get("runner", DEFAULT_AGENT_RUNNER_NAME)).strip()
    valid_runners = set(AGENT_RUNNER_PRESETS) | {"custom"}
    if runner_name not in valid_runners:
        raise StageCheckError(
            f"agent_runner.runner must be one of {sorted(valid_runners)}, got '{runner_name}'"
        )

    enabled = bool(runner_section.get("enabled", False))
    raw_command = runner_section.get("command")
    claude_dangerous_opt_in = _coerce_bool(
        runner_section.get("claude_dangerously_skip_permissions"),
        default=False,
    ) or _coerce_bool(os.environ.get("AUTOLAB_CLAUDE_ALLOW_DANGEROUS"), default=False)
    if raw_command is not None:
        command = str(raw_command).strip()
    else:
        if runner_name == "claude" and claude_dangerous_opt_in:
            command = AGENT_RUNNER_CLAUDE_DANGEROUS_PRESET
        else:
            command = AGENT_RUNNER_PRESETS.get(runner_name, DEFAULT_AGENT_RUNNER_COMMAND)
    if enabled and not command:
        raise StageCheckError("agent_runner.command must be set when agent_runner.enabled is true")

    raw_stages = runner_section.get("stages")
    if raw_stages is None:
        stages = list(DEFAULT_AGENT_RUNNER_STAGES)
    else:
        if not isinstance(raw_stages, list):
            raise StageCheckError("agent_runner.stages must be a list of stage names")
        stages = []
        for raw_stage in raw_stages:
            stage = str(raw_stage).strip()
            if stage not in RUNNER_ELIGIBLE_STAGES:
                raise StageCheckError(f"agent_runner.stages includes unsupported stage '{stage}'")
            if stage not in stages:
                stages.append(stage)
    if enabled and not stages:
        raise StageCheckError("agent_runner.stages must include at least one active stage")

    raw_timeout = runner_section.get("timeout_seconds", DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS)
    try:
        timeout_seconds = float(raw_timeout)
    except Exception as exc:
        raise StageCheckError("agent_runner.timeout_seconds must be a non-negative number") from exc
    if timeout_seconds < 0:
        raise StageCheckError("agent_runner.timeout_seconds must be >= 0")
    if timeout_seconds == 0:
        timeout_seconds = DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS

    edit_scope = _load_agent_runner_edit_scope(runner_section)

    return AgentRunnerConfig(
        runner=runner_name,
        enabled=enabled,
        command=command,
        stages=tuple(stages),
        edit_scope=edit_scope,
        timeout_seconds=timeout_seconds,
        claude_dangerously_skip_permissions=claude_dangerous_opt_in,
    )


def _load_protected_files(policy: dict[str, Any]) -> list[str]:
    """Return normalized protected file paths from verifier policy.

    Supports:
    - ``protected_files`` legacy list
    - ``protected_file_profiles`` with optional ``protected_profile`` selector
    - ``safe_automation_protected_files`` toggle (profile: ``safe_automation``)
    """
    def _normalize_list(raw_values: Any) -> list[str]:
        if not isinstance(raw_values, list):
            return []
        normalized_paths: list[str] = []
        for raw_entry in raw_values:
            normalized = str(raw_entry).strip().replace("\\", "/")
            if normalized and normalized not in normalized_paths:
                normalized_paths.append(normalized)
        return normalized_paths

    result = _normalize_list(policy.get("protected_files", []))

    profile_name = str(policy.get("protected_profile", "default")).strip() or "default"
    if _coerce_bool(policy.get("safe_automation_protected_files"), default=False):
        profile_name = "safe_automation"

    profile_map = policy.get("protected_file_profiles", {})
    if isinstance(profile_map, dict):
        profile_values = profile_map.get(profile_name)
        if isinstance(profile_values, list):
            for path in _normalize_list(profile_values):
                if path not in result:
                    result.append(path)

    safe_profile = policy.get("safe_automation_protected_files_list")
    if _coerce_bool(policy.get("safe_automation_protected_files"), default=False):
        for path in _normalize_list(safe_profile):
            if path not in result:
                result.append(path)

    return result


def _resolve_run_agent_mode(mode_value: str | None) -> str:
    candidate = str(mode_value or "policy").strip().lower()
    if candidate in {"policy", "force_on", "force_off"}:
        return candidate
    return "policy"


def _resolve_policy_python_bin(policy: dict[str, Any]) -> str:
    value = str(policy.get("python_bin", "python3")).strip()
    return value or "python3"


def _resolve_stage_requirements(
    policy: dict[str, Any],
    stage: str,
    *,
    registry_verifier_categories: dict[str, bool] | None = None,
) -> dict[str, bool]:
    requirements: dict[str, bool] = {
        "tests": False,
        "dry_run": False,
        "schema": False,
        "prompt_lint": False,
        "consistency": False,
        "env_smoke": False,
        "docs_target_update": False,
    }

    # Layer 1: registry verifier_categories from workflow.yaml (base defaults).
    if isinstance(registry_verifier_categories, dict):
        for key in requirements:
            if key in registry_verifier_categories:
                requirements[key] = bool(registry_verifier_categories[key])

    # Layer 2: legacy top-level policy keys (backward-compat).
    legacy_mapping = {
        "tests": "require_tests",
        "dry_run": "require_dry_run",
        "schema": "require_schema",
        "prompt_lint": "require_prompt_lint",
        "consistency": "require_consistency",
        "env_smoke": "require_env_smoke",
        "docs_target_update": "require_docs_target_update",
    }
    for key, legacy_key in legacy_mapping.items():
        if legacy_key in policy:
            requirements[key] = _coerce_bool(policy.get(legacy_key), default=requirements[key])

    # Layer 3: per-stage policy overrides (highest priority).
    requirements_by_stage = policy.get("requirements_by_stage", {})
    if isinstance(requirements_by_stage, dict):
        stage_section = requirements_by_stage.get(stage, {})
        if isinstance(stage_section, dict):
            for key in requirements:
                if key in stage_section:
                    requirements[key] = _coerce_bool(stage_section.get(key), default=requirements[key])
    return requirements


def _resolve_stage_max_retries(policy: dict[str, Any], stage: str, *, fallback: int = 5) -> int:
    """Return the per-stage max_retries from retry_policy_by_stage.

    Falls back to *fallback* (typically the global max_stage_attempts) when the
    stage is not configured or the section is missing.
    """
    retry_section = policy.get("retry_policy_by_stage", {})
    if isinstance(retry_section, dict):
        stage_config = retry_section.get(stage)
        if isinstance(stage_config, dict):
            val = stage_config.get("max_retries")
            if val is not None:
                return int(val)
    return fallback


def _resolve_policy_command(raw: str, *, python_bin: str) -> str:
    command = str(raw).strip()
    if not command:
        return ""
    return command.replace("<PYTHON_BIN>", python_bin).replace("{{python_bin}}", python_bin)
