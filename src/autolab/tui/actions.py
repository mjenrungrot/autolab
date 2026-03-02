from __future__ import annotations

import os
import shlex
from pathlib import Path

from autolab.state import _resolve_repo_root
from autolab.tui.models import (
    ActionSpec,
    CommandIntent,
    LoopActionOptions,
    RunActionOptions,
)

_RUN_AGENT_MODES = {"policy", "force_on", "force_off"}
_VERIFY_EXPECTED_WRITES = (
    ".autolab/verification_result.json",
    ".autolab/logs/verification_*.json",
    ".autolab/logs/orchestrator.log",
)
_RUN_EXPECTED_WRITES = (
    ".autolab/state.json",
    ".autolab/agent_result.json",
    ".autolab/logs/orchestrator.log",
    ".autolab/todo_state.json",
    "docs/todo.md",
)
_LOOP_EXPECTED_WRITES_BASE = (
    ".autolab/state.json",
    ".autolab/logs/orchestrator.log",
    ".autolab/todo_state.json",
    "docs/todo.md",
)
_LOOP_EXPECTED_WRITES_AUTO = (
    ".autolab/lock",
    ".autolab/logs/overnight_summary.md",
)
_TODO_SYNC_EXPECTED_WRITES = (
    ".autolab/todo_state.json",
    ".autolab/todo_focus.json",
    "docs/todo.md",
    ".autolab/logs/orchestrator.log",
)
_LOCK_BREAK_EXPECTED_WRITES = (
    ".autolab/lock",
    ".autolab/logs/orchestrator.log",
)

ACTION_CATALOG: tuple[ActionSpec, ...] = (
    ActionSpec(
        action_id="open_selected_artifact",
        label="Open selected artifact (viewer)",
        description="Open the selected file in the in-TUI read-only viewer.",
        kind="view",
        risk_level="low",
        group="files",
        user_label="Open selected file",
        help_text="Preview the selected file in read-only mode.",
    ),
    ActionSpec(
        action_id="open_selected_artifact_editor",
        label="Open selected artifact in $EDITOR",
        description="Launch the selected file in your external editor.",
        kind="view",
        risk_level="low",
        group="files",
        user_label="Open selected file in editor",
        help_text="Open the selected file in your configured external editor.",
        requires_confirmation=True,
    ),
    ActionSpec(
        action_id="open_selected_run_manifest",
        label="Open selected run manifest",
        description="Open runs/<run_id>/run_manifest.json in the viewer.",
        kind="view",
        risk_level="low",
        group="runs",
        user_label="Open selected run manifest",
        help_text="Inspect scheduler status, timestamps, and synced artifacts for a run.",
    ),
    ActionSpec(
        action_id="open_selected_run_metrics",
        label="Open selected run metrics",
        description="Open runs/<run_id>/metrics.json in the viewer.",
        kind="view",
        risk_level="low",
        group="runs",
        user_label="Open selected run metrics",
        help_text="Inspect extracted metrics for the selected run.",
    ),
    ActionSpec(
        action_id="open_rendered_prompt",
        label="Open rendered stage prompt",
        description="Open the resolved prompt text for the current stage.",
        kind="view",
        risk_level="low",
        group="home",
        user_label="Open rendered prompt",
        help_text="Preview the exact prompt content Autolab will run.",
    ),
    ActionSpec(
        action_id="open_render_context",
        label="Open rendered prompt context",
        description="Open the resolved context payload used during prompt rendering.",
        kind="view",
        risk_level="low",
        group="home",
        user_label="Open render context",
        help_text="Inspect resolved token values and render metadata.",
    ),
    ActionSpec(
        action_id="open_stage_prompt",
        label="Open current stage prompt",
        description="Open .autolab/prompts/stage_*.md for the selected stage.",
        kind="view",
        risk_level="low",
        group="home",
        user_label="Open stage guidance",
        help_text="Read the stage instructions before taking action.",
    ),
    ActionSpec(
        action_id="open_state_history",
        label="Open state/history",
        description="Open .autolab/state.json in the viewer.",
        kind="view",
        risk_level="low",
        group="home",
        user_label="Open workflow state",
        help_text="Review raw state fields and state history metadata.",
    ),
    ActionSpec(
        action_id="verify_current_stage",
        label="Verify current stage",
        description="Run autolab verify for the selected/current stage.",
        kind="mutating",
        risk_level="medium",
        group="home",
        user_label="Verify current stage",
        help_text="Run verification checks and refresh verification artifacts.",
        requires_confirmation=True,
        requires_arm=True,
    ),
    ActionSpec(
        action_id="run_once",
        label="Run one transition",
        description="Run one autolab transition with optional verify gating.",
        kind="mutating",
        risk_level="medium",
        group="home",
        user_label="Run one transition",
        help_text="Execute one workflow transition and update state/artifacts.",
        requires_confirmation=True,
        requires_arm=True,
    ),
    ActionSpec(
        action_id="run_loop",
        label="Start loop",
        description="Run bounded autolab loop with optional unattended mode.",
        kind="mutating",
        risk_level="high",
        group="advanced",
        user_label="Start loop (advanced)",
        help_text="Run repeated transitions until limits are reached.",
        advanced=True,
        requires_confirmation=True,
        requires_arm=True,
    ),
    ActionSpec(
        action_id="todo_sync",
        label="Todo sync",
        description="Reconcile docs/todo.md and .autolab/todo_state.json.",
        kind="mutating",
        risk_level="medium",
        group="home",
        user_label="Sync todo state",
        help_text="Reconcile todo markdown and todo JSON state files.",
        requires_confirmation=True,
        requires_arm=True,
    ),
    ActionSpec(
        action_id="lock_break",
        label="Break lock",
        description="Force-remove the active autolab lock.",
        kind="mutating",
        risk_level="high",
        group="advanced",
        user_label="Break active lock (advanced)",
        help_text="Force-remove a stale or stuck lock file.",
        advanced=True,
        requires_confirmation=True,
        requires_arm=True,
    ),
)


def list_actions() -> tuple[ActionSpec, ...]:
    return ACTION_CATALOG


def _resolve_run_agent_mode(raw_mode: str) -> str:
    normalized = str(raw_mode).strip().lower()
    if normalized in _RUN_AGENT_MODES:
        return normalized
    return "policy"


def _apply_run_agent_mode(argv: list[str], run_agent_mode: str) -> None:
    mode = _resolve_run_agent_mode(run_agent_mode)
    if mode == "force_on":
        argv.append("--run-agent")
    elif mode == "force_off":
        argv.append("--no-run-agent")


def _base_state_argv(*subcommands: str, state_path: Path) -> list[str]:
    return ["autolab", *subcommands, "--state-file", str(state_path)]


def build_verify_intent(
    *,
    state_path: Path,
    stage: str,
) -> CommandIntent:
    repo_root = _resolve_repo_root(state_path)
    normalized_stage = str(stage).strip()
    argv = _base_state_argv("verify", state_path=state_path)
    if normalized_stage:
        argv.extend(["--stage", normalized_stage])
    return CommandIntent(
        action_id="verify_current_stage",
        argv=tuple(argv),
        cwd=repo_root,
        expected_writes=_VERIFY_EXPECTED_WRITES,
        mutating=True,
    )


def build_run_intent(
    *,
    state_path: Path,
    options: RunActionOptions,
) -> CommandIntent:
    repo_root = _resolve_repo_root(state_path)
    argv = _base_state_argv("run", state_path=state_path)
    if options.verify:
        argv.append("--verify")
    if options.auto_decision:
        argv.append("--auto-decision")
    _apply_run_agent_mode(argv, options.run_agent_mode)
    return CommandIntent(
        action_id="run_once",
        argv=tuple(argv),
        cwd=repo_root,
        expected_writes=_RUN_EXPECTED_WRITES,
        mutating=True,
    )


def build_loop_intent(
    *,
    state_path: Path,
    options: LoopActionOptions,
) -> CommandIntent:
    repo_root = _resolve_repo_root(state_path)
    max_iterations = max(1, int(options.max_iterations))
    auto_enabled = bool(options.auto)
    argv = _base_state_argv("loop", state_path=state_path)
    argv.extend(["--max-iterations", str(max_iterations)])
    if auto_enabled:
        max_hours = max(0.01, float(options.max_hours))
        argv.extend(["--auto", "--max-hours", f"{max_hours:g}"])
    if options.verify:
        argv.append("--verify")
    _apply_run_agent_mode(argv, options.run_agent_mode)
    expected_writes: list[str] = list(_LOOP_EXPECTED_WRITES_BASE)
    if auto_enabled:
        expected_writes.extend(_LOOP_EXPECTED_WRITES_AUTO)
    return CommandIntent(
        action_id="run_loop",
        argv=tuple(argv),
        cwd=repo_root,
        expected_writes=tuple(expected_writes),
        mutating=True,
    )


def build_todo_sync_intent(*, state_path: Path) -> CommandIntent:
    repo_root = _resolve_repo_root(state_path)
    return CommandIntent(
        action_id="todo_sync",
        argv=tuple(_base_state_argv("todo", "sync", state_path=state_path)),
        cwd=repo_root,
        expected_writes=_TODO_SYNC_EXPECTED_WRITES,
        mutating=True,
    )


def build_lock_break_intent(*, state_path: Path, reason: str) -> CommandIntent:
    repo_root = _resolve_repo_root(state_path)
    normalized_reason = str(reason).strip() or "tui manual break"
    argv = _base_state_argv("lock", "break", state_path=state_path)
    argv.extend(["--reason", normalized_reason])
    return CommandIntent(
        action_id="lock_break",
        argv=tuple(argv),
        cwd=repo_root,
        expected_writes=_LOCK_BREAK_EXPECTED_WRITES,
        mutating=True,
    )


def build_open_in_editor_intent(
    *,
    target_path: Path,
    cwd: Path,
) -> CommandIntent:
    editor = os.environ.get("EDITOR", "").strip() or "cursor"
    editor_parts = shlex.split(editor)
    if not editor_parts:
        editor_parts = ["cursor"]
    argv = [*editor_parts, str(target_path)]
    return CommandIntent(
        action_id="open_selected_artifact_editor",
        argv=tuple(argv),
        cwd=cwd,
        expected_writes=(str(target_path),),
        mutating=False,
    )
