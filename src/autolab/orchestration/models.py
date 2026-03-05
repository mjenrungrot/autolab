"""Typed orchestration request/response models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from autolab.models import RunOutcome


@dataclass(frozen=True)
class RunRequest:
    state_path: Path
    decision: str | None = None
    run_agent_mode: str = "policy"
    verify_before_evaluate: bool = False
    assistant: bool = False
    auto_mode: bool = False
    auto_decision: bool = False
    strict_implementation_progress: bool = True


@dataclass(frozen=True)
class LoopRequest:
    state_path: Path
    max_iterations: int
    run_agent_mode: str = "policy"
    assistant: bool = False
    verify_before_evaluate: bool = False
    auto_mode: bool = False
    auto_decision: bool = False
    strict_implementation_progress: bool = True


@dataclass(frozen=True)
class EngineContext:
    repo_root: Path


@dataclass(frozen=True)
class LoopOutcome:
    outcomes: tuple[RunOutcome, ...] = field(default_factory=tuple)
    completed_iterations: int = 0
    final_exit_code: int = 0
    terminal_reason: str = "iteration_budget_reached"
