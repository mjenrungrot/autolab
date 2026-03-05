from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

ActionKind = Literal["view", "mutating"]
ActionRisk = Literal["low", "medium", "high"]
StageStatus = Literal["complete", "current", "blocked", "upcoming"]
ViewMode = Literal["home", "runs", "files", "console", "help"]
RenderPreviewStatus = Literal["ok", "unavailable", "error"]


@dataclass(frozen=True)
class StageItem:
    name: str
    status: StageStatus
    attempts: str
    is_current: bool


@dataclass(frozen=True)
class RunItem:
    run_id: str
    status: str
    host_mode: str
    job_id: str
    sync_status: str
    started_at: str
    completed_at: str
    manifest_path: Path
    metrics_path: Path


@dataclass(frozen=True)
class TodoItem:
    task_id: str
    source: str
    stage: str
    task_class: str
    text: str
    priority: str


@dataclass(frozen=True)
class ArtifactItem:
    path: Path
    exists: bool
    source: str


@dataclass(frozen=True)
class BacklogExperimentItem:
    experiment_id: str
    iteration_id: str
    hypothesis_id: str
    experiment_type: str
    status: str
    is_current: bool


@dataclass(frozen=True)
class BacklogHypothesisItem:
    hypothesis_id: str
    title: str
    status: str
    is_completed: bool


@dataclass(frozen=True)
class VerificationSummary:
    generated_at: str
    stage_effective: str
    passed: bool
    message: str
    failing_commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class RenderPreview:
    stage: str
    status: RenderPreviewStatus
    template_path: Path | None
    runner_text: str
    runner_excerpt: str
    audit_text: str = ""
    brief_text: str = ""
    human_text: str = ""
    context_payload: dict[str, Any] = field(default_factory=dict)
    error_message: str = ""


@dataclass(frozen=True)
class ActionSpec:
    action_id: str
    label: str
    description: str
    kind: ActionKind
    risk_level: ActionRisk = "low"
    group: str = "general"
    user_label: str = ""
    help_text: str = ""
    advanced: bool = False
    requires_confirmation: bool = False
    requires_arm: bool = False


@dataclass(frozen=True)
class RunActionOptions:
    verify: bool = False
    run_agent_mode: str = "policy"
    auto_decision: bool = False


@dataclass(frozen=True)
class LoopActionOptions:
    max_iterations: int = 3
    max_hours: float = 2.0
    auto: bool = True
    verify: bool = True
    run_agent_mode: str = "policy"


@dataclass(frozen=True)
class CommandIntent:
    action_id: str
    argv: tuple[str, ...]
    cwd: Path
    expected_writes: tuple[str, ...]
    mutating: bool


@dataclass
class CommandHistoryEntry:
    action_id: str
    action_label: str
    command: str
    started_at: float
    return_code: int | None = None
    duration: float | None = None
    stopped: bool = False
    status: Literal["running", "succeeded", "failed", "interrupted"] = "running"


@dataclass(frozen=True)
class CommandHistoryItem:
    label: str
    command: str
    exit_code: int | None
    duration_seconds: float | None


@dataclass(frozen=True)
class RecommendedAction:
    action_id: str
    reason: str


@dataclass(frozen=True)
class HandoffSummary:
    handoff_json_path: Path | None
    handoff_md_path: Path | None
    current_scope: str
    scope_root: str
    current_stage: str
    wave_status: str
    wave_current: int | None
    wave_executed: int
    wave_total: int
    task_total: int
    task_completed: int
    task_failed: int
    task_blocked: int
    task_pending: int
    latest_verifier_passed: bool | None
    blocker_count: int
    pending_decision_count: int
    recommended_command: str
    safe_resume_status: str
    safe_resume_command: str


@dataclass(frozen=True)
class CockpitSnapshot:
    repo_root: Path
    state_path: Path
    autolab_dir: Path
    iteration_dir: Path | None
    current_stage: str
    stage_attempt: int
    max_stage_attempts: int
    last_run_id: str
    stage_items: tuple[StageItem, ...]
    runs: tuple[RunItem, ...]
    todos: tuple[TodoItem, ...]
    verification: VerificationSummary | None
    render_preview: RenderPreview
    top_blockers: tuple[str, ...]
    primary_blocker: str
    secondary_blockers: tuple[str, ...]
    backlog_experiments: tuple[BacklogExperimentItem, ...] = ()
    backlog_hypotheses: tuple[BacklogHypothesisItem, ...] = ()
    backlog_error: str = ""
    recommended_actions: tuple[RecommendedAction, ...] = ()
    stage_summaries: dict[str, str] = field(default_factory=dict)
    artifacts_by_stage: dict[str, tuple[ArtifactItem, ...]] = field(
        default_factory=dict
    )
    common_artifacts: tuple[ArtifactItem, ...] = ()
    handoff: HandoffSummary | None = None
