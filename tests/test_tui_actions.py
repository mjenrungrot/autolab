from __future__ import annotations

from pathlib import Path

import pytest

from autolab.tui.actions import (
    build_checkpoint_create_intent,
    build_experiment_create_intent,
    build_experiment_move_intent,
    build_focus_intent,
    build_hooks_install_intent,
    build_human_review_intent,
    build_lock_break_intent,
    build_loop_intent,
    build_open_in_editor_intent,
    build_remote_doctor_intent,
    build_run_intent,
    build_todo_sync_intent,
    build_uat_init_intent,
    build_verify_intent,
    list_actions,
)
from autolab.tui.models import LoopActionOptions, RunActionOptions


def test_action_catalog_contains_required_entries() -> None:
    actions = list_actions()
    action_ids = {action.action_id for action in actions}
    assert "open_rendered_prompt" in action_ids
    assert "open_render_context" in action_ids
    assert "open_rendered_audit" in action_ids
    assert "open_rendered_brief" in action_ids
    assert "open_rendered_human" in action_ids
    assert "open_verification_result" in action_ids
    assert "verify_current_stage" in action_ids
    assert "run_once" in action_ids
    assert "resolve_human_review" in action_ids
    assert "run_loop" in action_ids
    assert "todo_sync" in action_ids
    assert "checkpoint_create" in action_ids
    assert "remote_doctor" in action_ids
    assert "uat_init" in action_ids
    assert "hooks_install" in action_ids
    assert "focus_experiment" in action_ids
    assert "experiment_create" in action_ids
    assert "experiment_move" in action_ids
    assert "open_selected_artifact" in action_ids
    rendered_action = next(
        action for action in actions if action.action_id == "open_rendered_prompt"
    )
    assert rendered_action.kind == "view"
    assert rendered_action.requires_arm is False
    assert rendered_action.requires_confirmation is False
    verify_action = next(
        action for action in actions if action.action_id == "verify_current_stage"
    )
    assert verify_action.kind == "mutating"
    assert verify_action.requires_arm is True
    assert verify_action.requires_confirmation is True
    assert verify_action.risk_level == "medium"
    assert verify_action.user_label

    open_verification_action = next(
        action for action in actions if action.action_id == "open_verification_result"
    )
    assert open_verification_action.kind == "view"
    assert open_verification_action.requires_confirmation is False
    assert open_verification_action.risk_level == "low"

    run_loop = next(action for action in actions if action.action_id == "run_loop")
    assert run_loop.advanced is True
    assert run_loop.risk_level == "high"

    checkpoint_action = next(
        action for action in actions if action.action_id == "checkpoint_create"
    )
    assert checkpoint_action.kind == "mutating"
    assert checkpoint_action.requires_arm is True
    assert checkpoint_action.requires_confirmation is True

    remote_action = next(
        action for action in actions if action.action_id == "remote_doctor"
    )
    assert remote_action.kind == "view"
    assert remote_action.requires_arm is False
    assert remote_action.requires_confirmation is True

    uat_action = next(action for action in actions if action.action_id == "uat_init")
    assert uat_action.kind == "mutating"
    assert uat_action.requires_arm is True
    assert uat_action.requires_confirmation is True
    assert "--suggest" in uat_action.help_text

    hooks_action = next(
        action for action in actions if action.action_id == "hooks_install"
    )
    assert hooks_action.kind == "mutating"
    assert hooks_action.requires_arm is True
    assert hooks_action.requires_confirmation is True

    human_review_action = next(
        action for action in actions if action.action_id == "resolve_human_review"
    )
    assert human_review_action.kind == "mutating"
    assert human_review_action.advanced is False
    assert human_review_action.requires_arm is True
    assert human_review_action.requires_confirmation is True

    focus_action = next(
        action for action in actions if action.action_id == "focus_experiment"
    )
    assert focus_action.kind == "mutating"
    assert focus_action.advanced is True
    assert focus_action.requires_arm is True
    assert focus_action.requires_confirmation is True

    experiment_create_action = next(
        action for action in actions if action.action_id == "experiment_create"
    )
    assert experiment_create_action.kind == "mutating"
    assert experiment_create_action.advanced is True
    assert experiment_create_action.requires_arm is True
    assert experiment_create_action.requires_confirmation is True

    experiment_move_action = next(
        action for action in actions if action.action_id == "experiment_move"
    )
    assert experiment_move_action.kind == "mutating"
    assert experiment_move_action.advanced is True
    assert experiment_move_action.requires_arm is True
    assert experiment_move_action.requires_confirmation is True


def test_build_run_intent_respects_options(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    intent = build_run_intent(
        state_path=state_path,
        options=RunActionOptions(
            verify=True,
            run_agent_mode="force_off",
            auto_decision=True,
        ),
    )
    assert intent.action_id == "run_once"
    assert "--verify" in intent.argv
    assert "--auto-decision" in intent.argv
    assert "--no-run-agent" in intent.argv
    assert intent.mutating is True
    assert ".autolab/state.json" in intent.expected_writes


def test_build_loop_intent_respects_options(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    intent = build_loop_intent(
        state_path=state_path,
        options=LoopActionOptions(
            max_iterations=4,
            max_hours=1.5,
            auto=True,
            verify=False,
            run_agent_mode="force_on",
        ),
    )
    assert intent.action_id == "run_loop"
    assert "--max-iterations" in intent.argv
    assert "4" in intent.argv
    assert "--auto" in intent.argv
    assert "--max-hours" in intent.argv
    assert "1.5" in intent.argv
    assert "--run-agent" in intent.argv
    assert "--verify" not in intent.argv
    assert ".autolab/logs/overnight_summary.md" in intent.expected_writes
    assert ".autolab/lock" in intent.expected_writes


def test_build_loop_intent_without_auto_omits_auto_only_outputs(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    intent = build_loop_intent(
        state_path=state_path,
        options=LoopActionOptions(
            max_iterations=2,
            max_hours=3.0,
            auto=False,
            verify=True,
            run_agent_mode="invalid-mode",
        ),
    )
    assert "--auto" not in intent.argv
    assert "--max-hours" not in intent.argv
    assert "--run-agent" not in intent.argv
    assert "--no-run-agent" not in intent.argv
    assert ".autolab/logs/overnight_summary.md" not in intent.expected_writes
    assert ".autolab/lock" not in intent.expected_writes


def test_build_verify_and_todo_sync_intents(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    verify_intent = build_verify_intent(state_path=state_path, stage="design")
    assert verify_intent.argv[-2:] == ("--stage", "design")
    assert verify_intent.mutating is True

    todo_sync_intent = build_todo_sync_intent(state_path=state_path)
    assert todo_sync_intent.argv[:3] == ("autolab", "todo", "sync")
    assert todo_sync_intent.mutating is True


def test_build_checkpoint_remote_uat_and_hooks_intents(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"

    checkpoint_intent = build_checkpoint_create_intent(state_path=state_path)
    assert checkpoint_intent.argv[:3] == ("autolab", "checkpoint", "create")
    assert checkpoint_intent.mutating is True
    assert ".autolab/checkpoints/index.json" in checkpoint_intent.expected_writes

    remote_intent = build_remote_doctor_intent(state_path=state_path)
    assert remote_intent.argv[:3] == ("autolab", "remote", "doctor")
    assert remote_intent.expected_writes == ()
    assert remote_intent.mutating is False

    uat_intent = build_uat_init_intent(state_path=state_path)
    assert uat_intent.argv[:3] == ("autolab", "uat", "init")
    assert "--suggest" in uat_intent.argv
    assert uat_intent.mutating is True
    assert "experiments/*/*/uat.md" in uat_intent.expected_writes

    hooks_intent = build_hooks_install_intent(state_path=state_path)
    assert hooks_intent.argv[:3] == ("autolab", "hooks", "install")
    assert hooks_intent.mutating is True
    assert hooks_intent.expected_writes == (".git/hooks/post-commit",)


def test_build_verify_intent_omits_blank_stage(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    verify_intent = build_verify_intent(state_path=state_path, stage="  ")
    assert "--stage" not in verify_intent.argv


def test_build_run_intent_invalid_run_agent_mode_falls_back_to_policy(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    intent = build_run_intent(
        state_path=state_path,
        options=RunActionOptions(
            verify=False,
            run_agent_mode="invalid",
            auto_decision=False,
        ),
    )
    assert "--run-agent" not in intent.argv
    assert "--no-run-agent" not in intent.argv


def test_build_human_review_intent_validates_status(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    intent = build_human_review_intent(state_path=state_path, status="retry")
    assert intent.action_id == "resolve_human_review"
    assert intent.argv[:3] == ("autolab", "review", "--state-file")
    assert intent.argv[-2:] == ("--status", "retry")
    assert ".autolab/state.json" in intent.expected_writes
    assert ".autolab/agent_result.json" in intent.expected_writes
    assert ".autolab/logs/orchestrator.log" in intent.expected_writes
    assert ".autolab/backlog.yaml" in intent.expected_writes
    assert intent.mutating is True

    with pytest.raises(ValueError):
        build_human_review_intent(state_path=state_path, status="invalid")


def test_lock_break_and_editor_intent_invariants(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    lock_break_intent = build_lock_break_intent(state_path=state_path, reason="   ")
    assert lock_break_intent.action_id == "lock_break"
    assert lock_break_intent.argv[-2:] == ("--reason", "tui manual break")
    assert lock_break_intent.mutating is True

    target = tmp_path / "docs" / "notes.md"
    monkeypatch.delenv("EDITOR", raising=False)
    editor_intent = build_open_in_editor_intent(target_path=target, cwd=tmp_path)
    assert editor_intent.action_id == "open_selected_artifact_editor"
    assert editor_intent.argv[0] == "cursor"
    assert editor_intent.argv[-1] == str(target)
    assert editor_intent.expected_writes == (str(target),)
    assert editor_intent.mutating is False

    editor_action = next(
        action
        for action in list_actions()
        if action.action_id == "open_selected_artifact_editor"
    )
    assert editor_action.requires_confirmation is True
    assert editor_action.requires_arm is False


def test_build_focus_intent_respects_optional_identifiers(tmp_path: Path) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    intent = build_focus_intent(
        state_path=state_path,
        experiment_id="e10",
        iteration_id="iter10",
    )
    assert intent.action_id == "focus_experiment"
    assert intent.argv[:3] == ("autolab", "focus", "--state-file")
    assert "--experiment-id" in intent.argv
    assert "--iteration-id" in intent.argv
    assert intent.mutating is True
    assert ".autolab/state.json" in intent.expected_writes

    no_ids = build_focus_intent(state_path=state_path)
    assert "--experiment-id" not in no_ids.argv
    assert "--iteration-id" not in no_ids.argv


def test_build_experiment_create_intent_includes_optional_hypothesis(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    with_hypothesis = build_experiment_create_intent(
        state_path=state_path,
        experiment_id="e2",
        iteration_id="iter2",
        hypothesis_id="h7",
    )
    assert with_hypothesis.action_id == "experiment_create"
    assert with_hypothesis.argv[:4] == (
        "autolab",
        "experiment",
        "create",
        "--state-file",
    )
    assert "--experiment-id" in with_hypothesis.argv
    assert "--iteration-id" in with_hypothesis.argv
    assert with_hypothesis.argv[-2:] == ("--hypothesis-id", "h7")
    assert "experiments/plan/iter2" in with_hypothesis.expected_writes

    without_hypothesis = build_experiment_create_intent(
        state_path=state_path,
        experiment_id="e3",
        iteration_id="iter3",
        hypothesis_id="",
    )
    assert "--hypothesis-id" not in without_hypothesis.argv


def test_build_experiment_move_intent_includes_required_target_type(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / ".autolab" / "state.json"
    intent = build_experiment_move_intent(
        state_path=state_path,
        to_type="in_progress",
        experiment_id="e1",
        iteration_id="iter1",
    )
    assert intent.action_id == "experiment_move"
    assert intent.argv[:4] == (
        "autolab",
        "experiment",
        "move",
        "--state-file",
    )
    assert "--experiment-id" in intent.argv
    assert "--iteration-id" in intent.argv
    assert intent.argv[-2:] == ("--to", "in_progress")
    assert "experiments/in_progress/iter1" in intent.expected_writes
    assert ".autolab/backlog.yaml" in intent.expected_writes
