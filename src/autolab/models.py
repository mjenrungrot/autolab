"""Autolab data models — exceptions, dataclasses, and coercion helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _coerce_bool(value: Any, *, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return bool(value) if value is not None else default


def _coerce_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return default


def _coerce_positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        return default
    return parsed if parsed > 0 else default


class StateError(RuntimeError):
    """Raised when state cannot be loaded or validated."""


class StageCheckError(RuntimeError):
    """Raised when a stage fails lightweight checks."""


@dataclass(frozen=True)
class RunOutcome:
    exit_code: int
    transitioned: bool
    stage_before: str
    stage_after: str
    message: str
    pause_reason: str = ""
    commit_allowed: bool = True
    commit_task_id: str = ""
    commit_cycle_stage: str = ""
    commit_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalResult:
    """Structured return from _evaluate_stage, replacing raw tuple."""

    next_stage: str
    status: str  # "complete" | "failed"
    summary: str  # human-readable message
    needs_retry: bool = False  # structured flag replacing string matching


@dataclass(frozen=True)
class AgentRunnerConfig:
    runner: str
    enabled: bool
    command: str
    stages: tuple[str, ...]
    edit_scope: "AgentRunnerEditScopeConfig"
    timeout_seconds: float
    claude_dangerously_skip_permissions: bool = True
    codex_dangerously_bypass_approvals_and_sandbox: bool = True


@dataclass(frozen=True)
class AgentRunnerEditScopeConfig:
    mode: str
    core_dirs: tuple[str, ...]
    ensure_iteration_dir: bool


@dataclass(frozen=True)
class RenderedPromptBundle:
    template_path: Path
    rendered_path: Path
    context_path: Path
    prompt_text: str
    context_payload: dict[str, Any]
    audit_template_path: Path | None = None
    audit_path: Path | None = None
    brief_template_path: Path | None = None
    brief_path: Path | None = None
    human_template_path: Path | None = None
    human_path: Path | None = None
    audit_text: str = ""
    brief_text: str = ""
    human_text: str = ""


@dataclass(frozen=True)
class GuardrailConfig:
    max_same_decision_streak: int
    max_no_progress_decisions: int
    max_update_docs_cycles: int
    max_generated_todo_tasks: int
    on_breach: str
    max_stalled_blocker_cycles: int = 3
    max_consecutive_errors: int = 5
    error_backoff_base_seconds: float = 10.0


@dataclass(frozen=True)
class CampaignComparisonConfig:
    complexity_proxy: str
    change_size_metric: str


@dataclass(frozen=True)
class MeaningfulChangeConfig:
    require_verification: bool
    require_implementation_progress: bool
    require_git_for_progress: bool
    on_non_git_behavior: str
    exclude_paths: tuple[str, ...]
    require_non_review_progress_in_implementation_cycle: bool = True
    implementation_cycle_exclude_paths: tuple[str, ...] = (
        ".autolab/**",
        "docs/todo.md",
        "**/implementation_review.md",
        "**/review_result.json",
    )


@dataclass(frozen=True)
class StrictModeConfig:
    forbid_auto_stop: bool
    require_human_review_for_stop: bool


@dataclass(frozen=True)
class AutoCommitConfig:
    mode: str


@dataclass(frozen=True)
class LaunchRuntimeConfig:
    execute: bool
    script_generation: str
    local_timeout_seconds: float
    slurm_submit_timeout_seconds: float


@dataclass(frozen=True)
class SlurmMonitorRuntimeConfig:
    poll_command_template: str
    poll_timeout_seconds: float
    sync_command_template: str
    sync_timeout_seconds: float


@dataclass(frozen=True)
class ExtractRuntimeConfig:
    require_parser_hook: bool
    summary_mode: str
    summary_llm_command: str
    summary_llm_timeout_seconds: float


@dataclass(frozen=True)
class PlanApprovalPolicyConfig:
    enabled: bool
    require_for_project_wide_tasks: bool
    max_tasks_without_approval: int
    max_waves_without_approval: int
    max_project_wide_paths_without_approval: int
    require_after_retries: bool


@dataclass(frozen=True)
class PlanExecutionImplementationConfig:
    enabled: bool
    run_unit: str
    max_parallel_tasks: int
    task_retry_max: int
    wave_retry_max: int
    failure_mode: str
    on_wave_retry_exhausted: str
    require_verification_commands: bool
    approval: PlanApprovalPolicyConfig = field(
        default_factory=lambda: PlanApprovalPolicyConfig(
            enabled=True,
            require_for_project_wide_tasks=True,
            max_tasks_without_approval=6,
            max_waves_without_approval=2,
            max_project_wide_paths_without_approval=3,
            require_after_retries=True,
        )
    )


@dataclass(frozen=True)
class OverlaySource:
    layer: str  # "scaffold_default", "preset", "host", "scope", "stage", "risk", "repo_local"
    name: str  # e.g., "local_dev", "slurm", "project_wide", ""
    keys_contributed: tuple[str, ...]


@dataclass(frozen=True)
class EffectivePolicyResult:
    merged: dict[str, Any]
    sources: tuple[OverlaySource, ...]
    preset: str
    host_mode: str
    scope_kind: str
    stage: str
    profile_mode: str
    risk_flags: dict[str, bool]


@dataclass(frozen=True)
class RemoteHostDetectionConfig:
    require_commands: tuple[str, ...]


@dataclass(frozen=True)
class RemoteGitSyncConfig:
    revision_source: str
    require_clean_worktree: bool
    fetch_command: str
    checkout_command: str


@dataclass(frozen=True)
class RemoteArtifactPullConfig:
    enabled: bool
    allow_patterns: tuple[str, ...]
    max_file_size_mb: float


@dataclass(frozen=True)
class RemoteDataPolicyConfig:
    local_sync: str
    deny_patterns: tuple[str, ...]


@dataclass(frozen=True)
class RemoteEnvConfig:
    cache_vars: dict[str, str]


@dataclass(frozen=True)
class RemoteProfileConfig:
    name: str
    mode: str
    enabled_for_host_modes: tuple[str, ...]
    login_host: str
    remote_repo_root: str
    bootstrap_command: str
    python_path: str
    submit_command: str
    host_detection: RemoteHostDetectionConfig
    git_sync: RemoteGitSyncConfig
    artifact_pull: RemoteArtifactPullConfig
    data_policy: RemoteDataPolicyConfig
    env: RemoteEnvConfig
    smoke_command: str = ""


@dataclass(frozen=True)
class RemoteProfilesConfig:
    schema_version: str
    path: Path
    default_profile: str
    profiles: dict[str, RemoteProfileConfig]


@dataclass(frozen=True)
class RevisionLabelInfo:
    label: str
    source: str
    dirty: bool


@dataclass(frozen=True)
class PlanExecutionConfig:
    implementation: PlanExecutionImplementationConfig
