from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:
    yaml = None

from autolab.constants import (
    ACTIVE_STAGES,
    AGENT_RUNNER_CLAUDE_DANGEROUS_PRESET,
    AGENT_RUNNER_CODEX_DANGEROUS_PRESET,
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
    DEFAULT_IMPLEMENTATION_CYCLE_EXCLUDE_PATHS,
    DEFAULT_MEANINGFUL_EXCLUDE_PATHS,
    DEFAULT_PLAN_EXECUTION_ENABLED,
    DEFAULT_PLAN_EXECUTION_FAILURE_MODE,
    DEFAULT_PLAN_EXECUTION_MAX_PARALLEL_TASKS,
    DEFAULT_PLAN_EXECUTION_ON_WAVE_RETRY_EXHAUSTED,
    DEFAULT_PLAN_EXECUTION_REQUIRE_VERIFICATION_COMMANDS,
    DEFAULT_PLAN_EXECUTION_RUN_UNIT,
    DEFAULT_PLAN_EXECUTION_TASK_RETRY_MAX,
    DEFAULT_PLAN_EXECUTION_WAVE_RETRY_MAX,
    DEFAULT_PLAN_APPROVAL_ENABLED,
    DEFAULT_PLAN_APPROVAL_REQUIRE_FOR_PROJECT_WIDE_TASKS,
    DEFAULT_PLAN_APPROVAL_MAX_TASKS_WITHOUT_APPROVAL,
    DEFAULT_PLAN_APPROVAL_MAX_WAVES_WITHOUT_APPROVAL,
    DEFAULT_PLAN_APPROVAL_MAX_PROJECT_WIDE_PATHS_WITHOUT_APPROVAL,
    DEFAULT_PLAN_APPROVAL_REQUIRE_AFTER_RETRIES,
    DEFAULT_PROFILE_MODE,
    DEFAULT_UAT_SURFACE_PATTERNS,
    PACKAGE_SCAFFOLD_DIR,
    PLAN_EXECUTION_FAILURE_MODES,
    PLAN_EXECUTION_RUN_UNITS,
    RUNNER_ELIGIBLE_STAGES,
    TERMINAL_STAGES,
)
from autolab.models import (
    AgentRunnerConfig,
    AgentRunnerEditScopeConfig,
    AutoCommitConfig,
    CampaignComparisonConfig,
    CampaignGovernanceConfig,
    EffectivePolicyResult,
    ExtractRuntimeConfig,
    GuardrailConfig,
    LaunchRuntimeConfig,
    MeaningfulChangeConfig,
    OracleApplyPolicyConfig,
    OraclePolicyConfig,
    OracleTriggerConfig,
    OverlaySource,
    PlanApprovalPolicyConfig,
    PlanExecutionConfig,
    PlanExecutionImplementationConfig,
    SlurmMonitorRuntimeConfig,
    StageCheckError,
    StrictModeConfig,
    _coerce_bool,
    _coerce_float,
)
from autolab.policy_resolution import (
    derive_risk_flags,
    extract_overlay,
    resolve_effective_policy,
)
from autolab.remote_profiles import normalize_profile_mode


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


def _load_effective_policy(
    repo_root: Path,
    *,
    host_mode: str = "",
    scope_kind: str = "",
    stage: str = "",
) -> EffectivePolicyResult:
    """Compute the effective policy by merging all overlay layers."""
    raw_policy = _load_verifier_policy(repo_root)

    # Read policy_resolution config
    resolution_cfg = raw_policy.get("policy_resolution")
    if not isinstance(resolution_cfg, dict):
        resolution_cfg = {}
    preset_name = str(resolution_cfg.get("default_preset", "")).strip()

    # Load scaffold defaults
    scaffold_policy_path = PACKAGE_SCAFFOLD_DIR / "verifier_policy.yaml"
    scaffold_defaults: dict[str, Any] = {}
    if yaml is not None and scaffold_policy_path.exists():
        try:
            loaded = yaml.safe_load(scaffold_policy_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                scaffold_defaults = loaded
        except Exception:
            pass

    # Load preset from scaffold bundle
    preset_policy: dict[str, Any] = {}
    if preset_name:
        preset_path = PACKAGE_SCAFFOLD_DIR / "policy" / f"{preset_name}.yaml"
        if yaml is not None and preset_path.exists():
            try:
                loaded = yaml.safe_load(preset_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    preset_policy = loaded
            except Exception:
                pass

    # Extract dimensional overlays
    host_overlay = extract_overlay(raw_policy, "host", host_mode) if host_mode else {}
    scope_overlay = (
        extract_overlay(raw_policy, "scope", scope_kind) if scope_kind else {}
    )
    stage_overlay = extract_overlay(raw_policy, "stage", stage) if stage else {}
    risk_overlay = extract_overlay(raw_policy, "risk", "")

    # Prepare repo-local overrides (strip meta sections)
    repo_local = dict(raw_policy)
    for meta_key in ("policy_resolution", "policy_overlays"):
        repo_local.pop(meta_key, None)

    # Read profile_mode and UAT patterns
    profile_mode = normalize_profile_mode(
        str(raw_policy.get("profile_mode", DEFAULT_PROFILE_MODE)).strip()
    )
    if not profile_mode:
        profile_mode = DEFAULT_PROFILE_MODE

    raw_uat = raw_policy.get("uat_surface_patterns")
    if isinstance(raw_uat, list):
        uat_patterns = [str(p).strip() for p in raw_uat if str(p).strip()]
    else:
        uat_patterns = list(DEFAULT_UAT_SURFACE_PATTERNS)

    resolved_scope_kind = scope_kind or "experiment"
    project_wide_unique_paths: list[str] = []
    if not scope_kind or not stage:
        try:
            from autolab.state import (
                _load_state,
                _normalize_state,
                _resolve_iteration_directory,
            )
            from autolab.uat import resolve_project_wide_paths

            state_path = repo_root / ".autolab" / "state.json"
            if state_path.exists():
                state_payload = _normalize_state(_load_state(state_path))
                if not scope_kind:
                    resolved_scope_kind = (
                        str(state_payload.get("scope_kind", "")).strip()
                        or resolved_scope_kind
                    )
                iteration_id = str(state_payload.get("iteration_id", "")).strip()
                experiment_id = str(state_payload.get("experiment_id", "")).strip()
                if iteration_id:
                    iteration_dir, _ = _resolve_iteration_directory(
                        repo_root,
                        iteration_id=iteration_id,
                        experiment_id=experiment_id,
                        require_exists=False,
                    )
                    resolved_scope_kind, project_wide_unique_paths = (
                        resolve_project_wide_paths(
                            repo_root,
                            iteration_dir,
                        )
                    )
        except Exception:
            project_wide_unique_paths = []
    else:
        try:
            from autolab.state import (
                _load_state,
                _normalize_state,
                _resolve_iteration_directory,
            )
            from autolab.uat import resolve_project_wide_paths

            state_path = repo_root / ".autolab" / "state.json"
            if state_path.exists():
                state_payload = _normalize_state(_load_state(state_path))
                iteration_id = str(state_payload.get("iteration_id", "")).strip()
                experiment_id = str(state_payload.get("experiment_id", "")).strip()
                if iteration_id:
                    iteration_dir, _ = _resolve_iteration_directory(
                        repo_root,
                        iteration_id=iteration_id,
                        experiment_id=experiment_id,
                        require_exists=False,
                    )
                    _resolved_scope_kind, project_wide_unique_paths = (
                        resolve_project_wide_paths(
                            repo_root,
                            iteration_dir,
                        )
                    )
                    if not scope_kind:
                        resolved_scope_kind = _resolved_scope_kind
        except Exception:
            project_wide_unique_paths = []

    # Merge
    merged, raw_sources = resolve_effective_policy(
        scaffold_defaults=scaffold_defaults,
        preset_policy=preset_policy,
        host_overlay=host_overlay,
        scope_overlay=scope_overlay,
        stage_overlay=stage_overlay,
        risk_overlay=risk_overlay,
        repo_local_overrides=repo_local,
    )

    # Derive risk flags
    risk_flags = derive_risk_flags(
        host_mode=host_mode or "local",
        scope_kind=resolved_scope_kind,
        profile_mode=profile_mode,
        project_wide_unique_paths=project_wide_unique_paths,
        uat_surface_patterns=uat_patterns,
        plan_approval_required=False,
    )

    # Build OverlaySource tuples
    overlay_sources = tuple(
        OverlaySource(
            layer=layer,
            name=name,
            keys_contributed=tuple(keys),
        )
        for layer, name, keys in raw_sources
    )

    return EffectivePolicyResult(
        merged=merged,
        sources=overlay_sources,
        preset=preset_name,
        host_mode=host_mode or "local",
        scope_kind=resolved_scope_kind,
        stage=stage,
        profile_mode=profile_mode,
        risk_flags=risk_flags,
    )


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
    max_stalled_blocker_cycles = int(
        guardrails.get("max_stalled_blocker_cycles", 3) or 3
    )
    on_breach = (
        str(guardrails.get("on_breach", "human_review")).strip() or "human_review"
    )
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
    if max_stalled_blocker_cycles < 1:
        max_stalled_blocker_cycles = 1
    max_consecutive_errors = int(guardrails.get("max_consecutive_errors", 5) or 5)
    error_backoff_base_seconds = float(
        guardrails.get("error_backoff_base_seconds", 10.0) or 10.0
    )
    if max_consecutive_errors < 1:
        max_consecutive_errors = 1
    if error_backoff_base_seconds < 0:
        error_backoff_base_seconds = 0.0
    return GuardrailConfig(
        max_same_decision_streak=max_same,
        max_no_progress_decisions=max_no_progress,
        max_update_docs_cycles=max_update_docs,
        max_generated_todo_tasks=max_generated_todo_tasks,
        on_breach=on_breach,
        max_stalled_blocker_cycles=max_stalled_blocker_cycles,
        max_consecutive_errors=max_consecutive_errors,
        error_backoff_base_seconds=error_backoff_base_seconds,
    )


def _load_campaign_comparison_config(repo_root: Path) -> CampaignComparisonConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    campaign = autorun.get("campaign") if isinstance(autorun, dict) else {}
    if not isinstance(campaign, dict):
        campaign = {}

    complexity_proxy = (
        str(campaign.get("complexity_proxy", "changed_surface")).strip().lower()
        or "changed_surface"
    )
    if complexity_proxy not in {"changed_surface", "none"}:
        complexity_proxy = "changed_surface"

    change_size_metric = (
        str(campaign.get("change_size_metric", "files")).strip().lower() or "files"
    )
    if change_size_metric not in {"files", "lines", "chars"}:
        change_size_metric = "files"

    return CampaignComparisonConfig(
        complexity_proxy=complexity_proxy,
        change_size_metric=change_size_metric,
    )


def _load_campaign_governance_config(repo_root: Path) -> CampaignGovernanceConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    campaign = autorun.get("campaign") if isinstance(autorun, dict) else {}
    if not isinstance(campaign, dict):
        campaign = {}

    try:
        max_fix_attempts_per_idea = int(campaign.get("max_fix_attempts_per_idea", 2))
    except Exception:
        max_fix_attempts_per_idea = 2
    if max_fix_attempts_per_idea < 0:
        max_fix_attempts_per_idea = 0

    max_timeout_factor = _coerce_float(
        campaign.get("max_timeout_factor", 2.0),
        default=2.0,
    )
    if max_timeout_factor <= 0:
        max_timeout_factor = 2.0

    try:
        max_no_improvement_streak = int(campaign.get("max_no_improvement_streak", 3))
    except Exception:
        max_no_improvement_streak = 3
    if max_no_improvement_streak < 1:
        max_no_improvement_streak = 1

    try:
        max_crash_streak_before_rethink = int(
            campaign.get("max_crash_streak_before_rethink", 2)
        )
    except Exception:
        max_crash_streak_before_rethink = 2
    if max_crash_streak_before_rethink < 1:
        max_crash_streak_before_rethink = 1

    return CampaignGovernanceConfig(
        max_fix_attempts_per_idea=max_fix_attempts_per_idea,
        max_timeout_factor=max_timeout_factor,
        max_no_improvement_streak=max_no_improvement_streak,
        max_crash_streak_before_rethink=max_crash_streak_before_rethink,
    )


def _load_oracle_policy(
    repo_root: Path,
    *,
    host_mode: str = "",
    scope_kind: str = "",
    stage: str = "",
) -> OraclePolicyConfig:
    result = _load_effective_policy(
        repo_root,
        host_mode=host_mode,
        scope_kind=scope_kind,
        stage=stage,
    )
    oracle = result.merged.get("oracle")
    if not isinstance(oracle, dict):
        oracle = {}

    mode = str(oracle.get("mode", "browser_only")).strip().lower() or "browser_only"
    if mode != "browser_only":
        mode = "browser_only"

    try:
        max_auto_attempts_per_epoch = int(
            oracle.get("max_auto_attempts_per_epoch", 1) or 1
        )
    except Exception:
        max_auto_attempts_per_epoch = 1
    if max_auto_attempts_per_epoch < 1:
        max_auto_attempts_per_epoch = 1

    try:
        timeout_minutes = int(oracle.get("timeout_minutes", 60) or 60)
    except Exception:
        timeout_minutes = 60
    if timeout_minutes < 1:
        timeout_minutes = 60

    return OraclePolicyConfig(
        auto_allowed=_coerce_bool(oracle.get("auto_allowed", False), default=False),
        mode=mode,
        max_auto_attempts_per_epoch=max_auto_attempts_per_epoch,
        timeout_minutes=timeout_minutes,
        browser_model_strategy=str(
            oracle.get("browser_model_strategy", "current")
        ).strip()
        or "current",
        browser_manual_login_profile_required=_coerce_bool(
            oracle.get("browser_manual_login_profile_required", True),
            default=True,
        ),
        browser_auto_reattach_delay=str(
            oracle.get("browser_auto_reattach_delay", "30s")
        ).strip()
        or "30s",
        browser_auto_reattach_interval=str(
            oracle.get("browser_auto_reattach_interval", "2m")
        ).strip()
        or "2m",
        browser_auto_reattach_timeout=str(
            oracle.get("browser_auto_reattach_timeout", "2m")
        ).strip()
        or "2m",
        preview_before_send=_coerce_bool(
            oracle.get("preview_before_send", True),
            default=True,
        ),
        apply_on_success=_coerce_bool(
            oracle.get("apply_on_success", True),
            default=True,
        ),
        graceful_failure=_coerce_bool(
            oracle.get("graceful_failure", True),
            default=True,
        ),
    )


def _load_oracle_trigger_config(
    repo_root: Path,
    *,
    host_mode: str = "",
    scope_kind: str = "",
    stage: str = "",
) -> OracleTriggerConfig:
    result = _load_effective_policy(
        repo_root,
        host_mode=host_mode,
        scope_kind=scope_kind,
        stage=stage,
    )
    triggers = result.merged.get("oracle_triggers")
    if not isinstance(triggers, dict):
        triggers = {}

    try:
        no_improvement_streak = int(triggers.get("no_improvement_streak", 5) or 5)
    except Exception:
        no_improvement_streak = 5
    if no_improvement_streak < 1:
        no_improvement_streak = 5

    try:
        crash_streak = int(triggers.get("crash_streak", 3) or 3)
    except Exception:
        crash_streak = 3
    if crash_streak < 1:
        crash_streak = 3

    return OracleTriggerConfig(
        no_improvement_streak=no_improvement_streak,
        crash_streak=crash_streak,
        contradictory_evidence=_coerce_bool(
            triggers.get("contradictory_evidence", True),
            default=True,
        ),
        blocked_review=_coerce_bool(
            triggers.get("blocked_review", True),
            default=True,
        ),
        explicit_policy_request=_coerce_bool(
            triggers.get("explicit_policy_request", True),
            default=True,
        ),
    )


def _load_oracle_apply_policy(
    repo_root: Path,
    *,
    host_mode: str = "",
    scope_kind: str = "",
    stage: str = "",
) -> OracleApplyPolicyConfig:
    result = _load_effective_policy(
        repo_root,
        host_mode=host_mode,
        scope_kind=scope_kind,
        stage=stage,
    )
    apply_cfg = result.merged.get("oracle_apply")
    if not isinstance(apply_cfg, dict):
        apply_cfg = {}
    ingestion_mode = str(apply_cfg.get("ingestion_mode", "hybrid")).strip().lower()
    if ingestion_mode not in {"hybrid", "strict_only", "llm_only"}:
        ingestion_mode = "hybrid"
    llm_command = str(apply_cfg.get("llm_command", "")).strip()
    llm_timeout_seconds = _coerce_float(
        apply_cfg.get("llm_timeout_seconds"),
        default=300.0,
    )
    if llm_timeout_seconds <= 0:
        llm_timeout_seconds = 300.0

    return OracleApplyPolicyConfig(
        ingestion_mode=ingestion_mode,
        llm_command=llm_command,
        llm_timeout_seconds=llm_timeout_seconds,
        allow_continue_search=_coerce_bool(
            apply_cfg.get("allow_continue_search", True),
            default=True,
        ),
        allow_switch_family=_coerce_bool(
            apply_cfg.get("allow_switch_family", True),
            default=True,
        ),
        allow_rewind_design=_coerce_bool(
            apply_cfg.get("allow_rewind_design", False),
            default=False,
        ),
        allow_request_human_review=_coerce_bool(
            apply_cfg.get("allow_request_human_review", True),
            default=True,
        ),
        allow_stop_campaign=_coerce_bool(
            apply_cfg.get("allow_stop_campaign", True),
            default=True,
        ),
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
    on_non_git_behavior = (
        str(meaningful.get("on_non_git_behavior", "warn_and_continue")).strip().lower()
    )
    if on_non_git_behavior not in {"warn_and_continue", "fail"}:
        on_non_git_behavior = "warn_and_continue"
    raw_patterns = meaningful.get(
        "exclude_paths", list(DEFAULT_MEANINGFUL_EXCLUDE_PATHS)
    )
    patterns: list[str] = []
    if isinstance(raw_patterns, list):
        for entry in raw_patterns:
            candidate = str(entry).strip()
            if candidate:
                patterns.append(candidate)
    if not patterns:
        patterns = list(DEFAULT_MEANINGFUL_EXCLUDE_PATHS)
    require_non_review_progress_in_implementation_cycle = _coerce_bool(
        meaningful.get("require_non_review_progress_in_implementation_cycle"),
        default=True,
    )
    raw_implementation_cycle_patterns = meaningful.get(
        "implementation_cycle_exclude_paths",
        list(DEFAULT_IMPLEMENTATION_CYCLE_EXCLUDE_PATHS),
    )
    implementation_cycle_patterns: list[str] = []
    if isinstance(raw_implementation_cycle_patterns, list):
        for entry in raw_implementation_cycle_patterns:
            candidate = str(entry).strip()
            if candidate:
                implementation_cycle_patterns.append(candidate)
    if not implementation_cycle_patterns:
        implementation_cycle_patterns = list(DEFAULT_IMPLEMENTATION_CYCLE_EXCLUDE_PATHS)
    return MeaningfulChangeConfig(
        require_verification=require_verification,
        require_implementation_progress=require_implementation_progress,
        require_git_for_progress=require_git_for_progress,
        on_non_git_behavior=on_non_git_behavior,
        exclude_paths=tuple(patterns),
        require_non_review_progress_in_implementation_cycle=require_non_review_progress_in_implementation_cycle,
        implementation_cycle_exclude_paths=tuple(implementation_cycle_patterns),
    )


def _load_strict_mode_config(
    repo_root: Path, *, auto_mode: bool = False
) -> StrictModeConfig:
    policy = _load_verifier_policy(repo_root)
    autorun = policy.get("autorun")
    strict = autorun.get("strict_mode") if isinstance(autorun, dict) else {}
    if not isinstance(strict, dict):
        strict = {}
    raw_forbid_auto_stop = strict.get("forbid_auto_stop")
    raw_require_human_review_for_stop = strict.get("require_human_review_for_stop")
    require_human_review_default = bool(
        auto_mode and raw_require_human_review_for_stop is None
    )
    return StrictModeConfig(
        forbid_auto_stop=_coerce_bool(raw_forbid_auto_stop, default=False),
        require_human_review_for_stop=_coerce_bool(
            raw_require_human_review_for_stop,
            default=require_human_review_default,
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


def _load_launch_runtime_config(repo_root: Path) -> LaunchRuntimeConfig:
    """Return runtime launch execution configuration with safe defaults."""
    policy = _load_verifier_policy(repo_root)
    launch = policy.get("launch")
    if not isinstance(launch, dict):
        launch = {}

    execute = _coerce_bool(launch.get("execute"), default=True)
    script_generation = (
        str(launch.get("script_generation", "missing_only")).strip().lower()
    )
    if script_generation not in {"missing_only", "always"}:
        script_generation = "missing_only"

    local_timeout_seconds = _coerce_float(
        launch.get("local_timeout_seconds"), default=900.0
    )
    if local_timeout_seconds <= 0:
        local_timeout_seconds = 900.0

    slurm_submit_timeout_seconds = _coerce_float(
        launch.get("slurm_submit_timeout_seconds"),
        default=30.0,
    )
    if slurm_submit_timeout_seconds <= 0:
        slurm_submit_timeout_seconds = 30.0

    return LaunchRuntimeConfig(
        execute=execute,
        script_generation=script_generation,
        local_timeout_seconds=local_timeout_seconds,
        slurm_submit_timeout_seconds=slurm_submit_timeout_seconds,
    )


def _load_launch_execute_policy(repo_root: Path) -> bool:
    """Return whether launch stage is allowed to execute commands/submit jobs."""
    return _load_launch_runtime_config(repo_root).execute


def _load_slurm_monitor_runtime_config(repo_root: Path) -> SlurmMonitorRuntimeConfig:
    policy = _load_verifier_policy(repo_root)
    slurm = policy.get("slurm")
    if not isinstance(slurm, dict):
        slurm = {}
    monitor = slurm.get("monitor")
    if not isinstance(monitor, dict):
        monitor = {}

    poll_command_template = str(monitor.get("poll_command_template", "")).strip()
    poll_timeout_seconds = _coerce_float(
        monitor.get("poll_timeout_seconds"), default=30.0
    )
    if poll_timeout_seconds <= 0:
        poll_timeout_seconds = 30.0

    sync_command_template = str(monitor.get("sync_command_template", "")).strip()
    sync_timeout_seconds = _coerce_float(
        monitor.get("sync_timeout_seconds"), default=180.0
    )
    if sync_timeout_seconds <= 0:
        sync_timeout_seconds = 180.0

    return SlurmMonitorRuntimeConfig(
        poll_command_template=poll_command_template,
        poll_timeout_seconds=poll_timeout_seconds,
        sync_command_template=sync_command_template,
        sync_timeout_seconds=sync_timeout_seconds,
    )


def _load_extract_runtime_config(repo_root: Path) -> ExtractRuntimeConfig:
    policy = _load_verifier_policy(repo_root)
    extract = policy.get("extract_results")
    if not isinstance(extract, dict):
        extract = {}

    parser_block = extract.get("parser")
    if not isinstance(parser_block, dict):
        parser_block = {}
    require_parser_hook = _coerce_bool(parser_block.get("require_hook"), default=False)

    summary_block = extract.get("summary")
    if not isinstance(summary_block, dict):
        summary_block = {}
    summary_mode = str(summary_block.get("mode", "llm_on_demand")).strip().lower()
    if summary_mode not in {"llm_on_demand", "none"}:
        summary_mode = "llm_on_demand"

    summary_llm_command = str(summary_block.get("llm_command", "")).strip()
    summary_llm_timeout_seconds = _coerce_float(
        summary_block.get("llm_timeout_seconds"), default=300.0
    )
    if summary_llm_timeout_seconds <= 0:
        summary_llm_timeout_seconds = 300.0

    return ExtractRuntimeConfig(
        require_parser_hook=require_parser_hook,
        summary_mode=summary_mode,
        summary_llm_command=summary_llm_command,
        summary_llm_timeout_seconds=summary_llm_timeout_seconds,
    )


def _load_plan_execution_config(repo_root: Path) -> PlanExecutionConfig:
    policy = _load_verifier_policy(repo_root)
    plan_execution = policy.get("plan_execution")
    if not isinstance(plan_execution, dict):
        plan_execution = {}

    implementation = plan_execution.get("implementation")
    if not isinstance(implementation, dict):
        implementation = {}

    enabled = _coerce_bool(
        implementation.get("enabled"),
        default=DEFAULT_PLAN_EXECUTION_ENABLED,
    )
    run_unit = (
        str(implementation.get("run_unit", DEFAULT_PLAN_EXECUTION_RUN_UNIT))
        .strip()
        .lower()
    )
    if run_unit not in PLAN_EXECUTION_RUN_UNITS:
        raise StageCheckError(
            "plan_execution.implementation.run_unit must be one of "
            f"{sorted(PLAN_EXECUTION_RUN_UNITS)}"
        )

    def _parse_int_setting(
        field: str,
        *,
        default: int,
        source: dict[str, Any] | None = None,
        error_prefix: str = "plan_execution.implementation",
    ) -> int:
        raw_value = (source or implementation).get(field)
        if raw_value is None:
            return default
        if isinstance(raw_value, bool):
            raise StageCheckError(f"{error_prefix}.{field} must be an integer")
        if isinstance(raw_value, int):
            return raw_value
        if isinstance(raw_value, float):
            if not raw_value.is_integer():
                raise StageCheckError(f"{error_prefix}.{field} must be an integer")
            return int(raw_value)
        if isinstance(raw_value, str):
            candidate = raw_value.strip()
            if not candidate:
                raise StageCheckError(f"{error_prefix}.{field} must be an integer")
            try:
                return int(candidate)
            except Exception as exc:
                raise StageCheckError(
                    f"{error_prefix}.{field} must be an integer"
                ) from exc
        raise StageCheckError(f"{error_prefix}.{field} must be an integer")

    max_parallel_tasks = _parse_int_setting(
        "max_parallel_tasks",
        default=DEFAULT_PLAN_EXECUTION_MAX_PARALLEL_TASKS,
    )
    if max_parallel_tasks < 1:
        raise StageCheckError(
            "plan_execution.implementation.max_parallel_tasks must be >= 1"
        )

    task_retry_max = _parse_int_setting(
        "task_retry_max",
        default=DEFAULT_PLAN_EXECUTION_TASK_RETRY_MAX,
    )
    if task_retry_max < 0:
        raise StageCheckError(
            "plan_execution.implementation.task_retry_max must be >= 0"
        )

    wave_retry_max = _parse_int_setting(
        "wave_retry_max",
        default=DEFAULT_PLAN_EXECUTION_WAVE_RETRY_MAX,
    )
    if wave_retry_max < 0:
        raise StageCheckError(
            "plan_execution.implementation.wave_retry_max must be >= 0"
        )

    failure_mode = (
        str(
            implementation.get(
                "failure_mode",
                DEFAULT_PLAN_EXECUTION_FAILURE_MODE,
            )
        )
        .strip()
        .lower()
    )
    if failure_mode not in PLAN_EXECUTION_FAILURE_MODES:
        raise StageCheckError(
            "plan_execution.implementation.failure_mode must be one of "
            f"{sorted(PLAN_EXECUTION_FAILURE_MODES)}"
        )

    on_wave_retry_exhausted = (
        str(
            implementation.get(
                "on_wave_retry_exhausted",
                DEFAULT_PLAN_EXECUTION_ON_WAVE_RETRY_EXHAUSTED,
            )
        )
        .strip()
        .lower()
    )
    if on_wave_retry_exhausted not in TERMINAL_STAGES:
        raise StageCheckError(
            "plan_execution.implementation.on_wave_retry_exhausted must be a terminal stage"
        )

    require_verification_commands = _coerce_bool(
        implementation.get("require_verification_commands"),
        default=DEFAULT_PLAN_EXECUTION_REQUIRE_VERIFICATION_COMMANDS,
    )

    approval = implementation.get("approval")
    if approval is None:
        approval = {}
    if not isinstance(approval, dict):
        raise StageCheckError(
            "plan_execution.implementation.approval must be a mapping"
        )

    approval_enabled = _coerce_bool(
        approval.get("enabled"),
        default=DEFAULT_PLAN_APPROVAL_ENABLED,
    )
    require_for_project_wide_tasks = _coerce_bool(
        approval.get("require_for_project_wide_tasks"),
        default=DEFAULT_PLAN_APPROVAL_REQUIRE_FOR_PROJECT_WIDE_TASKS,
    )
    max_tasks_without_approval = _parse_int_setting(
        "max_tasks_without_approval",
        default=DEFAULT_PLAN_APPROVAL_MAX_TASKS_WITHOUT_APPROVAL,
        source=approval,
        error_prefix="plan_execution.implementation.approval",
    )
    if max_tasks_without_approval < 1:
        raise StageCheckError(
            "plan_execution.implementation.approval.max_tasks_without_approval must be >= 1"
        )
    max_waves_without_approval = _parse_int_setting(
        "max_waves_without_approval",
        default=DEFAULT_PLAN_APPROVAL_MAX_WAVES_WITHOUT_APPROVAL,
        source=approval,
        error_prefix="plan_execution.implementation.approval",
    )
    if max_waves_without_approval < 1:
        raise StageCheckError(
            "plan_execution.implementation.approval.max_waves_without_approval must be >= 1"
        )
    max_project_wide_paths_without_approval = _parse_int_setting(
        "max_project_wide_paths_without_approval",
        default=DEFAULT_PLAN_APPROVAL_MAX_PROJECT_WIDE_PATHS_WITHOUT_APPROVAL,
        source=approval,
        error_prefix="plan_execution.implementation.approval",
    )
    if max_project_wide_paths_without_approval < 0:
        raise StageCheckError(
            "plan_execution.implementation.approval.max_project_wide_paths_without_approval must be >= 0"
        )
    require_after_retries = _coerce_bool(
        approval.get("require_after_retries"),
        default=DEFAULT_PLAN_APPROVAL_REQUIRE_AFTER_RETRIES,
    )

    return PlanExecutionConfig(
        implementation=PlanExecutionImplementationConfig(
            enabled=enabled,
            run_unit=run_unit,
            max_parallel_tasks=max_parallel_tasks,
            task_retry_max=task_retry_max,
            wave_retry_max=wave_retry_max,
            failure_mode=failure_mode,
            on_wave_retry_exhausted=on_wave_retry_exhausted,
            require_verification_commands=require_verification_commands,
            approval=PlanApprovalPolicyConfig(
                enabled=approval_enabled,
                require_for_project_wide_tasks=require_for_project_wide_tasks,
                max_tasks_without_approval=max_tasks_without_approval,
                max_waves_without_approval=max_waves_without_approval,
                max_project_wide_paths_without_approval=max_project_wide_paths_without_approval,
                require_after_retries=require_after_retries,
            ),
        )
    )


def _load_slurm_lifecycle_strict_policy(repo_root: Path) -> bool:
    """Return whether strict synced->completed SLURM lifecycle is enforced."""
    policy = _load_verifier_policy(repo_root)
    slurm = policy.get("slurm")
    if not isinstance(slurm, dict):
        return True
    return _coerce_bool(slurm.get("lifecycle_strict"), default=True)


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

    mode = (
        str(raw_edit_scope.get("mode", DEFAULT_AGENT_RUNNER_EDIT_SCOPE_MODE))
        .strip()
        .lower()
    )
    if mode not in AGENT_RUNNER_EDIT_SCOPE_MODES:
        raise StageCheckError(
            f"agent_runner.edit_scope.mode must be one of {', '.join(AGENT_RUNNER_EDIT_SCOPE_MODES)}"
        )

    raw_core_dirs = raw_edit_scope.get(
        "core_dirs", list(DEFAULT_AGENT_RUNNER_CORE_DIRS)
    )
    if raw_core_dirs is None:
        raw_core_dirs = list(DEFAULT_AGENT_RUNNER_CORE_DIRS)
    if not isinstance(raw_core_dirs, list):
        raise StageCheckError(
            "agent_runner.edit_scope.core_dirs must be a list of repo-relative directory paths"
        )
    core_dirs: list[str] = []
    for raw_dir in raw_core_dirs:
        value = str(raw_dir).strip()
        if not value:
            raise StageCheckError(
                "agent_runner.edit_scope.core_dirs entries must be non-empty"
            )
        if value not in core_dirs:
            core_dirs.append(value)

    ensure_iteration_dir = bool(
        raw_edit_scope.get(
            "ensure_iteration_dir", DEFAULT_AGENT_RUNNER_ENSURE_ITERATION_DIR
        )
    )
    if mode == "scope_root_only":
        core_dirs = []

    return AgentRunnerEditScopeConfig(
        mode=mode,
        core_dirs=tuple(core_dirs),
        ensure_iteration_dir=ensure_iteration_dir,
    )


def _load_agent_runner_config(repo_root: Path) -> AgentRunnerConfig:
    policy_path = repo_root / ".autolab" / "verifier_policy.yaml"
    codex_dangerous_env_opt_in = _coerce_bool(
        os.environ.get("AUTOLAB_CODEX_ALLOW_DANGEROUS"),
        default=False,
    )
    default_runner_command = DEFAULT_AGENT_RUNNER_COMMAND
    if DEFAULT_AGENT_RUNNER_NAME == "codex":
        default_runner_command = AGENT_RUNNER_CODEX_DANGEROUS_PRESET
    elif DEFAULT_AGENT_RUNNER_NAME == "claude":
        default_runner_command = AGENT_RUNNER_CLAUDE_DANGEROUS_PRESET
    if yaml is None or not policy_path.exists():
        return AgentRunnerConfig(
            runner=DEFAULT_AGENT_RUNNER_NAME,
            enabled=False,
            command=default_runner_command,
            stages=DEFAULT_AGENT_RUNNER_STAGES,
            edit_scope=_default_agent_runner_edit_scope(),
            timeout_seconds=DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
            claude_dangerously_skip_permissions=True,
            codex_dangerously_bypass_approvals_and_sandbox=True,
        )

    try:
        loaded = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise StageCheckError(
            f"agent_runner policy could not be parsed at {policy_path}: {exc}"
        ) from exc

    if not isinstance(loaded, dict):
        return AgentRunnerConfig(
            runner=DEFAULT_AGENT_RUNNER_NAME,
            enabled=False,
            command=default_runner_command,
            stages=DEFAULT_AGENT_RUNNER_STAGES,
            edit_scope=_default_agent_runner_edit_scope(),
            timeout_seconds=DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
            claude_dangerously_skip_permissions=True,
            codex_dangerously_bypass_approvals_and_sandbox=True,
        )

    runner_section = loaded.get("agent_runner")
    if runner_section is None:
        return AgentRunnerConfig(
            runner=DEFAULT_AGENT_RUNNER_NAME,
            enabled=False,
            command=default_runner_command,
            stages=DEFAULT_AGENT_RUNNER_STAGES,
            edit_scope=_default_agent_runner_edit_scope(),
            timeout_seconds=DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS,
            claude_dangerously_skip_permissions=True,
            codex_dangerously_bypass_approvals_and_sandbox=True,
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
        default=True,
    ) or _coerce_bool(os.environ.get("AUTOLAB_CLAUDE_ALLOW_DANGEROUS"), default=False)
    codex_dangerous_opt_in = (
        _coerce_bool(
            runner_section.get("codex_dangerously_bypass_approvals_and_sandbox"),
            default=True,
        )
        or codex_dangerous_env_opt_in
    )
    if raw_command is not None:
        command = str(raw_command).strip()
    else:
        if runner_name == "claude" and claude_dangerous_opt_in:
            command = AGENT_RUNNER_CLAUDE_DANGEROUS_PRESET
        elif runner_name == "codex" and codex_dangerous_opt_in:
            command = AGENT_RUNNER_CODEX_DANGEROUS_PRESET
        else:
            command = AGENT_RUNNER_PRESETS.get(
                runner_name, DEFAULT_AGENT_RUNNER_COMMAND
            )
    if enabled and not command:
        raise StageCheckError(
            "agent_runner.command must be set when agent_runner.enabled is true"
        )

    raw_stages = runner_section.get("stages")
    dropped_unsupported_stages: list[str] = []
    if raw_stages is None:
        stages = list(DEFAULT_AGENT_RUNNER_STAGES)
    else:
        if not isinstance(raw_stages, list):
            raise StageCheckError("agent_runner.stages must be a list of stage names")
        stages = []
        for raw_stage in raw_stages:
            stage = str(raw_stage).strip()
            if stage not in RUNNER_ELIGIBLE_STAGES:
                if enabled:
                    raise StageCheckError(
                        f"agent_runner.stages includes unsupported stage '{stage}'"
                    )
                dropped_unsupported_stages.append(stage)
                continue
            if stage not in stages:
                stages.append(stage)
    if dropped_unsupported_stages and not enabled:
        dropped_rendered = ", ".join(
            repr(stage) for stage in dropped_unsupported_stages
        )
        print(
            (
                "warning: dropped unsupported agent_runner.stages entries while "
                f"agent_runner.enabled=false: {dropped_rendered}"
            ),
            file=sys.stderr,
        )
    if enabled and not stages:
        raise StageCheckError(
            "agent_runner.stages must include at least one active stage"
        )

    raw_timeout = runner_section.get(
        "timeout_seconds", DEFAULT_AGENT_RUNNER_TIMEOUT_SECONDS
    )
    try:
        timeout_seconds = float(raw_timeout)
    except Exception as exc:
        raise StageCheckError(
            "agent_runner.timeout_seconds must be a non-negative number"
        ) from exc
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
        codex_dangerously_bypass_approvals_and_sandbox=codex_dangerous_opt_in,
    )


def _load_protected_files(
    policy: dict[str, Any], *, auto_mode: bool = False
) -> list[str]:
    """Return normalized protected file paths from verifier policy.

    Supports:
    - ``protected_files`` legacy list
    - ``protected_file_profiles`` with optional ``protected_profile`` selector
    - ``safe_automation_protected_files`` toggle (profile: ``safe_automation``)
    - ``auto_mode`` runtime overlay for unattended safety defaults
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
    safe_automation_profile_enabled = _coerce_bool(
        policy.get("safe_automation_protected_files"), default=False
    )
    if auto_mode:
        safe_automation_profile_enabled = True
    if safe_automation_profile_enabled:
        profile_name = "safe_automation"

    profile_map = policy.get("protected_file_profiles", {})
    if isinstance(profile_map, dict):
        profile_values = profile_map.get(profile_name)
        if isinstance(profile_values, list):
            for path in _normalize_list(profile_values):
                if path not in result:
                    result.append(path)

    safe_profile = policy.get("safe_automation_protected_files_list")
    if safe_automation_profile_enabled:
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
    value = str(policy.get("python_bin", "")).strip()
    if not value:
        return sys.executable
    if value in {"python", "python3"}:
        return sys.executable
    return value


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
            requirements[key] = _coerce_bool(
                policy.get(legacy_key), default=requirements[key]
            )

    # Layer 3: per-stage policy overrides (highest priority).
    requirements_by_stage = policy.get("requirements_by_stage", {})
    if isinstance(requirements_by_stage, dict):
        stage_section = requirements_by_stage.get(stage, {})
        if isinstance(stage_section, dict):
            for key in requirements:
                if key in stage_section:
                    requirements[key] = _coerce_bool(
                        stage_section.get(key), default=requirements[key]
                    )
    return requirements


def _resolve_stage_max_retries(
    policy: dict[str, Any], stage: str, *, fallback: int = 5
) -> int:
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
    return command.replace("<PYTHON_BIN>", python_bin).replace(
        "{{python_bin}}", python_bin
    )
