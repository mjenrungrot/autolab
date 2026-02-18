"""Autolab data models — exceptions, dataclasses, and coercion helpers."""

from __future__ import annotations

from dataclasses import dataclass
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
    commit_allowed: bool = True
    commit_task_id: str = ""
    commit_cycle_stage: str = ""
    commit_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvalResult:
    """Structured return from _evaluate_stage, replacing raw tuple."""
    next_stage: str
    status: str          # "complete" | "failed"
    summary: str         # human-readable message
    needs_retry: bool = False  # structured flag replacing string matching


@dataclass(frozen=True)
class AgentRunnerConfig:
    runner: str
    enabled: bool
    command: str
    stages: tuple[str, ...]
    edit_scope: "AgentRunnerEditScopeConfig"
    timeout_seconds: float
    claude_dangerously_skip_permissions: bool = False


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


@dataclass(frozen=True)
class GuardrailConfig:
    max_same_decision_streak: int
    max_no_progress_decisions: int
    max_update_docs_cycles: int
    max_generated_todo_tasks: int
    on_breach: str


@dataclass(frozen=True)
class MeaningfulChangeConfig:
    require_verification: bool
    require_implementation_progress: bool
    require_git_for_progress: bool
    on_non_git_behavior: str
    exclude_paths: tuple[str, ...]


@dataclass(frozen=True)
class AutoCommitConfig:
    mode: str
