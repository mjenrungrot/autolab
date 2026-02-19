"""End-to-end tests for assistant mode lifecycle.

Covers:
  1. Task selection from todo_state.json via select_open_task
  2. Task completion evidence artifacts (.autolab/task_completions/<task_id>.json)
  3. No-tasks-remaining behaviour (auto_complete on/off)
  4. State transitions through the assistant cycle stages
     (select -> implement -> verify -> review -> done)
  5. Meaningful change evaluation via _evaluate_meaningful_change
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

from autolab.constants import ACTIVE_STAGES, ASSISTANT_CYCLE_STAGES, TERMINAL_STAGES
from autolab.models import (
    GuardrailConfig,
    MeaningfulChangeConfig,
    RunOutcome,
    StateError,
)
from autolab.run_assistant import (
    _append_task_ledger,
    _assistant_target_stage,
    _run_once_assistant,
)
from autolab.todo_sync import (
    _load_todo_state,
    _open_tasks_sorted,
    _upsert_task,
    mark_task_completed,
    select_open_task,
)
from autolab.utils import _evaluate_meaningful_change, _write_json


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _make_state(
    *,
    stage: str = "implementation",
    stage_attempt: int = 0,
    max_stage_attempts: int = 3,
    iteration_id: str = "iter_test_001",
    experiment_id: str = "e1",
    assistant_mode: str = "on",
    current_task_id: str = "",
    task_cycle_stage: str = "select",
    repeat_guard: dict[str, Any] | None = None,
    task_change_baseline: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "stage": stage,
        "stage_attempt": stage_attempt,
        "last_run_id": "",
        "sync_status": "",
        "max_stage_attempts": max_stage_attempts,
        "max_total_iterations": 10,
        "assistant_mode": assistant_mode,
        "current_task_id": current_task_id,
        "task_cycle_stage": task_cycle_stage,
        "repeat_guard": repeat_guard
        or {
            "last_decision": "",
            "same_decision_streak": 0,
            "last_open_task_count": -1,
            "no_progress_decisions": 0,
            "update_docs_cycle_count": 0,
            "last_verification_passed": False,
        },
        "task_change_baseline": task_change_baseline or {},
    }


def _scaffold_repo(tmp_path: Path) -> Path:
    """Create a minimal repo with required directory structure."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autolab").mkdir()
    (repo / "docs").mkdir()
    _write(repo / "docs" / "todo.md", "# TODO\n\n## Tasks\n\n## Notes\nnotes\n")
    return repo


def _write_state(repo: Path, state: dict[str, Any]) -> Path:
    state_path = repo / ".autolab" / "state.json"
    _write_json_file(state_path, state)
    return state_path


def _read_state(repo: Path) -> dict[str, Any]:
    state_path = repo / ".autolab" / "state.json"
    return json.loads(state_path.read_text(encoding="utf-8"))


def _seed_todo_state(repo: Path, tasks: list[dict[str, Any]]) -> None:
    """Write a todo_state.json with the given task list."""
    todo_state: dict[str, Any] = {"version": 1, "next_order": 1, "tasks": {}}
    now = "2026-01-15T00:00:00Z"
    for task_def in tasks:
        task_id = task_def["task_id"]
        todo_state["tasks"][task_id] = {
            "task_id": task_id,
            "source": task_def.get("source", "manual"),
            "scope": task_def.get("scope", "manual_user"),
            "stage": task_def.get("stage", "implementation"),
            "task_class": task_def.get("task_class", "feature"),
            "text": task_def.get("text", f"Task {task_id}"),
            "text_key": task_def.get("text_key", f"task {task_id}"),
            "status": task_def.get("status", "open"),
            "first_seen_order": todo_state["next_order"],
            "first_seen_at": now,
            "last_seen_at": now,
            "last_evidence_at": "",
            "priority": task_def.get("priority", None),
        }
        todo_state["next_order"] += 1
    _write_json_file(repo / ".autolab" / "todo_state.json", todo_state)


def _default_meaningful_config(
    *,
    require_verification: bool = False,
    exclude_paths: tuple[str, ...] = (".autolab/**", "docs/todo.md"),
) -> MeaningfulChangeConfig:
    return MeaningfulChangeConfig(
        require_verification=require_verification,
        require_implementation_progress=True,
        require_git_for_progress=False,
        on_non_git_behavior="warn_and_continue",
        exclude_paths=exclude_paths,
    )


def _default_guardrail_config() -> GuardrailConfig:
    return GuardrailConfig(
        max_same_decision_streak=3,
        max_no_progress_decisions=2,
        max_generated_todo_tasks=5,
        max_update_docs_cycles=3,
        on_breach="human_review",
    )


# ===========================================================================
# 1. Task selection
# ===========================================================================


class TestTaskSelection:
    """Tests for select_open_task picking the right task from todo_state."""

    def test_select_returns_first_open_task(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(
            repo,
            [
                {"task_id": "task_aaa", "stage": "design", "text": "Design the thing"},
                {
                    "task_id": "task_bbb",
                    "stage": "implementation",
                    "text": "Build the thing",
                },
            ],
        )
        result = select_open_task(repo)
        assert result is not None
        assert result["task_id"] == "task_aaa"
        assert result["stage"] == "design"

    def test_select_returns_none_when_empty(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(repo, [])
        result = select_open_task(repo)
        assert result is None

    def test_select_skips_completed_tasks(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(
            repo,
            [
                {"task_id": "task_done", "stage": "design", "status": "completed"},
                {
                    "task_id": "task_open",
                    "stage": "implementation",
                    "text": "Open task",
                },
            ],
        )
        result = select_open_task(repo)
        assert result is not None
        assert result["task_id"] == "task_open"

    def test_select_returns_none_when_all_completed(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(
            repo,
            [
                {"task_id": "task_done1", "stage": "design", "status": "completed"},
                {
                    "task_id": "task_done2",
                    "stage": "implementation",
                    "status": "completed",
                },
            ],
        )
        result = select_open_task(repo)
        assert result is None

    def test_select_prioritizes_implementation_when_requested(
        self, tmp_path: Path
    ) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(
            repo,
            [
                {"task_id": "task_design", "stage": "design", "text": "Design task"},
                {
                    "task_id": "task_impl",
                    "stage": "implementation",
                    "text": "Impl task",
                },
            ],
        )
        result = select_open_task(repo, prioritize_implementation=True)
        assert result is not None
        assert result["task_id"] == "task_impl"
        assert result["stage"] == "implementation"

    def test_select_respects_priority_ordering(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(
            repo,
            [
                {"task_id": "task_low", "stage": "implementation", "priority": "low"},
                {"task_id": "task_high", "stage": "implementation", "priority": "high"},
            ],
        )
        result = select_open_task(repo)
        assert result is not None
        assert result["task_id"] == "task_high"

    def test_select_prefers_manual_over_generated(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(
            repo,
            [
                {
                    "task_id": "task_gen",
                    "stage": "implementation",
                    "source": "generated",
                },
                {
                    "task_id": "task_manual",
                    "stage": "implementation",
                    "source": "manual",
                },
            ],
        )
        result = select_open_task(repo)
        assert result is not None
        assert result["task_id"] == "task_manual"


# ===========================================================================
# 2. Task completion evidence
# ===========================================================================


class TestTaskCompletionEvidence:
    """Tests that the review stage writes .autolab/task_completions/<task_id>.json."""

    def _run_review_pass(
        self, repo: Path, task_id: str, *, changed: list[str], meaningful: list[str]
    ) -> RunOutcome:
        """Set up state and mocks to exercise the review branch that writes evidence."""
        state = _make_state(
            stage="implementation",
            current_task_id=task_id,
            task_cycle_stage="review",
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 0,
                "last_verification_passed": True,
            },
            task_change_baseline={},
        )
        state_path = _write_state(repo, state)
        _seed_todo_state(
            repo,
            [
                {
                    "task_id": task_id,
                    "stage": "implementation",
                    "text": "Write evidence test",
                }
            ],
        )

        mock_config = _default_meaningful_config()
        mock_snapshot: dict[str, str] = {}

        with (
            mock.patch(
                "autolab.run_assistant._load_meaningful_change_config",
                return_value=mock_config,
            ),
            mock.patch(
                "autolab.run_assistant._evaluate_meaningful_change",
                return_value=(True, changed, meaningful, mock_snapshot),
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch("autolab.run_assistant._persist_agent_result"),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(False, ""),
            ),
            mock.patch(
                "autolab.run_assistant._collect_change_snapshot", return_value={}
            ),
            mock.patch(
                "autolab.run_assistant._assistant_commit_paths",
                return_value=("src/foo.py",),
            ),
            mock.patch("autolab.run_assistant.mark_task_completed", return_value=True),
            mock.patch("subprocess.run") as mock_subprocess,
        ):
            mock_subprocess.return_value = mock.Mock(returncode=0, stdout="abc123\n")
            outcome = _run_once_assistant(state_path)
        return outcome

    def test_review_pass_writes_evidence_file(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        task_id = "task_evidence_01"
        changed = ["src/foo.py", ".autolab/state.json"]
        meaningful = ["src/foo.py"]

        outcome = self._run_review_pass(
            repo, task_id, changed=changed, meaningful=meaningful
        )

        evidence_path = repo / ".autolab" / "task_completions" / f"{task_id}.json"
        assert evidence_path.exists(), "Evidence file must be written on review pass"

        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
        assert evidence["task_id"] == task_id
        assert evidence["verification_passed"] is True
        assert "completed_at" in evidence
        assert evidence["changed_files"] == sorted(changed)
        assert evidence["meaningful_files"] == sorted(meaningful)

    def test_review_pass_outcome_marks_commit_allowed(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        outcome = self._run_review_pass(
            repo, "task_commit_01", changed=["src/x.py"], meaningful=["src/x.py"]
        )
        assert outcome.commit_allowed is True
        assert outcome.commit_cycle_stage == "review"

    def test_review_pass_clears_task_and_sets_done(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        self._run_review_pass(
            repo, "task_clear_01", changed=["src/a.py"], meaningful=["src/a.py"]
        )
        state = _read_state(repo)
        assert state["current_task_id"] == ""
        assert state["task_cycle_stage"] == "done"
        assert state["task_change_baseline"] == {}


# ===========================================================================
# 3. No tasks remaining
# ===========================================================================


class TestNoTasksRemaining:
    """Tests for behaviour when no open tasks are in todo_state."""

    def _run_with_no_tasks(
        self,
        repo: Path,
        *,
        auto_complete: bool = True,
        auto_mode: bool = False,
    ) -> RunOutcome:
        state = _make_state(
            stage="implementation",
            task_cycle_stage="select",
            current_task_id="",
        )
        state_path = _write_state(repo, state)
        _seed_todo_state(repo, [])

        with (
            mock.patch("autolab.run_assistant.select_open_task", return_value=None),
            mock.patch(
                "autolab.run_assistant._load_assistant_auto_complete_policy",
                return_value=auto_complete,
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch("autolab.run_assistant._persist_agent_result"),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(False, ""),
            ),
            mock.patch(
                "autolab.run_assistant._mark_backlog_experiment_completed",
                return_value=(False, None, "nothing to mark"),
            ),
        ):
            outcome = _run_once_assistant(state_path, auto_mode=auto_mode)
        return outcome

    def test_no_tasks_auto_complete_true_transitions_to_stop(
        self, tmp_path: Path
    ) -> None:
        repo = _scaffold_repo(tmp_path)
        outcome = self._run_with_no_tasks(repo, auto_complete=True)
        assert outcome.stage_after == "stop"
        assert "no actionable tasks" in outcome.message
        state = _read_state(repo)
        assert state["stage"] == "stop"
        assert state["task_cycle_stage"] == "done"
        assert state["current_task_id"] == ""

    def test_no_tasks_auto_complete_false_transitions_to_human_review(
        self, tmp_path: Path
    ) -> None:
        repo = _scaffold_repo(tmp_path)
        outcome = self._run_with_no_tasks(repo, auto_complete=False)
        assert outcome.stage_after == "human_review"
        assert "auto_complete_backlog=false" in outcome.message
        state = _read_state(repo)
        assert state["stage"] == "human_review"
        assert state["task_cycle_stage"] == "done"


# ===========================================================================
# 4. State transitions through assistant cycle stages
# ===========================================================================


class TestAssistantCycleStages:
    """Tests verifying correct transitions through select -> implement -> verify -> review -> done."""

    def test_select_transition_picks_task_and_sets_implement(
        self, tmp_path: Path
    ) -> None:
        repo = _scaffold_repo(tmp_path)
        task_id = "task_select_01"
        state = _make_state(
            stage="implementation",
            task_cycle_stage="select",
            current_task_id="",
        )
        state_path = _write_state(repo, state)
        _seed_todo_state(
            repo,
            [
                {
                    "task_id": task_id,
                    "stage": "implementation",
                    "task_class": "feature",
                    "text": "Build widget",
                },
            ],
        )

        fake_task = {
            "task_id": task_id,
            "source": "manual",
            "stage": "implementation",
            "task_class": "feature",
            "text": "Build widget",
        }

        with (
            mock.patch(
                "autolab.run_assistant.select_open_task", return_value=fake_task
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch("autolab.run_assistant._persist_agent_result"),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(False, ""),
            ),
            mock.patch(
                "autolab.run_assistant._collect_change_snapshot", return_value={}
            ),
        ):
            outcome = _run_once_assistant(state_path)

        assert outcome.commit_cycle_stage == "select"
        assert outcome.commit_allowed is False
        state = _read_state(repo)
        assert state["task_cycle_stage"] == "implement"
        assert state["current_task_id"] == task_id

    def test_implement_runs_standard_then_sets_verify(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        task_id = "task_impl_01"
        state = _make_state(
            stage="implementation",
            task_cycle_stage="implement",
            current_task_id=task_id,
        )
        state_path = _write_state(repo, state)

        standard_outcome = RunOutcome(
            exit_code=0,
            transitioned=False,
            stage_before="implementation",
            stage_after="implementation",
            message="standard run ok",
        )

        with (
            mock.patch(
                "autolab.run_assistant._run_once_standard",
                return_value=standard_outcome,
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(False, ""),
            ),
        ):
            outcome = _run_once_assistant(state_path)

        assert "verify" in outcome.message
        state = _read_state(repo)
        assert state["task_cycle_stage"] == "verify"

    def test_verify_pass_sets_review(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        task_id = "task_verify_01"
        state = _make_state(
            stage="implementation",
            task_cycle_stage="verify",
            current_task_id=task_id,
        )
        state_path = _write_state(repo, state)

        with (
            mock.patch(
                "autolab.run_assistant._run_verification_step",
                return_value=(True, "all checks passed"),
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch("autolab.run_assistant._persist_agent_result"),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(False, ""),
            ),
        ):
            outcome = _run_once_assistant(state_path)

        assert outcome.commit_cycle_stage == "verify"
        state = _read_state(repo)
        assert state["task_cycle_stage"] == "review"
        assert state["repeat_guard"]["last_verification_passed"] is True

    def test_verify_fail_sets_implement(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        task_id = "task_verify_fail"
        state = _make_state(
            stage="implementation",
            task_cycle_stage="verify",
            current_task_id=task_id,
        )
        state_path = _write_state(repo, state)

        with (
            mock.patch(
                "autolab.run_assistant._run_verification_step",
                return_value=(False, "tests failed"),
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch("autolab.run_assistant._persist_agent_result"),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(False, ""),
            ),
        ):
            outcome = _run_once_assistant(state_path)

        state = _read_state(repo)
        assert state["task_cycle_stage"] == "implement"
        assert state["repeat_guard"]["last_verification_passed"] is False

    def test_review_no_meaningful_change_loops_back_to_implement(
        self, tmp_path: Path
    ) -> None:
        repo = _scaffold_repo(tmp_path)
        task_id = "task_review_fail"
        state = _make_state(
            stage="implementation",
            task_cycle_stage="review",
            current_task_id=task_id,
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 0,
                "last_verification_passed": True,
            },
        )
        state_path = _write_state(repo, state)

        mock_config = _default_meaningful_config()

        with (
            mock.patch(
                "autolab.run_assistant._load_meaningful_change_config",
                return_value=mock_config,
            ),
            mock.patch(
                "autolab.run_assistant._evaluate_meaningful_change",
                return_value=(False, [], [], {}),
            ),
            mock.patch(
                "autolab.run_assistant._load_guardrail_config",
                return_value=_default_guardrail_config(),
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch("autolab.run_assistant._persist_agent_result"),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(False, ""),
            ),
        ):
            outcome = _run_once_assistant(state_path)

        assert outcome.commit_allowed is False
        assert "blocked" in outcome.message
        state = _read_state(repo)
        assert state["task_cycle_stage"] == "implement"
        assert state["repeat_guard"]["no_progress_decisions"] == 1

    def test_done_cycle_triggers_new_select(self, tmp_path: Path) -> None:
        """When task_cycle_stage is 'done', assistant should start a new selection."""
        repo = _scaffold_repo(tmp_path)
        state = _make_state(
            stage="implementation",
            task_cycle_stage="done",
            current_task_id="",
        )
        state_path = _write_state(repo, state)

        fake_task = {
            "task_id": "task_next",
            "source": "manual",
            "stage": "implementation",
            "task_class": "feature",
            "text": "Next task",
        }

        with (
            mock.patch(
                "autolab.run_assistant.select_open_task", return_value=fake_task
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch("autolab.run_assistant._persist_agent_result"),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(False, ""),
            ),
            mock.patch(
                "autolab.run_assistant._collect_change_snapshot", return_value={}
            ),
        ):
            outcome = _run_once_assistant(state_path)

        state = _read_state(repo)
        assert state["task_cycle_stage"] == "implement"
        assert state["current_task_id"] == "task_next"


# ===========================================================================
# 5. Meaningful change evaluation
# ===========================================================================


class TestMeaningfulChangeEvaluation:
    """Tests for _evaluate_meaningful_change logic."""

    def test_no_git_changes_returns_not_meaningful(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        config = _default_meaningful_config()
        with mock.patch("autolab.utils._collect_change_snapshot", return_value={}):
            meaningful, changed, meaningful_files, snapshot = (
                _evaluate_meaningful_change(
                    repo,
                    config,
                    baseline_snapshot={},
                )
            )
        assert meaningful is False
        assert changed == []
        assert meaningful_files == []

    def test_excluded_paths_are_filtered(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        config = _default_meaningful_config(
            exclude_paths=(".autolab/**", "docs/todo.md")
        )
        current = {
            ".autolab/state.json": "M:abc123",
            "docs/todo.md": "M:def456",
        }
        with mock.patch("autolab.utils._collect_change_snapshot", return_value=current):
            meaningful, changed, meaningful_files, snapshot = (
                _evaluate_meaningful_change(
                    repo,
                    config,
                    baseline_snapshot={},
                )
            )
        assert meaningful is False
        assert len(changed) == 2
        assert meaningful_files == []

    def test_source_file_changes_are_meaningful(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        config = _default_meaningful_config()
        current = {
            "src/model.py": "M:aaa111",
            ".autolab/state.json": "M:bbb222",
        }
        with mock.patch("autolab.utils._collect_change_snapshot", return_value=current):
            meaningful, changed, meaningful_files, snapshot = (
                _evaluate_meaningful_change(
                    repo,
                    config,
                    baseline_snapshot={},
                )
            )
        assert meaningful is True
        assert "src/model.py" in meaningful_files
        assert ".autolab/state.json" not in meaningful_files

    def test_baseline_delta_detects_new_changes_only(self, tmp_path: Path) -> None:
        """Only files that differ from baseline should be considered changed."""
        repo = _scaffold_repo(tmp_path)
        config = _default_meaningful_config()
        baseline = {"src/old.py": "M:same_hash"}
        current = {
            "src/old.py": "M:same_hash",
            "src/new.py": "M:new_hash",
        }
        with mock.patch("autolab.utils._collect_change_snapshot", return_value=current):
            meaningful, changed, meaningful_files, snapshot = (
                _evaluate_meaningful_change(
                    repo,
                    config,
                    baseline_snapshot=baseline,
                )
            )
        assert meaningful is True
        assert "src/new.py" in meaningful_files
        assert "src/old.py" not in changed

    def test_baseline_delta_detects_modified_files(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        config = _default_meaningful_config()
        baseline = {"src/changed.py": "M:old_hash"}
        current = {"src/changed.py": "M:new_hash"}
        with mock.patch("autolab.utils._collect_change_snapshot", return_value=current):
            meaningful, changed, meaningful_files, snapshot = (
                _evaluate_meaningful_change(
                    repo,
                    config,
                    baseline_snapshot=baseline,
                )
            )
        assert meaningful is True
        assert "src/changed.py" in meaningful_files

    def test_no_baseline_treats_all_current_as_changed(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        config = _default_meaningful_config()
        current = {"src/a.py": "M:h1", "src/b.py": "A:h2"}
        with mock.patch("autolab.utils._collect_change_snapshot", return_value=current):
            meaningful, changed, meaningful_files, snapshot = (
                _evaluate_meaningful_change(
                    repo,
                    config,
                    baseline_snapshot=None,
                )
            )
        assert meaningful is True
        assert set(meaningful_files) == {"src/a.py", "src/b.py"}


# ===========================================================================
# 6. Assistant target stage resolution
# ===========================================================================


class TestAssistantTargetStage:
    """Tests for _assistant_target_stage mapping tasks to workflow stages."""

    def test_active_stage_in_task_is_preserved(self) -> None:
        for stage in ACTIVE_STAGES:
            task = {"stage": stage, "task_class": "feature"}
            assert _assistant_target_stage(task) == stage

    def test_docs_class_defaults_to_update_docs(self) -> None:
        task = {"stage": "", "task_class": "docs"}
        assert _assistant_target_stage(task) == "update_docs"

    def test_experiment_class_defaults_to_design(self) -> None:
        task = {"stage": "", "task_class": "experiment"}
        assert _assistant_target_stage(task) == "design"

    def test_unknown_class_defaults_to_implementation(self) -> None:
        task = {"stage": "", "task_class": "unknown"}
        assert _assistant_target_stage(task) == "implementation"

    def test_missing_stage_and_class(self) -> None:
        task: dict[str, Any] = {}
        assert _assistant_target_stage(task) == "implementation"


# ===========================================================================
# 7. Task ledger writing
# ===========================================================================


class TestTaskLedger:
    """Tests for _append_task_ledger producing JSONL records."""

    def test_ledger_appends_valid_jsonl(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _append_task_ledger(
            repo,
            event="select",
            task_id="task_led_01",
            stage_before="implementation",
            stage_after="implementation",
            transitioned=False,
            status="complete",
            exit_code=0,
            message="selected task",
        )
        ledger_path = repo / ".autolab" / "task_history.jsonl"
        assert ledger_path.exists()
        line = ledger_path.read_text(encoding="utf-8").strip()
        record = json.loads(line)
        assert record["event"] == "select"
        assert record["task_id"] == "task_led_01"
        assert record["exit_code"] == 0

    def test_ledger_includes_verification_when_provided(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _append_task_ledger(
            repo,
            event="verify",
            task_id="task_led_02",
            stage_before="implementation",
            stage_after="implementation",
            transitioned=False,
            status="complete",
            exit_code=0,
            message="verified",
            verification_passed=True,
            verification_message="all good",
        )
        ledger_path = repo / ".autolab" / "task_history.jsonl"
        record = json.loads(ledger_path.read_text(encoding="utf-8").strip())
        assert record["verification"]["passed"] is True
        assert record["verification"]["message"] == "all good"

    def test_ledger_includes_commit_decision_when_provided(
        self, tmp_path: Path
    ) -> None:
        repo = _scaffold_repo(tmp_path)
        _append_task_ledger(
            repo,
            event="review",
            task_id="task_led_03",
            stage_before="implementation",
            stage_after="implementation",
            transitioned=False,
            status="complete",
            exit_code=0,
            message="review passed",
            commit_allowed=True,
            commit_reason="meaningful change",
        )
        ledger_path = repo / ".autolab" / "task_history.jsonl"
        record = json.loads(ledger_path.read_text(encoding="utf-8").strip())
        assert record["commit_decision"]["allowed"] is True
        assert record["commit_decision"]["reason"] == "meaningful change"

    def test_ledger_appends_multiple_entries(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        for i in range(3):
            _append_task_ledger(
                repo,
                event=f"event_{i}",
                task_id=f"task_{i}",
                stage_before="implementation",
                stage_after="implementation",
                transitioned=False,
                status="complete",
                exit_code=0,
                message=f"entry {i}",
            )
        ledger_path = repo / ".autolab" / "task_history.jsonl"
        lines = [
            l for l in ledger_path.read_text(encoding="utf-8").strip().split("\n") if l
        ]
        assert len(lines) == 3


# ===========================================================================
# 8. Guardrail breach on repeated no-progress
# ===========================================================================


class TestGuardrailBreach:
    """Test that repeated no-progress in review triggers escalation."""

    def test_no_progress_breach_escalates_to_human_review(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        task_id = "task_guardrail_01"
        state = _make_state(
            stage="implementation",
            task_cycle_stage="review",
            current_task_id=task_id,
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 1,  # one away from max (2)
                "update_docs_cycle_count": 0,
                "last_verification_passed": True,
            },
        )
        state_path = _write_state(repo, state)

        mock_config = _default_meaningful_config()
        guardrail = _default_guardrail_config()

        with (
            mock.patch(
                "autolab.run_assistant._load_meaningful_change_config",
                return_value=mock_config,
            ),
            mock.patch(
                "autolab.run_assistant._evaluate_meaningful_change",
                return_value=(False, [], [], {}),
            ),
            mock.patch(
                "autolab.run_assistant._load_guardrail_config", return_value=guardrail
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch("autolab.run_assistant._persist_agent_result"),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(False, ""),
            ),
            mock.patch("autolab.run_assistant._write_guardrail_breach"),
        ):
            outcome = _run_once_assistant(state_path, auto_mode=True)

        assert outcome.exit_code == 1
        assert "guardrail" in outcome.message.lower()
        state = _read_state(repo)
        assert state["stage"] == "human_review"
        assert state["task_cycle_stage"] == "done"
        assert state["current_task_id"] == ""


# ===========================================================================
# 9. mark_task_completed in todo_sync
# ===========================================================================


class TestMarkTaskCompleted:
    """Tests for the mark_task_completed utility."""

    def test_mark_completed_removes_task_from_open_list(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(
            repo,
            [
                {
                    "task_id": "task_mc_01",
                    "stage": "implementation",
                    "text": "Finish it",
                },
                {"task_id": "task_mc_02", "stage": "design", "text": "Plan it"},
            ],
        )

        result = mark_task_completed(repo, "task_mc_01")
        assert result is True

        todo_state = json.loads(
            (repo / ".autolab" / "todo_state.json").read_text(encoding="utf-8")
        )
        # Completed tasks are pruned
        assert "task_mc_01" not in todo_state["tasks"]
        remaining = [t for t in todo_state["tasks"].values() if t["status"] == "open"]
        assert len(remaining) == 1
        assert remaining[0]["task_id"] == "task_mc_02"

    def test_mark_completed_returns_false_for_missing_task(
        self, tmp_path: Path
    ) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(repo, [])
        result = mark_task_completed(repo, "nonexistent_task")
        assert result is False

    def test_mark_completed_returns_false_for_empty_id(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(repo, [])
        result = mark_task_completed(repo, "")
        assert result is False

    def test_mark_completed_updates_todo_md(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        _seed_todo_state(
            repo,
            [
                {
                    "task_id": "task_mc_03",
                    "stage": "implementation",
                    "text": "Write tests",
                },
            ],
        )
        mark_task_completed(repo, "task_mc_03")

        todo_md = (repo / "docs" / "todo.md").read_text(encoding="utf-8")
        assert "Write tests" not in todo_md


# ===========================================================================
# 10. Completed experiment blocks further editing
# ===========================================================================


class TestCompletedExperimentBlocking:
    """Test that a completed experiment in backlog stops the assistant."""

    def test_completed_experiment_transitions_to_stop(self, tmp_path: Path) -> None:
        repo = _scaffold_repo(tmp_path)
        state = _make_state(
            stage="implementation",
            task_cycle_stage="implement",
            current_task_id="task_exp_01",
        )
        state_path = _write_state(repo, state)

        with (
            mock.patch(
                "autolab.run_assistant._is_active_experiment_completed",
                return_value=(True, "experiment e1 is done"),
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_pre_sync", return_value=([], "")
            ),
            mock.patch(
                "autolab.run_assistant._safe_todo_post_sync", return_value=([], "")
            ),
            mock.patch("autolab.run_assistant._persist_agent_result"),
            mock.patch(
                "autolab.run_assistant._detect_priority_host_mode", return_value="local"
            ),
            mock.patch("autolab.run_assistant._write_block_reason"),
        ):
            outcome = _run_once_assistant(state_path)

        assert outcome.stage_after == "stop"
        assert outcome.transitioned is True
        state = _read_state(repo)
        assert state["stage"] == "stop"
        assert state["task_cycle_stage"] == "done"
