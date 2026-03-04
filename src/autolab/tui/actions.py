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
_REVIEW_EXPECTED_WRITES = (
    ".autolab/state.json",
    ".autolab/agent_result.json",
    ".autolab/logs/orchestrator.log",
    ".autolab/backlog.yaml",
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
_FOCUS_EXPECTED_WRITES = (
    ".autolab/state.json",
    ".autolab/agent_result.json",
    ".autolab/todo_state.json",
    ".autolab/todo_focus.json",
    "docs/todo.md",
    ".autolab/logs/orchestrator.log",
)
_EXPERIMENT_BASE_EXPECTED_WRITES = (
    ".autolab/state.json",
    ".autolab/agent_result.json",
    ".autolab/backlog.yaml",
    ".autolab/todo_state.json",
    ".autolab/todo_focus.json",
    "docs/todo.md",
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
        action_id="open_rendered_audit",
        label="Open rendered audit contract",
        description="Open the rendered audit/verifier contract for the current stage.",
        kind="view",
        risk_level="low",
        group="home",
        user_label="Open rendered audit",
        help_text="Inspect the human-readable audit policy contract for this stage.",
    ),
    ActionSpec(
        action_id="open_retry_brief",
        label="Open retry brief",
        description="Open the distilled retry blocker brief for implementation stage.",
        kind="view",
        risk_level="low",
        group="home",
        user_label="Open retry brief",
        help_text="Inspect short blocker bullets from the last failed review/verification.",
    ),
    ActionSpec(
        action_id="open_verification_result",
        label="Open verification result",
        description="Open .autolab/verification_result.json for detailed verifier output.",
        kind="view",
        risk_level="low",
        group="home",
        user_label="Open verification result",
        help_text="Inspect verifier status, command outcomes, and staged failures.",
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
        action_id="resolve_human_review",
        label="Resolve human review",
        description="Record a human review decision: pass, retry, or stop.",
        kind="mutating",
        risk_level="medium",
        group="home",
        user_label="Resolve human review",
        help_text="Record human review decision (pass, retry, or stop).",
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
    ActionSpec(
        action_id="focus_experiment",
        label="Focus experiment",
        description="Retarget workflow focus to a backlog experiment/iteration.",
        kind="mutating",
        risk_level="medium",
        group="advanced",
        user_label="Focus experiment (advanced)",
        help_text="Run autolab focus to retarget state and sync steering artifacts.",
        advanced=True,
        requires_confirmation=True,
        requires_arm=True,
    ),
    ActionSpec(
        action_id="experiment_create",
        label="Create experiment",
        description="Create a new plan experiment and iteration skeleton.",
        kind="mutating",
        risk_level="high",
        group="advanced",
        user_label="Create experiment (advanced)",
        help_text="Run autolab experiment create and append a backlog experiment entry.",
        advanced=True,
        requires_confirmation=True,
        requires_arm=True,
    ),
    ActionSpec(
        action_id="experiment_move",
        label="Move experiment",
        description="Move an experiment across plan/in_progress/done lifecycle types.",
        kind="mutating",
        risk_level="high",
        group="advanced",
        user_label="Move experiment (advanced)",
        help_text="Run autolab experiment move to update backlog type and iteration path.",
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


def _dedupe_preserve_order(entries: list[str]) -> tuple[str, ...]:
    deduped: list[str] = []
    seen: set[str] = set()
    for entry in entries:
        if entry in seen:
            continue
        seen.add(entry)
        deduped.append(entry)
    return tuple(deduped)


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


def build_human_review_intent(*, state_path: Path, status: str) -> CommandIntent:
    repo_root = _resolve_repo_root(state_path)
    normalized_status = str(status).strip().lower()
    if normalized_status not in {"pass", "retry", "stop"}:
        raise ValueError(
            "invalid review status: expected one of 'pass', 'retry', or 'stop'"
        )
    argv = _base_state_argv("review", state_path=state_path)
    argv.extend(["--status", normalized_status])
    return CommandIntent(
        action_id="resolve_human_review",
        argv=tuple(argv),
        cwd=repo_root,
        expected_writes=_REVIEW_EXPECTED_WRITES,
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


def build_focus_intent(
    *,
    state_path: Path,
    experiment_id: str = "",
    iteration_id: str = "",
) -> CommandIntent:
    repo_root = _resolve_repo_root(state_path)
    argv = _base_state_argv("focus", state_path=state_path)
    normalized_iteration_id = str(iteration_id).strip()
    normalized_experiment_id = str(experiment_id).strip()
    if normalized_iteration_id:
        argv.extend(["--iteration-id", normalized_iteration_id])
    if normalized_experiment_id:
        argv.extend(["--experiment-id", normalized_experiment_id])
    return CommandIntent(
        action_id="focus_experiment",
        argv=tuple(argv),
        cwd=repo_root,
        expected_writes=_FOCUS_EXPECTED_WRITES,
        mutating=True,
    )


def build_experiment_create_intent(
    *,
    state_path: Path,
    experiment_id: str,
    iteration_id: str,
    hypothesis_id: str = "",
) -> CommandIntent:
    repo_root = _resolve_repo_root(state_path)
    normalized_experiment_id = str(experiment_id).strip()
    normalized_iteration_id = str(iteration_id).strip()
    normalized_hypothesis_id = str(hypothesis_id).strip()
    argv = _base_state_argv("experiment", "create", state_path=state_path)
    argv.extend(["--experiment-id", normalized_experiment_id])
    argv.extend(["--iteration-id", normalized_iteration_id])
    if normalized_hypothesis_id:
        argv.extend(["--hypothesis-id", normalized_hypothesis_id])
    expected_writes = _dedupe_preserve_order(
        [
            *_EXPERIMENT_BASE_EXPECTED_WRITES,
            f"experiments/plan/{normalized_iteration_id}",
        ]
    )
    return CommandIntent(
        action_id="experiment_create",
        argv=tuple(argv),
        cwd=repo_root,
        expected_writes=expected_writes,
        mutating=True,
    )


def build_experiment_move_intent(
    *,
    state_path: Path,
    to_type: str,
    experiment_id: str = "",
    iteration_id: str = "",
) -> CommandIntent:
    repo_root = _resolve_repo_root(state_path)
    normalized_to = str(to_type).strip()
    normalized_experiment_id = str(experiment_id).strip()
    normalized_iteration_id = str(iteration_id).strip()
    argv = _base_state_argv("experiment", "move", state_path=state_path)
    if normalized_iteration_id:
        argv.extend(["--iteration-id", normalized_iteration_id])
    if normalized_experiment_id:
        argv.extend(["--experiment-id", normalized_experiment_id])
    argv.extend(["--to", normalized_to])
    expected_writes = _dedupe_preserve_order(
        [
            *_EXPERIMENT_BASE_EXPECTED_WRITES,
            ".autolab/*.json",
            (
                f"experiments/{normalized_to}/{normalized_iteration_id}"
                if normalized_to and normalized_iteration_id
                else "experiments/*/<iteration_id>"
            ),
        ]
    )
    return CommandIntent(
        action_id="experiment_move",
        argv=tuple(argv),
        cwd=repo_root,
        expected_writes=expected_writes,
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
