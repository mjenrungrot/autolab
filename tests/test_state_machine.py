"""Integration tests for the autolab state machine.

Covers happy-path transitions, retry cycles, guardrail breaches,
assistant-mode behaviour, and the fixes for GAP 1 & GAP 2.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import yaml

from autolab.__main__ import (
    TERMINAL_STAGES,
    RunOutcome,
    _run_once,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_state(
    *,
    stage: str = "hypothesis",
    stage_attempt: int = 0,
    max_stage_attempts: int = 3,
    iteration_id: str = "iter_test_001",
    experiment_id: str = "e1",
    assistant_mode: str = "off",
    current_task_id: str = "",
    task_cycle_stage: str = "select",
    repeat_guard: dict[str, Any] | None = None,
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
        "task_change_baseline": {},
    }


def _write_state(repo: Path, state: dict[str, Any]) -> Path:
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state_path


def _read_state(repo: Path) -> dict[str, Any]:
    state_path = repo / ".autolab" / "state.json"
    return json.loads(state_path.read_text(encoding="utf-8"))


def _write_backlog(
    repo: Path,
    *,
    experiment_id: str = "e1",
    iteration_id: str = "iter_test_001",
    status: str = "open",
    hypothesis_status: str = "open",
) -> None:
    backlog = {
        "hypotheses": [
            {
                "id": "h1",
                "status": hypothesis_status,
                "title": "Test hypothesis",
                "success_metric": "metric",
                "target_delta": 0.0,
            },
        ],
        "experiments": [
            {
                "id": experiment_id,
                "hypothesis_id": "h1",
                "status": status,
                "iteration_id": iteration_id,
            },
        ],
    }
    path = repo / ".autolab" / "backlog.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(backlog, sort_keys=False), encoding="utf-8")


def _write_policy(repo: Path, *, guardrails: dict[str, Any] | None = None) -> None:
    policy = {
        "test_command": "true",
        "dry_run_command": "true",
        "require_tests": False,
        "require_dry_run": False,
        "require_env_smoke": False,
        "require_docs_target_update": False,
        "launch": {
            "execute": True,
            "local_timeout_seconds": 900,
            "slurm_submit_timeout_seconds": 30,
        },
        "template_fill": {"enabled": False},
        "agent_runner": {"enabled": False, "stages": []},
        "autorun": {
            "guardrails": guardrails
            or {
                "max_same_decision_streak": 3,
                "max_no_progress_decisions": 2,
                "max_update_docs_cycles": 3,
                "on_breach": "human_review",
            },
            "auto_commit": {"mode": "off"},
            "meaningful_change": {
                "require_implementation_progress": False,
                "require_git_for_progress": False,
                "on_non_git_behavior": "warn_and_continue",
                "require_verification": False,
                "exclude_paths": [],
            },
        },
    }
    path = repo / ".autolab" / "verifier_policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")


def _write_todo_state(repo: Path, tasks: dict[str, Any] | None = None) -> None:
    payload = {"version": 1, "next_order": 1, "tasks": tasks or {}}
    path = repo / ".autolab" / "todo_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_todo_md(repo: Path, content: str = "") -> None:
    path = repo / "docs" / "todo.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _seed_iteration(repo: Path, iteration_id: str = "iter_test_001") -> Path:
    iteration_dir = repo / "experiments" / "plan" / iteration_id
    iteration_dir.mkdir(parents=True, exist_ok=True)
    return iteration_dir


def _seed_hypothesis(iteration_dir: Path) -> None:
    (iteration_dir / "hypothesis.md").write_text(
        (
            "# Hypothesis Statement\n\n"
            "## Primary Metric\n"
            "PrimaryMetric: accuracy; Unit: %; Success: baseline +2.0\n\n"
            "- metric: accuracy\n"
            "- metric_mode: maximize\n"
            "- target_delta: 2.0\n"
            "- criteria: improve top-1 accuracy by at least 2.0 points\n"
        ),
        encoding="utf-8",
    )


def _seed_design(iteration_dir: Path, iteration_id: str = "iter_test_001") -> None:
    design = {
        "id": "d1",
        "iteration_id": iteration_id,
        "hypothesis_id": "h1",
        "entrypoint": {"module": "train", "args": {}},
        "compute": {"location": "local", "gpus": 0},
        "metrics": ["loss"],
        "baselines": [{"name": "baseline1", "value": 1.0}],
    }
    (iteration_dir / "design.yaml").write_text(
        yaml.safe_dump(design, sort_keys=False), encoding="utf-8"
    )


def _seed_implementation(iteration_dir: Path) -> None:
    (iteration_dir / "implementation_plan.md").write_text(
        "# Implementation\nStep 1.", encoding="utf-8"
    )


def _seed_review_pass(iteration_dir: Path) -> None:
    (iteration_dir / "implementation_review.md").write_text(
        "# Review\nLGTM.", encoding="utf-8"
    )
    review = {
        "status": "pass",
        "blocking_findings": [],
        "required_checks": {
            "tests": "pass",
            "dry_run": "skip",
            "schema": "pass",
            "env_smoke": "skip",
            "docs_target_update": "skip",
        },
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    (iteration_dir / "review_result.json").write_text(
        json.dumps(review, indent=2), encoding="utf-8"
    )


def _seed_review_retry(iteration_dir: Path) -> None:
    (iteration_dir / "implementation_review.md").write_text(
        "# Review\nNeeds work.", encoding="utf-8"
    )
    review = {
        "status": "needs_retry",
        "blocking_findings": ["issue1"],
        "required_checks": {
            "tests": "fail",
            "dry_run": "skip",
            "schema": "pass",
            "env_smoke": "skip",
            "docs_target_update": "skip",
        },
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    (iteration_dir / "review_result.json").write_text(
        json.dumps(review, indent=2), encoding="utf-8"
    )


def _seed_launch(iteration_dir: Path, run_id: str = "run_001") -> None:
    if not (iteration_dir / "design.yaml").exists():
        _seed_design(iteration_dir, iteration_id=iteration_dir.name)
    if not (iteration_dir / "review_result.json").exists():
        _seed_review_pass(iteration_dir)
    launch_dir = iteration_dir / "launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    (launch_dir / "run_local.sh").write_text("#!/bin/bash\necho run", encoding="utf-8")
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "iteration_id": iteration_dir.name,
        "launch_mode": "local",
        "host_mode": "local",
        "command": "bash launch/run_local.sh",
        "resource_request": {},
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _seed_extract(iteration_dir: Path, run_id: str = "run_001") -> None:
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "iteration_id": iteration_dir.name,
        "launch_mode": "local",
        "host_mode": "local",
        "command": "bash launch/run_local.sh",
        "resource_request": {},
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    metrics = {
        "iteration_id": iteration_dir.name,
        "run_id": run_id,
        "status": "completed",
        "primary_metric": {
            "name": "loss",
            "value": 0.5,
            "delta_vs_baseline": 0.0,
        },
        "baseline_results": [],
        "variant_results": [],
    }
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )


def _seed_update_docs(iteration_dir: Path) -> None:
    (iteration_dir / "docs_update.md").write_text(
        (
            "# Docs Update\n\n"
            "- run_id: run_001\n"
            "- metrics artifact: runs/run_001/metrics.json\n"
            "- manifest artifact: runs/run_001/run_manifest.json\n"
        ),
        encoding="utf-8",
    )
    analysis_dir = iteration_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "summary.md").write_text("# Summary\nResults.", encoding="utf-8")


def _setup_repo(
    tmp_path: Path,
    *,
    hypothesis_status: str = "open",
    backlog_experiment_status: str = "open",
    **state_kwargs: Any,
) -> tuple[Path, Path, Path]:
    """Set up a complete test repo with state, backlog, policy, and todo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _make_state(**state_kwargs)
    state_path = _write_state(repo, state)
    _write_backlog(
        repo,
        experiment_id=state.get("experiment_id", "e1"),
        iteration_id=state.get("iteration_id", "iter_test_001"),
        status=backlog_experiment_status,
        hypothesis_status=hypothesis_status,
    )
    _write_policy(repo)
    _write_todo_state(repo)
    _write_todo_md(repo)
    iteration_dir = _seed_iteration(repo, state.get("iteration_id", "iter_test_001"))
    return repo, state_path, iteration_dir


def _run(state_path: Path, **kwargs: Any) -> RunOutcome:
    """Run one cycle with agent runner disabled."""
    kwargs.setdefault("run_agent_mode", "force_off")
    kwargs.setdefault("strict_implementation_progress", False)
    with mock.patch("autolab.run_standard._generate_run_id", return_value="run_001"):
        return _run_once(state_path, kwargs.pop("decision", None), **kwargs)


# ---------------------------------------------------------------------------
# Phase 2: Happy Path (hypothesis → design → implementation → review)
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_hypothesis_to_design(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")
        _seed_hypothesis(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_before == "hypothesis"
        assert outcome.stage_after == "design"
        assert outcome.exit_code == 0
        persisted = _read_state(repo)
        assert persisted["stage"] == "design"

    def test_design_to_implementation(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="design")
        _seed_design(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "implementation"
        persisted = _read_state(repo)
        assert persisted["stage"] == "implementation"

    def test_implementation_to_review(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="implementation")
        _seed_implementation(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "implementation_review"
        persisted = _read_state(repo)
        assert persisted["stage"] == "implementation_review"

    def test_review_pass_to_launch(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="implementation_review")
        _seed_review_pass(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "launch"
        persisted = _read_state(repo)
        assert persisted["stage"] == "launch"

    def test_launch_to_slurm_monitor(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_launch(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        assert persisted["last_run_id"] == "run_001"

    def test_extract_to_update_docs(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="extract_results")
        _seed_extract(it_dir)
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "update_docs"
        persisted = _read_state(repo)
        assert persisted["stage"] == "update_docs"

    def test_update_docs_to_decide_repeat(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="update_docs")
        _seed_update_docs(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "decide_repeat"
        persisted = _read_state(repo)
        assert persisted["stage"] == "decide_repeat"

    def test_decide_repeat_to_stop(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")

        outcome = _run(state_path, decision="stop")

        assert outcome.transitioned
        assert outcome.stage_after == "stop"
        persisted = _read_state(repo)
        assert persisted["stage"] == "stop"

    def test_decide_repeat_to_hypothesis(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")

        outcome = _run(state_path, decision="hypothesis")

        assert outcome.transitioned
        assert outcome.stage_after == "hypothesis"
        persisted = _read_state(repo)
        assert persisted["stage"] == "hypothesis"
        assert persisted["stage_attempt"] == 0

    def test_terminal_stage_noop(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="human_review")

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert outcome.stage_after == "human_review"

    def test_stop_stage_noop(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="stop")

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert outcome.stage_after == "stop"


# ---------------------------------------------------------------------------
# Phase 3: Retry Cycle
# ---------------------------------------------------------------------------


class TestRetryCycle:
    def test_review_retry_increments_attempt(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="implementation_review", stage_attempt=0
        )
        _seed_review_retry(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "implementation"
        persisted = _read_state(repo)
        assert persisted["stage"] == "implementation"
        assert persisted["stage_attempt"] == 1

    def test_retry_carries_attempt_forward(self, tmp_path: Path) -> None:
        """impl(attempt=1) → impl_review should carry the attempt."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="implementation", stage_attempt=1
        )
        _seed_implementation(it_dir)

        outcome = _run(state_path)

        assert outcome.stage_after == "implementation_review"
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 1  # carried, not reset

    def test_retry_budget_exhausted_escalates(self, tmp_path: Path) -> None:
        """At max attempts, retry should escalate to human_review."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="implementation_review",
            stage_attempt=2,
            max_stage_attempts=3,
        )
        _seed_review_retry(it_dir)

        outcome = _run(state_path)

        assert outcome.stage_after == "human_review"
        assert "budget exhausted" in outcome.message
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"

    def test_stage_check_failure_increments_attempt(self, tmp_path: Path) -> None:
        """Missing stage artifacts should increment attempt and retry."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="hypothesis", stage_attempt=0
        )
        # Don't seed hypothesis.md — stage check will fail

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 1
        assert persisted["stage"] == "hypothesis"  # stays, not escalated yet

    def test_stage_check_failure_exhaustion(self, tmp_path: Path) -> None:
        """Repeated stage failures should escalate to human_review."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="hypothesis",
            stage_attempt=2,
            max_stage_attempts=3,
        )
        # Don't seed hypothesis.md

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"


# ---------------------------------------------------------------------------
# Phase 4: decide_repeat with --decision and --auto-decision
# ---------------------------------------------------------------------------


class TestDecideRepeat:
    def test_no_decision_pauses(self, tmp_path: Path) -> None:
        """At decide_repeat without --decision, nothing happens."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert outcome.stage_after == "decide_repeat"
        assert "requires --decision" in outcome.message

    def test_decide_stop_marks_backlog_completed(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")

        outcome = _run(state_path, decision="stop")

        assert outcome.stage_after == "stop"
        backlog = yaml.safe_load((repo / ".autolab" / "backlog.yaml").read_text())
        experiment = backlog["experiments"][0]
        assert experiment["status"] in {"done", "completed"}

    def test_decide_design_loops_back(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")

        outcome = _run(state_path, decision="design")

        assert outcome.transitioned
        assert outcome.stage_after == "design"
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 0

    def test_decide_repeat_uses_decision_result_artifact(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")
        payload = {
            "schema_version": "1.0",
            "decision": "design",
            "rationale": "More refinement is needed before stopping.",
            "evidence": [
                {
                    "source": "metrics",
                    "pointer": "runs/run_001/metrics.json",
                    "summary": "Target not met",
                }
            ],
            "risks": ["Underpowered result sample"],
        }
        (it_dir / "decision_result.json").write_text(
            json.dumps(payload, indent=2),
            encoding="utf-8",
        )

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "design"
        assert "decision_result.json" in outcome.message

    def test_decide_repeat_invalid_decision_result_blocks(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")
        (it_dir / "decision_result.json").write_text("{invalid json", encoding="utf-8")

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert outcome.stage_after == "decide_repeat"
        assert "Invalid decision artifact" in outcome.message


# ---------------------------------------------------------------------------
# Phase 5: Guardrail Tests
# ---------------------------------------------------------------------------


class TestGuardrails:
    def test_same_decision_streak_breach(self, tmp_path: Path) -> None:
        """Streak exceeding max should escalate to on_breach."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "design",
                "same_decision_streak": 3,  # max is 3, so one more = 4 > 3
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 0,
                "last_verification_passed": False,
            },
        )

        outcome = _run(
            state_path, decision="design", auto_mode=True, auto_decision=True
        )

        assert outcome.stage_after == "human_review"
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"

    def test_same_decision_streak_resets_on_different_decision(
        self, tmp_path: Path
    ) -> None:
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "design",
                "same_decision_streak": 2,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 0,
                "last_verification_passed": False,
            },
        )

        outcome = _run(
            state_path, decision="hypothesis", auto_mode=True, auto_decision=True
        )

        assert outcome.stage_after == "hypothesis"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["same_decision_streak"] == 1

    def test_update_docs_cycle_count_breach(self, tmp_path: Path) -> None:
        """update_docs_cycle_count exceeding max should escalate."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="extract_results",
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 3,  # max is 3, so +1 = 4 > 3
                "last_verification_passed": False,
            },
        )
        _seed_extract(it_dir)
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)

        outcome = _run(state_path)

        assert outcome.stage_after == "human_review"
        assert "update_docs cycle limit" in outcome.message
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"

    def test_update_docs_cycle_count_increments_normally(self, tmp_path: Path) -> None:
        """Normal extract → update_docs should increment the counter."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="extract_results",
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 1,
                "last_verification_passed": False,
            },
        )
        _seed_extract(it_dir)
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)

        outcome = _run(state_path)

        assert outcome.stage_after == "update_docs"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 2


# ---------------------------------------------------------------------------
# GAP 1: update_docs_cycle_count reset in decide_repeat
# ---------------------------------------------------------------------------


class TestGap1UpdateDocsCycleCountReset:
    def test_reset_on_non_terminal_transition(self, tmp_path: Path) -> None:
        """decide_repeat → hypothesis should reset update_docs_cycle_count to 0."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 2,
                "last_verification_passed": False,
            },
        )

        outcome = _run(state_path, decision="hypothesis")

        assert outcome.stage_after == "hypothesis"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 0

    def test_reset_on_design_transition(self, tmp_path: Path) -> None:
        """decide_repeat → design should also reset."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 3,
                "last_verification_passed": False,
            },
        )

        outcome = _run(state_path, decision="design")

        assert outcome.stage_after == "design"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 0

    def test_no_reset_on_stop_transition(self, tmp_path: Path) -> None:
        """decide_repeat → stop should NOT reset (terminal stage)."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 2,
                "last_verification_passed": False,
            },
        )

        outcome = _run(state_path, decision="stop")

        assert outcome.stage_after == "stop"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 2

    def test_no_reset_on_human_review_transition(self, tmp_path: Path) -> None:
        """decide_repeat → human_review should NOT reset."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 2,
                "last_verification_passed": False,
            },
        )

        outcome = _run(state_path, decision="human_review")

        assert outcome.stage_after == "human_review"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 2


# ---------------------------------------------------------------------------
# GAP 2: Assistant mode stop marks backlog completed
# ---------------------------------------------------------------------------


class TestGap2AssistantStopMarksBacklog:
    def test_assistant_no_tasks_marks_backlog_completed(self, tmp_path: Path) -> None:
        """Assistant mode with no tasks → stop should mark backlog experiment done.

        Use stage='human_review' (terminal) so the todo sync does NOT inject
        fallback tasks, leaving the task list genuinely empty.  The hypothesis
        is closed so no generated candidates are created either.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="human_review",
            assistant_mode="on",
            task_cycle_stage="select",
            hypothesis_status="done",
        )

        outcome = _run(state_path, assistant=True)

        assert outcome.stage_after == "stop"
        backlog = yaml.safe_load((repo / ".autolab" / "backlog.yaml").read_text())
        experiment = backlog["experiments"][0]
        assert experiment["status"] in {"done", "completed"}

    def test_assistant_no_tasks_message_mentions_completion(
        self, tmp_path: Path
    ) -> None:
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="human_review",
            assistant_mode="on",
            task_cycle_stage="select",
            hypothesis_status="done",
        )

        outcome = _run(state_path, assistant=True)

        assert "no actionable tasks" in outcome.message


# ---------------------------------------------------------------------------
# Phase 6: Assistant Mode
# ---------------------------------------------------------------------------


class TestAssistantMode:
    def test_assistant_with_task_selects_and_transitions(self, tmp_path: Path) -> None:
        """Assistant with an open manual task should select it and transition.

        The task is written to docs/todo.md as a bullet so the pre-sync picks it
        up and creates a proper entry in todo_state.  We also close the backlog
        hypothesis to avoid auto-generated candidates from interfering.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="human_review",
            assistant_mode="on",
            task_cycle_stage="select",
            hypothesis_status="done",
        )
        # Write a manual todo bullet that the pre-sync will ingest as a task.
        _write_todo_md(repo, "# Tasks\n- [ ] [implementation] Implement the feature\n")

        outcome = _run(state_path, assistant=True)

        assert outcome.transitioned
        persisted = _read_state(repo)
        assert persisted["current_task_id"]  # some task was selected
        assert persisted["stage"] == "implementation"
        assert persisted["task_cycle_stage"] == "implement"

    def test_assistant_human_review_forces_task_selection(self, tmp_path: Path) -> None:
        """From human_review, assistant should force task selection."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="human_review",
            assistant_mode="on",
            task_cycle_stage="done",
            current_task_id="old_task",
            hypothesis_status="done",
        )
        _write_todo_md(repo, "# Tasks\n- [ ] [design] Re-design experiment\n")

        outcome = _run(state_path, assistant=True)

        persisted = _read_state(repo)
        assert persisted["current_task_id"]
        assert persisted["stage"] != "human_review"

    def test_assistant_no_tasks_stops(self, tmp_path: Path) -> None:
        """Assistant mode with empty todo and no generated candidates stops.

        Uses human_review as start stage (terminal) so that the fallback
        task injection in todo_sync is suppressed.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="human_review",
            assistant_mode="on",
            task_cycle_stage="select",
            hypothesis_status="done",
        )

        outcome = _run(state_path, assistant=True)

        assert outcome.stage_after == "stop"
        persisted = _read_state(repo)
        assert persisted["stage"] == "stop"
        assert persisted["task_cycle_stage"] == "done"
        assert persisted["current_task_id"] == ""

    def test_assistant_select_writes_task_ledger(self, tmp_path: Path) -> None:
        repo, state_path, _it_dir = _setup_repo(
            tmp_path,
            stage="human_review",
            assistant_mode="on",
            task_cycle_stage="select",
            hypothesis_status="done",
        )
        _write_todo_md(
            repo, "# Tasks\n- [ ] [stage:implementation] Implement scoped fix\n"
        )

        outcome = _run(state_path, assistant=True)

        assert outcome.transitioned
        ledger_path = repo / ".autolab" / "task_history.jsonl"
        assert ledger_path.exists()
        entries = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert entries
        latest = entries[-1]
        assert latest["event"] == "select"
        assert latest["stage_after"] == "implementation"
        assert latest["task_id"]

    def test_assistant_verify_writes_verification_ledger(self, tmp_path: Path) -> None:
        repo, state_path, _it_dir = _setup_repo(
            tmp_path,
            stage="implementation",
            assistant_mode="on",
            task_cycle_stage="verify",
            current_task_id="task_abc",
            hypothesis_status="done",
        )
        with mock.patch(
            "autolab.run_assistant._run_verification_step",
            return_value=(False, "verification failed"),
        ):
            outcome = _run(state_path, assistant=True)

        assert outcome.exit_code == 0
        ledger_path = repo / ".autolab" / "task_history.jsonl"
        assert ledger_path.exists()
        entries = [
            json.loads(line)
            for line in ledger_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        latest = entries[-1]
        assert latest["event"] == "verify"
        assert latest["task_id"] == "task_abc"
        assert latest["verification"]["passed"] is False


# ---------------------------------------------------------------------------
# Full cycle integration: hypothesis → stop via decide_repeat
# ---------------------------------------------------------------------------


class TestFullCycle:
    def test_full_happy_path_cycle(self, tmp_path: Path) -> None:
        """Walk through the entire happy path from hypothesis to stop."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")
        iteration_id = "iter_test_001"

        # hypothesis → design
        _seed_hypothesis(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "design"

        # design → implementation
        _seed_design(it_dir, iteration_id)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"

        # implementation → implementation_review
        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"

        # implementation_review → launch
        _seed_review_pass(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "launch"

        # launch → slurm_monitor
        _seed_launch(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "slurm_monitor"

        # slurm_monitor → extract_results (auto-skip for local runs)
        outcome = _run(state_path)
        assert outcome.stage_after == "extract_results"

        # extract_results → update_docs
        _seed_extract(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "update_docs"

        # update_docs → decide_repeat
        _seed_update_docs(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "decide_repeat"

        # decide_repeat → stop
        outcome = _run(state_path, decision="stop")
        assert outcome.stage_after == "stop"

        # Verify final state
        persisted = _read_state(repo)
        assert persisted["stage"] == "stop"
        assert persisted["stage_attempt"] == 0

    def test_retry_then_pass_cycle(self, tmp_path: Path) -> None:
        """Review retry → re-implementation → review pass → launch."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="implementation_review")

        # First review: needs_retry
        _seed_review_retry(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"
        assert _read_state(repo)["stage_attempt"] == 1

        # Re-implementation
        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"
        assert _read_state(repo)["stage_attempt"] == 1  # carried

        # Second review: pass
        _seed_review_pass(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "launch"


# ---------------------------------------------------------------------------
# SLURM Cluster & Long-Running Launched Task Scenarios
# ---------------------------------------------------------------------------


def _seed_slurm_launch(
    iteration_dir: Path,
    run_id: str = "run_001",
    *,
    iteration_id: str = "iter_test_001",
    job_id: str = "12345",
    sync_status: str = "completed",
) -> None:
    """Seed a SLURM launch with run_manifest, sbatch script, and sync status."""
    if not (iteration_dir / "design.yaml").exists():
        _seed_design(iteration_dir, iteration_id=iteration_dir.name)
    design_path = iteration_dir / "design.yaml"
    design_payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    if isinstance(design_payload, dict):
        compute = design_payload.get("compute")
        if not isinstance(compute, dict):
            compute = {}
        compute["location"] = "slurm"
        design_payload["compute"] = compute
        design_path.write_text(
            yaml.safe_dump(design_payload, sort_keys=False), encoding="utf-8"
        )
    if not (iteration_dir / "review_result.json").exists():
        _seed_review_pass(iteration_dir)
    launch_dir = iteration_dir / "launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    (launch_dir / "run_slurm.sbatch").write_text(
        "#!/bin/bash\n#SBATCH --job-name=test\necho run",
        encoding="utf-8",
    )
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    normalized_sync = str(sync_status).strip().lower()
    if normalized_sync in {"ok", "completed", "success", "passed"}:
        manifest_status = "synced"
        manifest_sync = "ok"
    elif normalized_sync in {"failed", "error"}:
        manifest_status = "failed"
        manifest_sync = "failed"
    else:
        manifest_status = "submitted"
        manifest_sync = normalized_sync or "pending"

    manifest: dict[str, Any] = {
        "run_id": run_id,
        "iteration_id": iteration_id,
        "launch_mode": "slurm",
        "host_mode": "slurm",
        "command": "sbatch launch/run_slurm.sbatch",
        "resource_request": {"partition": "debug"},
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "status": manifest_status,
        "slurm": {"job_id": job_id},
        "artifact_sync_to_local": {"status": manifest_sync},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )


def _seed_slurm_extract(
    iteration_dir: Path,
    run_id: str = "run_001",
    *,
    iteration_id: str = "iter_test_001",
    job_id: str = "12345",
) -> None:
    """Seed SLURM extract_results artifacts (manifest + metrics)."""
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "iteration_id": iteration_id,
        "launch_mode": "slurm",
        "host_mode": "slurm",
        "command": "sbatch launch/run_slurm.sbatch",
        "resource_request": {"partition": "debug"},
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "status": "synced",
        "slurm": {"job_id": job_id},
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    metrics = {
        "iteration_id": iteration_id,
        "run_id": run_id,
        "status": "completed",
        "primary_metric": {
            "name": "loss",
            "value": 0.42,
            "delta_vs_baseline": 0.0,
        },
        "baseline_results": [],
        "variant_results": [],
    }
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )


def _write_slurm_ledger(
    repo: Path,
    run_id: str,
    *,
    job_id: str = "12345",
    iteration_id: str = "iter_test_001",
) -> None:
    """Write a SLURM job ledger entry to docs/slurm_job_list.md."""
    path = repo / "docs" / "slurm_job_list.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = f"- 2026-01-01 | job_id={job_id} | iteration_id={iteration_id} | run_id={run_id} | status=completed"
    path.write_text(f"# SLURM Job Ledger\n\n{entry}\n", encoding="utf-8")


class TestSlurmLaunchHappyPath:
    """SLURM launch → slurm_monitor when sync is complete and ledger exists."""

    def test_slurm_launch_to_slurm_monitor_with_completed_sync(
        self, tmp_path: Path
    ) -> None:
        """SLURM run with completed artifact sync transitions to slurm_monitor."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_slurm_launch(it_dir, sync_status="completed")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        assert persisted["last_run_id"] == "run_001"
        assert persisted["sync_status"] == "completed"

    def test_slurm_launch_sets_sync_status_in_state(self, tmp_path: Path) -> None:
        """Verify sync_status from manifest is persisted in state.json."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_slurm_launch(it_dir, sync_status="completed")
        _write_slurm_ledger(repo, "run_001")

        _run(state_path)

        persisted = _read_state(repo)
        assert persisted["sync_status"] == "completed"


class TestSlurmIncompleteSync:
    """SLURM jobs with incomplete artifact sync should transition to monitor."""

    def test_slurm_launch_pending_sync_blocks_transition(self, tmp_path: Path) -> None:
        """When artifact_sync_to_local is 'pending', launch should hand off to monitor."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_slurm_launch(it_dir, sync_status="pending")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.exit_code == 0
        assert outcome.transitioned
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        assert persisted["sync_status"] == "pending"

    def test_slurm_launch_failed_sync_blocks_transition(self, tmp_path: Path) -> None:
        """When launch manifest is failed, launch should fail for retry."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_slurm_launch(it_dir, sync_status="failed")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] in {"launch", "human_review"}

    def test_slurm_incomplete_sync_increments_stage_attempt(
        self, tmp_path: Path
    ) -> None:
        """Incomplete sync should hand off to slurm_monitor without launch retry."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="launch", stage_attempt=0
        )
        _seed_slurm_launch(it_dir, sync_status="pending")
        _write_slurm_ledger(repo, "run_001")

        _run(state_path)
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        assert persisted["stage_attempt"] == 0

    def test_slurm_sync_exhaustion_escalates_to_human_review(
        self, tmp_path: Path
    ) -> None:
        """Repeated launch failures (manifest status failed) exhaust retry budget."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="launch",
            stage_attempt=2,
            max_stage_attempts=3,
        )
        _seed_slurm_launch(it_dir, sync_status="failed")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"
        assert "budget exhausted" in outcome.message or "escalating" in outcome.message


class TestSlurmLedgerValidation:
    """SLURM ledger (docs/slurm_job_list.md) must contain the run entry."""

    def test_slurm_launch_missing_ledger_is_backfilled(self, tmp_path: Path) -> None:
        """SLURM launch should backfill missing ledger and continue."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_slurm_launch(it_dir, sync_status="completed")
        # Deliberately do NOT write the ledger

        outcome = _run(state_path)

        assert outcome.exit_code == 0
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        ledger = repo / "docs" / "slurm_job_list.md"
        assert ledger.exists()
        assert "run_id=run_001" in ledger.read_text(encoding="utf-8")

    def test_slurm_launch_ledger_missing_run_id_is_backfilled(
        self, tmp_path: Path
    ) -> None:
        """SLURM launch should append missing run_id entry and continue."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_slurm_launch(it_dir, sync_status="completed")
        # Write ledger with different run_id
        _write_slurm_ledger(repo, "run_OTHER", job_id="99999")

        outcome = _run(state_path)

        assert outcome.exit_code == 0
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        ledger = repo / "docs" / "slurm_job_list.md"
        assert "run_id=run_001" in ledger.read_text(encoding="utf-8")

    def test_slurm_update_docs_missing_ledger_fails(self, tmp_path: Path) -> None:
        """At update_docs stage, SLURM manifest without ledger entry blocks transition."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="update_docs")
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)
        _seed_update_docs(it_dir)
        # Create SLURM manifest for the run so update_docs sees a SLURM run
        _seed_slurm_extract(it_dir, "run_001")
        # Do NOT write slurm ledger

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] in {"update_docs", "human_review"}

    def test_slurm_update_docs_with_ledger_passes(self, tmp_path: Path) -> None:
        """At update_docs stage, SLURM manifest WITH ledger entry allows transition."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="update_docs")
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)
        _seed_update_docs(it_dir)
        _seed_slurm_extract(it_dir, "run_001")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "decide_repeat"


class TestSlurmManifestVariants:
    """SLURM detection works across different manifest field layouts."""

    def test_slurm_detected_via_host_mode_field(self, tmp_path: Path) -> None:
        """SLURM detected through host_mode field in manifest."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        launch_dir = it_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_slurm.sbatch").write_text(
            "#!/bin/bash\necho run", encoding="utf-8"
        )
        run_dir = it_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": "run_001",
            "iteration_id": "iter_test_001",
            "host_mode": "slurm",
            "started_at": "2026-01-01T00:00:00Z",
            "status": "completed",
            "slurm": {"job_id": "67890"},
            "artifact_sync_to_local": {"status": "completed"},
        }
        (run_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        _write_slurm_ledger(repo, "run_001", job_id="67890")

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "slurm_monitor"

    def test_slurm_detected_via_nested_resource_request(self, tmp_path: Path) -> None:
        """SLURM detected through resource_request.mode nested field."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        launch_dir = it_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_slurm.sbatch").write_text(
            "#!/bin/bash\necho run", encoding="utf-8"
        )
        run_dir = it_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": "run_001",
            "iteration_id": "iter_test_001",
            "started_at": "2026-01-01T00:00:00Z",
            "status": "completed",
            "resource_request": {"mode": "slurm", "slurm": {"job_id": "11111"}},
            "artifact_sync_to_local": {"status": "completed"},
        }
        (run_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        _write_slurm_ledger(repo, "run_001", job_id="11111")

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "slurm_monitor"

    def test_slurm_manifest_missing_job_id_fails(self, tmp_path: Path) -> None:
        """SLURM manifest without job_id should fail ledger validation."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        launch_dir = it_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_slurm.sbatch").write_text(
            "#!/bin/bash\necho run", encoding="utf-8"
        )
        run_dir = it_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": "run_001",
            "iteration_id": "iter_test_001",
            "launch_mode": "slurm",
            "started_at": "2026-01-01T00:00:00Z",
            "status": "completed",
            # No slurm.job_id!
            "artifact_sync_to_local": {"status": "completed"},
        }
        (run_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] in {"launch", "human_review"}


class TestSlurmFullCycle:
    """End-to-end SLURM workflow from launch through decide_repeat."""

    def test_slurm_monitor_waits_when_sync_not_ready(self, tmp_path: Path) -> None:
        """slurm_monitor remains active while SLURM artifacts are still unsynced."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="slurm_monitor")
        run_dir = it_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": "run_001",
            "iteration_id": "iter_test_001",
            "host_mode": "slurm",
            "command": "sbatch launch/run_slurm.sbatch",
            "resource_request": {"partition": "debug"},
            "status": "running",
            "slurm": {"job_id": "12345"},
            "artifact_sync_to_local": {"status": "pending"},
            "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
        }
        (run_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)

        outcome = _run(state_path)
        assert outcome.exit_code == 0
        assert not outcome.transitioned
        assert outcome.stage_after == "slurm_monitor"
        assert "waiting" in outcome.message

    def test_slurm_monitor_strict_mode_rejects_sync_ok_without_synced_status(
        self, tmp_path: Path
    ) -> None:
        """Strict mode requires manifest status=synced once sync reports success."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="slurm_monitor")
        run_dir = it_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        manifest = {
            "run_id": "run_001",
            "iteration_id": "iter_test_001",
            "host_mode": "slurm",
            "command": "sbatch launch/run_slurm.sbatch",
            "resource_request": {"partition": "debug"},
            "status": "completed",
            "slurm": {"job_id": "12345"},
            "artifact_sync_to_local": {"status": "ok"},
            "timestamps": {
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": "2026-01-01T00:05:00Z",
            },
        }
        (run_dir / "run_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] in {"slurm_monitor", "human_review"}
        assert (
            "strict SLURM lifecycle requires run_manifest.status='synced'"
            in outcome.message
        )

    def test_extract_results_strict_mode_finalizes_manifest_to_completed(
        self, tmp_path: Path
    ) -> None:
        """Extract stage finalizes synced SLURM manifests to completed in strict mode."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="extract_results")
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)
        _seed_slurm_extract(it_dir, "run_001")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.stage_after == "update_docs"
        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"
        assert manifest.get("timestamps", {}).get("completed_at")

    def test_slurm_full_cycle_launch_to_stop(self, tmp_path: Path) -> None:
        """Walk through launch → slurm_monitor → extract → update_docs → decide_repeat → stop for SLURM."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")

        # launch → slurm_monitor (SLURM with completed sync)
        _seed_slurm_launch(it_dir, sync_status="completed")
        _write_slurm_ledger(repo, "run_001")
        outcome = _run(state_path)
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["last_run_id"] == "run_001"
        assert persisted["sync_status"] == "completed"

        # slurm_monitor → extract_results (auto-skip for completed sync)
        outcome = _run(state_path)
        assert outcome.stage_after == "extract_results"

        # extract_results → update_docs
        _seed_slurm_extract(it_dir, "run_001")
        outcome = _run(state_path)
        assert outcome.stage_after == "update_docs"

        # update_docs → decide_repeat (needs ledger for SLURM)
        _seed_update_docs(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "decide_repeat"

        # decide_repeat → stop
        outcome = _run(state_path, decision="stop")
        assert outcome.stage_after == "stop"
        persisted = _read_state(repo)
        assert persisted["stage"] == "stop"

    def test_slurm_sync_retry_then_success(self, tmp_path: Path) -> None:
        """Pending SLURM launch hands off to monitor, then completes when synced."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="launch", stage_attempt=0
        )

        # Launch: pending sync -> handoff to monitor
        _seed_slurm_launch(it_dir, sync_status="pending")
        _write_slurm_ledger(repo, "run_001")
        outcome = _run(state_path)
        assert outcome.exit_code == 0
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        assert persisted["stage_attempt"] == 0

        # Monitor while pending: stay in monitor
        outcome = _run(state_path)
        assert outcome.exit_code == 0
        assert outcome.stage_after == "slurm_monitor"

        # Sync completes -> monitor advances
        _seed_slurm_launch(it_dir, sync_status="completed")
        outcome = _run(state_path)
        assert outcome.transitioned
        assert outcome.stage_after == "extract_results"
        persisted = _read_state(repo)
        assert persisted["sync_status"] == "completed"
        assert persisted["stage_attempt"] == 0


class TestSlurmSyncStatusValues:
    """Various sync_status values and their handling."""

    def test_sync_status_ok_is_accepted(self, tmp_path: Path) -> None:
        """'ok' is treated as completed sync."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_slurm_launch(it_dir, sync_status="ok")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "slurm_monitor"

    def test_sync_status_success_is_accepted(self, tmp_path: Path) -> None:
        """'success' is treated as completed sync."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_slurm_launch(it_dir, sync_status="success")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "slurm_monitor"

    def test_sync_status_running_blocks(self, tmp_path: Path) -> None:
        """'running' keeps workflow in monitor instead of failing launch."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_slurm_launch(it_dir, sync_status="running")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.exit_code == 0
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"


class TestLongRunningRetryBudget:
    """Long-running SLURM jobs remain in monitor without launch retry exhaustion."""

    def test_repeated_sync_failures_exhaust_budget(self, tmp_path: Path) -> None:
        """Repeated pending sync should keep stage at slurm_monitor without retries."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="launch",
            stage_attempt=0,
            max_stage_attempts=3,
        )
        _seed_slurm_launch(it_dir, sync_status="pending")
        _write_slurm_ledger(repo, "run_001")

        # Attempt 1: launch -> monitor
        outcome = _run(state_path)
        assert outcome.exit_code == 0
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        assert persisted["stage_attempt"] == 0

        # Attempt 2: still waiting
        outcome = _run(state_path)
        assert outcome.exit_code == 0
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        assert persisted["stage_attempt"] == 0

        # Attempt 3: still waiting, no exhaustion
        outcome = _run(state_path)
        assert outcome.exit_code == 0
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"

    def test_update_docs_cycle_limit_with_slurm(self, tmp_path: Path) -> None:
        """extract → update_docs cycle count guardrail works with SLURM runs."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="extract_results",
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 3,  # at max
                "last_verification_passed": False,
            },
        )
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)
        _seed_slurm_extract(it_dir, "run_001")
        _write_slurm_ledger(repo, "run_001")

        outcome = _run(state_path)

        assert outcome.stage_after == "human_review"
        assert "update_docs cycle limit" in outcome.message


class TestSlurmNoProgressGuardrails:
    """Guardrails at decide_repeat work correctly for SLURM scenarios."""

    def test_slurm_no_progress_breach_escalates(self, tmp_path: Path) -> None:
        """no_progress_decisions breach at decide_repeat escalates with SLURM context."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "hypothesis",
                "same_decision_streak": 0,
                "last_open_task_count": 3,
                "no_progress_decisions": 1,  # one more with same count → 2 >= max(2)
                "update_docs_cycle_count": 0,
                "last_verification_passed": False,
            },
        )
        # Write 3 open tasks in todo to match last_open_task_count
        _write_todo_md(
            repo,
            "# Tasks\n- [ ] [hypothesis] task1\n- [ ] [design] task2\n- [ ] [implementation] task3\n",
        )
        _write_todo_state(repo, {})  # fresh state, pre-sync will pick up bullets

        outcome = _run(
            state_path,
            decision="design",
            auto_mode=True,
            auto_decision=True,
        )

        assert outcome.stage_after == "human_review"
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"

    def test_slurm_progress_resets_no_progress_counter(self, tmp_path: Path) -> None:
        """When open task count decreases, no_progress_decisions resets to 0.

        At decide_repeat the pre-sync generates additional tasks (decide_repeat
        task + backlog hypothesis + backlog experiment), so last_open_task_count
        must be set high enough that the total open count (manual + generated)
        is strictly less than it.
        """
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "hypothesis",
                "same_decision_streak": 0,
                "last_open_task_count": 20,
                "no_progress_decisions": 1,
                "update_docs_cycle_count": 0,
                "last_verification_passed": False,
            },
        )
        # 2 manual tasks + ~3 generated = ~5 total, well below last_open_task_count=20
        _write_todo_md(
            repo, "# Tasks\n- [ ] [hypothesis] task1\n- [ ] [design] task2\n"
        )
        _write_todo_state(repo, {})

        outcome = _run(
            state_path,
            decision="design",
            auto_mode=True,
            auto_decision=True,
        )

        assert outcome.stage_after == "design"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["no_progress_decisions"] == 0


class TestLocalLaunchVsSlurm:
    """Local launches (no SLURM) should not require ledger or sync validation."""

    def test_local_launch_no_ledger_required(self, tmp_path: Path) -> None:
        """Local run_manifest (no SLURM) transitions without slurm_job_list.md."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="launch")
        _seed_launch(it_dir)  # local launch (no SLURM fields)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "slurm_monitor"

    def test_local_update_docs_no_ledger_required(self, tmp_path: Path) -> None:
        """Local run at update_docs does not require slurm_job_list.md."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="update_docs")
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)
        _seed_extract(it_dir, "run_001")  # local manifest
        _seed_update_docs(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "decide_repeat"


# ---------------------------------------------------------------------------
# Multi-iteration end-to-end test (Gap 1)
# ---------------------------------------------------------------------------


class TestMultiIterationEndToEnd:
    """Two full iterations: local run with review retry, then SLURM with sync retry."""

    def test_full_two_iteration_journey(self, tmp_path: Path) -> None:
        """Walk through 21 transitions across 2 iterations.

        Iteration 1 (local, review retry cycle):
          1. hypothesis → design
          2. design → implementation
          3. implementation → implementation_review
          4. implementation_review (needs_retry) → implementation  (retry)
          5. implementation → implementation_review
          6. implementation_review (pass) → launch
          7. launch → slurm_monitor
          7b. slurm_monitor → extract_results (auto-skip for local)
          8. extract_results → update_docs
          9. update_docs → decide_repeat
         10. decide_repeat → hypothesis  (loop-back)

        Iteration 2 (SLURM with sync retry):
         11. hypothesis → design
         12. design → implementation
         13. implementation → implementation_review
         14. implementation_review (pass) → launch
         15. launch (SLURM sync pending) → launch  (retry / stage_attempt++)
         16. launch (SLURM sync completed) → slurm_monitor
         16b. slurm_monitor → extract_results (auto-skip for completed sync)
         17. extract_results → update_docs
         18. update_docs → decide_repeat
         19. decide_repeat → stop
        """
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")
        iteration_id = "iter_test_001"

        # === ITERATION 1 (local) ===

        # 1. hypothesis → design
        _seed_hypothesis(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "design"

        # 2. design → implementation
        _seed_design(it_dir, iteration_id)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"

        # 3. implementation → implementation_review
        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"

        # 4. implementation_review (needs_retry) → implementation
        _seed_review_retry(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 1

        # 5. implementation → implementation_review (carries attempt)
        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 1

        # 6. implementation_review (pass) → launch
        _seed_review_pass(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "launch"

        # 7. launch → slurm_monitor
        _seed_launch(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["last_run_id"] == "run_001"

        # 7b. slurm_monitor → extract_results (auto-skip for local)
        outcome = _run(state_path)
        assert outcome.stage_after == "extract_results"

        # 8. extract_results → update_docs
        _seed_extract(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "update_docs"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 1

        # 9. update_docs → decide_repeat
        _seed_update_docs(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "decide_repeat"

        # 10. decide_repeat → hypothesis (loop-back)
        outcome = _run(state_path, decision="hypothesis")
        assert outcome.stage_after == "hypothesis"
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 0
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 0

        # === ITERATION 2 (SLURM with sync retry) ===

        # 11. hypothesis → design
        _seed_hypothesis(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "design"

        # 12. design → implementation
        _seed_design(it_dir, iteration_id)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"

        # 13. implementation → implementation_review
        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"

        # 14. implementation_review (pass) → launch
        _seed_review_pass(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "launch"

        # 15. launch (SLURM sync pending) → slurm_monitor (handoff)
        _seed_slurm_launch(it_dir, sync_status="pending")
        _write_slurm_ledger(repo, "run_001")
        outcome = _run(state_path)
        assert outcome.exit_code == 0
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"
        assert persisted["stage_attempt"] == 0

        # 16. slurm_monitor with pending sync stays waiting
        outcome = _run(state_path)
        assert outcome.stage_after == "slurm_monitor"
        persisted = _read_state(repo)
        assert persisted["stage"] == "slurm_monitor"

        # 16b. sync completes -> slurm_monitor advances to extract_results
        _seed_slurm_launch(it_dir, sync_status="completed")
        outcome = _run(state_path)
        assert outcome.stage_after == "extract_results"
        persisted = _read_state(repo)
        assert persisted["sync_status"] == "completed"
        assert persisted["stage_attempt"] == 0  # reset on forward transition

        # 17. extract_results → update_docs
        _seed_slurm_extract(it_dir, "run_001")
        outcome = _run(state_path)
        assert outcome.stage_after == "update_docs"
        persisted = _read_state(repo)
        # Cycle count was reset at step 10; this is the first extract→update_docs
        # in iteration 2, so it should be 1 (not 2).
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 1

        # 18. update_docs → decide_repeat
        _seed_update_docs(it_dir)
        _write_slurm_ledger(repo, "run_001")  # refresh ledger for SLURM validation
        outcome = _run(state_path)
        assert outcome.stage_after == "decide_repeat"

        # 19. decide_repeat → stop
        outcome = _run(state_path, decision="stop")
        assert outcome.stage_after == "stop"

        # Final assertions
        persisted = _read_state(repo)
        assert persisted["stage"] == "stop"
        backlog = yaml.safe_load((repo / ".autolab" / "backlog.yaml").read_text())
        assert backlog["experiments"][0]["status"] in {"done", "completed"}


# ---------------------------------------------------------------------------
# Gap 2: review_result.json status "failed" → human_review
# ---------------------------------------------------------------------------


class TestGapReviewFailed:
    """Seed review_result.json with status: 'failed' and verify escalation."""

    def _seed_review_failed(self, iteration_dir: Path) -> None:
        (iteration_dir / "implementation_review.md").write_text(
            "# Review\nFailed.",
            encoding="utf-8",
        )
        review = {
            "status": "failed",
            "blocking_findings": ["critical_issue"],
            "required_checks": {
                "tests": "fail",
                "dry_run": "skip",
                "schema": "pass",
                "env_smoke": "skip",
                "docs_target_update": "skip",
            },
            "reviewed_at": "2026-01-01T00:00:00Z",
        }
        (iteration_dir / "review_result.json").write_text(
            json.dumps(review, indent=2),
            encoding="utf-8",
        )

    def test_failed_review_escalates_to_human_review(self, tmp_path: Path) -> None:
        """status: 'failed' should go straight to human_review (not retry)."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="implementation_review",
            stage_attempt=0,
        )
        self._seed_review_failed(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "human_review"
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"

    def test_failed_review_does_not_consume_retry_budget(self, tmp_path: Path) -> None:
        """status: 'failed' escalation should not increment stage_attempt."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="implementation_review",
            stage_attempt=0,
        )
        self._seed_review_failed(it_dir)

        outcome = _run(state_path)

        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"
        # stage_attempt should be 0 (reset on transition), not incremented
        assert persisted["stage_attempt"] == 0


# ---------------------------------------------------------------------------
# Gap 3: Auto-decision from TODO tasks
# ---------------------------------------------------------------------------


class TestGapAutoDecisionFromTodo:
    """Test auto_decision=True integration with real TODO state."""

    def _write_todo_with_stage_tasks(
        self,
        repo: Path,
        *,
        stages: list[str],
    ) -> None:
        """Write TODO bullets with [stage:X] tags for auto-decision selection."""
        bullets = "\n".join(f"- [ ] [{stage}] Task for {stage}" for stage in stages)
        _write_todo_md(repo, f"# Tasks\n{bullets}\n")
        _write_todo_state(repo, {})  # fresh state; pre-sync will ingest bullets

    def test_auto_decision_selects_design_from_todo(self, tmp_path: Path) -> None:
        """Auto-decision with a [design] task in TODO selects 'design'."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
        )
        self._write_todo_with_stage_tasks(repo, stages=["design"])

        outcome = _run(
            state_path,
            auto_mode=True,
            auto_decision=True,
        )

        assert outcome.transitioned
        assert outcome.stage_after == "design"
        persisted = _read_state(repo)
        assert persisted["stage"] == "design"

    def test_auto_decision_selects_hypothesis_from_todo(self, tmp_path: Path) -> None:
        """Auto-decision with a [hypothesis] task in TODO selects 'hypothesis'."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
        )
        self._write_todo_with_stage_tasks(repo, stages=["hypothesis"])

        outcome = _run(
            state_path,
            auto_mode=True,
            auto_decision=True,
        )

        assert outcome.transitioned
        assert outcome.stage_after == "hypothesis"
        persisted = _read_state(repo)
        assert persisted["stage"] == "hypothesis"


# ---------------------------------------------------------------------------
# Gap 4: Completed experiment protection (backlog done → force stop)
# ---------------------------------------------------------------------------


class TestGapCompletedExperimentProtection:
    """When backlog experiment is already 'done'/'completed', force-stop from active stages."""

    def test_force_stop_from_active_stage(self, tmp_path: Path) -> None:
        """An active stage with completed experiment should be forced to stop."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="implementation",
            backlog_experiment_status="completed",
        )
        _seed_implementation(it_dir)

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "stop"
        assert "blocked completed experiment" in outcome.message
        persisted = _read_state(repo)
        assert persisted["stage"] == "stop"

    def test_noop_from_terminal_stage(self, tmp_path: Path) -> None:
        """A terminal stage with completed experiment should NOT be force-stopped (already terminal)."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="stop",
            backlog_experiment_status="completed",
        )

        outcome = _run(state_path)

        # Terminal stage → standard noop behavior
        assert not outcome.transitioned
        assert outcome.stage_after == "stop"

    def test_force_stop_from_decide_repeat(self, tmp_path: Path) -> None:
        """decide_repeat with completed experiment should force-stop."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            backlog_experiment_status="completed",
        )

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "stop"
        persisted = _read_state(repo)
        assert persisted["stage"] == "stop"


# ---------------------------------------------------------------------------
# Gap 5: update_docs_cycle_count reset durability across iterations
# ---------------------------------------------------------------------------


class TestGapUpdateDocsCycleCountResetAcrossIterations:
    """Verify update_docs_cycle_count resets on loop-back and doesn't accumulate."""

    def test_cycle_count_does_not_accumulate_across_iterations(
        self, tmp_path: Path
    ) -> None:
        """Full sequence: extract→update_docs (count=1), decide_repeat→hypothesis
        (count=0), full cycle back to extract→update_docs (count=1, not 2)."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="extract_results")
        iteration_id = "iter_test_001"
        _seed_extract(it_dir)
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)

        # extract_results → update_docs (cycle count becomes 1)
        outcome = _run(state_path)
        assert outcome.stage_after == "update_docs"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 1

        # update_docs → decide_repeat
        _seed_update_docs(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "decide_repeat"

        # decide_repeat → hypothesis (resets cycle count to 0)
        outcome = _run(state_path, decision="hypothesis")
        assert outcome.stage_after == "hypothesis"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 0

        # Walk back to extract_results
        _seed_hypothesis(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "design"

        _seed_design(it_dir, iteration_id)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"

        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"

        _seed_review_pass(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "launch"

        _seed_launch(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "slurm_monitor"

        # slurm_monitor → extract_results (auto-skip for local runs)
        outcome = _run(state_path)
        assert outcome.stage_after == "extract_results"

        _seed_extract(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "update_docs"

        # Critical assertion: count should be 1, not 2
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["update_docs_cycle_count"] == 1


# ---------------------------------------------------------------------------
# Gap 6: Multiple experiments in backlog (only matching one marked done)
# ---------------------------------------------------------------------------


class TestGapMultipleExperimentsBacklog:
    """Two experiments in backlog; stopping with experiment_id='e2' marks only e2 done."""

    def _write_multi_experiment_backlog(self, repo: Path) -> None:
        backlog = {
            "hypotheses": [
                {
                    "id": "h1",
                    "status": "open",
                    "title": "Hyp 1",
                    "success_metric": "m",
                    "target_delta": 0.0,
                },
            ],
            "experiments": [
                {
                    "id": "e1",
                    "hypothesis_id": "h1",
                    "status": "open",
                    "iteration_id": "iter_test_001",
                },
                {
                    "id": "e2",
                    "hypothesis_id": "h1",
                    "status": "open",
                    "iteration_id": "iter_test_002",
                },
            ],
        }
        path = repo / ".autolab" / "backlog.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(backlog, sort_keys=False), encoding="utf-8")

    def test_only_matching_experiment_marked_completed(self, tmp_path: Path) -> None:
        """Stopping experiment e2 marks only e2 completed; e1 stays open."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            experiment_id="e2",
            iteration_id="iter_test_002",
        )
        self._write_multi_experiment_backlog(repo)

        outcome = _run(state_path, decision="stop")

        assert outcome.stage_after == "stop"
        backlog = yaml.safe_load((repo / ".autolab" / "backlog.yaml").read_text())
        experiments = {e["id"]: e["status"] for e in backlog["experiments"]}
        assert experiments["e1"] == "open"
        assert experiments["e2"] == "completed"


# ---------------------------------------------------------------------------
# Gap 7: Combined no_progress + same_decision_streak guardrails
# ---------------------------------------------------------------------------


class TestGapDecideRepeatStreakAndNoProgressCombined:
    """Verify interaction between no_progress and same_decision_streak guardrails."""

    def test_no_progress_fires_before_streak_limit(self, tmp_path: Path) -> None:
        """no_progress_decisions hits max before same_decision_streak does."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "hypothesis",
                "same_decision_streak": 1,  # below max (3)
                "last_open_task_count": 5,
                "no_progress_decisions": 1,  # one more with same/higher count → 2 >= max(2)
                "update_docs_cycle_count": 0,
                "last_verification_passed": False,
            },
        )
        # Ensure open task count stays >= last_open_task_count (5)
        _write_todo_md(
            repo,
            "# Tasks\n" + "".join(f"- [ ] [hypothesis] task{i}\n" for i in range(6)),
        )
        _write_todo_state(repo, {})

        outcome = _run(
            state_path,
            decision="design",  # different decision, so streak resets to 1
            auto_mode=True,
            auto_decision=True,
        )

        assert outcome.stage_after == "human_review"
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"

    def test_streak_fires_before_no_progress(self, tmp_path: Path) -> None:
        """same_decision_streak hits max before no_progress_decisions does."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "design",
                "same_decision_streak": 3,  # one more same → 4 > max(3)
                "last_open_task_count": 20,
                "no_progress_decisions": 0,  # well below max
                "update_docs_cycle_count": 0,
                "last_verification_passed": False,
            },
        )
        # Ensure open count < last_open_task_count (20) so no_progress resets
        _write_todo_md(repo, "# Tasks\n- [ ] [design] task1\n")
        _write_todo_state(repo, {})

        outcome = _run(
            state_path,
            decision="design",  # same decision → streak = 4 > 3
            auto_mode=True,
            auto_decision=True,
        )

        assert outcome.stage_after == "human_review"
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"


# ---------------------------------------------------------------------------
# Gap 8: Assistant mode verify → fail → implement → verify → pass → review
# ---------------------------------------------------------------------------


def _write_policy_with_test_command(
    repo: Path,
    *,
    test_command: str = "true",
    require_tests: bool = True,
    require_verification: bool = False,
) -> None:
    """Write a verifier_policy.yaml with a specific test_command."""
    policy = {
        "test_command": test_command,
        "dry_run_command": "true",
        "require_tests": require_tests,
        "require_dry_run": False,
        "require_env_smoke": False,
        "require_docs_target_update": False,
        "template_fill": {"enabled": False},
        "agent_runner": {"enabled": False, "stages": []},
        "autorun": {
            "guardrails": {
                "max_same_decision_streak": 3,
                "max_no_progress_decisions": 2,
                "max_update_docs_cycles": 3,
                "on_breach": "human_review",
            },
            "auto_commit": {"mode": "off"},
            "meaningful_change": {
                "require_implementation_progress": False,
                "require_git_for_progress": False,
                "on_non_git_behavior": "warn_and_continue",
                "require_verification": require_verification,
                "exclude_paths": [],
            },
        },
    }
    path = repo / ".autolab" / "verifier_policy.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")


class TestGapAssistantVerifyReviewCycle:
    """Test that assistant mode verify cycle routes correctly on pass/fail."""

    def test_failed_verification_returns_to_implement(self, tmp_path: Path) -> None:
        """When verification fails, task_cycle_stage goes back to 'implement'."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="implementation",
            assistant_mode="on",
            current_task_id="task_001",
            task_cycle_stage="verify",
            hypothesis_status="done",
        )
        # Use a test_command that will fail
        _write_policy_with_test_command(repo, test_command="false", require_tests=True)

        outcome = _run(state_path, assistant=True)

        persisted = _read_state(repo)
        assert persisted["task_cycle_stage"] == "implement"
        assert persisted["repeat_guard"]["last_verification_passed"] is False

    def test_passed_verification_advances_to_review(self, tmp_path: Path) -> None:
        """When verification passes, task_cycle_stage goes to 'review'."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="implementation",
            assistant_mode="on",
            current_task_id="task_001",
            task_cycle_stage="verify",
            hypothesis_status="done",
        )
        # Use a test_command that succeeds
        _write_policy_with_test_command(repo, test_command="true", require_tests=True)

        outcome = _run(state_path, assistant=True)

        persisted = _read_state(repo)
        assert persisted["task_cycle_stage"] == "review"
        assert persisted["repeat_guard"]["last_verification_passed"] is True


class TestStageGateContracts:
    def test_standard_run_records_state_history(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")
        _seed_hypothesis(it_dir)

        outcome = _run(state_path)

        assert outcome.exit_code == 0
        persisted = _read_state(repo)
        history = persisted.get("history", [])
        assert isinstance(history, list)
        assert history, "expected at least one history entry"
        latest = history[-1]
        assert latest.get("stage_before") == "hypothesis"
        assert latest.get("stage_after") == "design"
        assert latest.get("status") == "complete"

    def test_hypothesis_requires_metric_target_and_criteria(
        self, tmp_path: Path
    ) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="hypothesis")
        (it_dir / "hypothesis.md").write_text(
            "# Hypothesis\n\nnon-empty but incomplete", encoding="utf-8"
        )

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        assert "missing required hypothesis contract field" in outcome.message
        persisted = _read_state(repo)
        assert persisted["stage"] == "hypothesis"

    def test_implementation_requires_dry_run_heading_when_policy_demands_it(
        self, tmp_path: Path
    ) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="implementation")
        _seed_implementation(it_dir)

        policy_path = repo / ".autolab" / "verifier_policy.yaml"
        policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
        policy["requirements_by_stage"] = {"implementation": {"dry_run": True}}
        policy_path.write_text(
            yaml.safe_dump(policy, sort_keys=False), encoding="utf-8"
        )

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        assert "dry-run section" in outcome.message

    def test_update_docs_requires_run_artifact_references(self, tmp_path: Path) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="update_docs")
        _seed_update_docs(it_dir)
        _seed_launch(it_dir, run_id="run_001")
        state = _read_state(repo)
        state["last_run_id"] = "run_001"
        _write_state(repo, state)

        # Deliberately omit run_id + artifact references.
        (it_dir / "docs_update.md").write_text(
            "# Documentation Update\n\nNo references here.\n", encoding="utf-8"
        )

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        assert "must reference state.last_run_id" in outcome.message
