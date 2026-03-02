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
    prompt_text: str
    prompt_excerpt: str
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


@dataclass(frozen=True)
class RecommendedAction:
    action_id: str
    reason: str


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
    recommended_actions: tuple[RecommendedAction, ...] = ()
    stage_summaries: dict[str, str] = field(default_factory=dict)
    artifacts_by_stage: dict[str, tuple[ArtifactItem, ...]] = field(
        default_factory=dict
    )
    common_artifacts: tuple[ArtifactItem, ...] = ()
