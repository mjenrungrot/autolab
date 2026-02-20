"""Comprehensive pipeline state-machine transition coverage tests.

Covers every (state, event) pair in the autolab 11-stage state machine:
forward transitions, retry cycles, budget exhaustion, guardrail breaches,
launch-mode matrix, SLURM sync status matrix, and decision artifact edge cases.

Organized into 9 test classes with ~72 parametrized cases total.
"""

from __future__ import annotations

import json
import subprocess
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
from autolab.constants import ACTIVE_STAGES, DECISION_STAGES
from autolab.launch_runtime import (
    _execute_launch_runtime,
    _execute_local_run,
    _execute_slurm_interactive_run,
    _execute_slurm_submit,
)
from autolab.models import StageCheckError
from autolab.state import _normalize_state


# ---------------------------------------------------------------------------
# Helpers (replicated from test_state_machine.py — module-private there)
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
    last_run_id: str = "",
) -> dict[str, Any]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "stage": stage,
        "stage_attempt": stage_attempt,
        "last_run_id": last_run_id,
        "pending_run_id": "",
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


def _write_policy(
    repo: Path,
    *,
    guardrails: dict[str, Any] | None = None,
    launch: dict[str, Any] | None = None,
) -> None:
    policy: dict[str, Any] = {
        "test_command": "true",
        "dry_run_command": "true",
        "require_tests": False,
        "require_dry_run": False,
        "require_env_smoke": False,
        "require_docs_target_update": False,
        "launch": launch
        or {
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


# --- Per-stage artifact seeders ---


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


def _seed_review_failed(iteration_dir: Path) -> None:
    (iteration_dir / "implementation_review.md").write_text(
        "# Review\nFailed.", encoding="utf-8"
    )
    review = {
        "status": "failed",
        "blocking_findings": ["critical issue"],
        "required_checks": {
            "tests": "fail",
            "dry_run": "fail",
            "schema": "fail",
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
    (launch_dir / "run_local.sh").write_text(
        '#!/bin/bash\nmkdir -p "runs/$AUTOLAB_RUN_ID"\necho \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\necho run',
        encoding="utf-8",
    )
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


def _seed_slurm_manifest(
    iteration_dir: Path,
    run_id: str = "run_001",
    *,
    host_mode: str = "slurm",
    status: str = "synced",
    sync_status: str = "ok",
) -> None:
    """Create a run_manifest.json with configurable host_mode, status, sync_status."""
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "run_id": run_id,
        "iteration_id": iteration_dir.name,
        "launch_mode": host_mode,
        "host_mode": host_mode,
        "command": "sbatch launch/run_slurm.sbatch"
        if host_mode == "slurm"
        else "bash launch/run_local.sh",
        "resource_request": {},
        "status": status,
        "artifact_sync_to_local": {"status": sync_status},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
        },
    }
    if status in {"synced", "completed", "failed", "partial"}:
        manifest["timestamps"]["completed_at"] = "2026-01-01T00:05:00Z"
        manifest["completed_at"] = "2026-01-01T00:05:00Z"
        manifest["started_at"] = "2026-01-01T00:00:00Z"
    if host_mode == "slurm":
        manifest["job_id"] = "12345"
        manifest["slurm"] = {"job_id": "12345"}
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


def _seed_decision_result(
    iteration_dir: Path,
    *,
    decision: str = "design",
    rationale: str = "More refinement needed.",
    evidence: list[dict[str, str]] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "decision": decision,
        "rationale": rationale,
        "evidence": evidence
        or [
            {
                "source": "metrics",
                "pointer": "runs/run_001/metrics.json",
                "summary": "Target not met",
            }
        ],
        "risks": [],
    }
    (iteration_dir / "decision_result.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8"
    )


# --- Repo scaffolding ---


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
# 1. TestEveryForwardTransition — parametrized happy-path edges
# ---------------------------------------------------------------------------


class TestEveryForwardTransition:
    """Cover every valid forward edge in the state graph."""

    @pytest.mark.parametrize(
        "from_stage, to_stage, seed_fn, extra_run_kwargs",
        [
            # 1. hypothesis → design
            ("hypothesis", "design", lambda it: _seed_hypothesis(it), {}),
            # 2. design → implementation
            ("design", "implementation", lambda it: _seed_design(it), {}),
            # 3. implementation → implementation_review
            (
                "implementation",
                "implementation_review",
                lambda it: _seed_implementation(it),
                {},
            ),
            # 4. implementation_review (pass) → launch
            ("implementation_review", "launch", lambda it: _seed_review_pass(it), {}),
            # 5. implementation_review (retry) → implementation (attempt increments)
            (
                "implementation_review",
                "implementation",
                lambda it: _seed_review_retry(it),
                {},
            ),
            # 6. implementation_review (failed) → human_review
            (
                "implementation_review",
                "human_review",
                lambda it: _seed_review_failed(it),
                {},
            ),
            # 7. launch → slurm_monitor (local run via bash subprocess)
            ("launch", "slurm_monitor", lambda it: _seed_launch(it), {}),
            # 10. extract_results → update_docs
            (
                "extract_results",
                "update_docs",
                lambda it: _seed_extract(it),
                {},
            ),
            # 11. update_docs → decide_repeat
            ("update_docs", "decide_repeat", lambda it: _seed_update_docs(it), {}),
            # 12. decide_repeat → hypothesis
            (
                "decide_repeat",
                "hypothesis",
                lambda it: None,
                {"decision": "hypothesis"},
            ),
            # 13. decide_repeat → design
            ("decide_repeat", "design", lambda it: None, {"decision": "design"}),
            # 14. decide_repeat → stop
            ("decide_repeat", "stop", lambda it: None, {"decision": "stop"}),
            # 15. decide_repeat → human_review
            (
                "decide_repeat",
                "human_review",
                lambda it: None,
                {"decision": "human_review"},
            ),
        ],
        ids=[
            "hypothesis_to_design",
            "design_to_implementation",
            "implementation_to_review",
            "review_pass_to_launch",
            "review_retry_to_implementation",
            "review_failed_to_human_review",
            "launch_to_slurm_monitor",
            "extract_to_update_docs",
            "update_docs_to_decide_repeat",
            "decide_hypothesis",
            "decide_design",
            "decide_stop",
            "decide_human_review",
        ],
    )
    def test_forward_transition(
        self,
        tmp_path: Path,
        from_stage: str,
        to_stage: str,
        seed_fn,
        extra_run_kwargs: dict,
    ) -> None:
        state_kwargs: dict[str, Any] = {"stage": from_stage}
        if from_stage == "extract_results":
            state_kwargs["last_run_id"] = "run_001"
        repo, state_path, it_dir = _setup_repo(tmp_path, **state_kwargs)
        seed_fn(it_dir)

        outcome = _run(state_path, **extra_run_kwargs)

        assert outcome.transitioned, (
            f"Expected transition from {from_stage} to {to_stage}"
        )
        assert outcome.stage_after == to_stage
        persisted = _read_state(repo)
        assert persisted["stage"] == to_stage

    def test_slurm_monitor_auto_skip_local(self, tmp_path: Path) -> None:
        """slurm_monitor auto-skips for local runs → extract_results (case 8)."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="slurm_monitor", last_run_id="run_001"
        )
        _seed_slurm_manifest(
            it_dir, "run_001", host_mode="local", status="completed", sync_status="ok"
        )

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "extract_results"

    def test_slurm_monitor_synced_to_extract(self, tmp_path: Path) -> None:
        """slurm_monitor with status=synced, sync=ok → extract_results (case 9)."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="slurm_monitor", last_run_id="run_001"
        )
        _seed_slurm_manifest(
            it_dir, "run_001", host_mode="slurm", status="synced", sync_status="ok"
        )

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "extract_results"

    def test_decide_repeat_no_decision_blocks(self, tmp_path: Path) -> None:
        """decide_repeat without a decision → stays, no transition (case 16)."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert outcome.stage_after == "decide_repeat"

    def test_decide_repeat_resets_stage_attempt(self, tmp_path: Path) -> None:
        """decide_repeat → hypothesis resets stage_attempt to 0."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="decide_repeat", stage_attempt=2
        )

        outcome = _run(state_path, decision="hypothesis")

        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 0

    def test_decide_stop_marks_backlog_done(self, tmp_path: Path) -> None:
        """decide_repeat → stop marks backlog experiment as done."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")

        outcome = _run(state_path, decision="stop")

        backlog = yaml.safe_load(
            (repo / ".autolab" / "backlog.yaml").read_text(encoding="utf-8")
        )
        experiment = backlog["experiments"][0]
        assert experiment["status"] in {"done", "completed"}

    def test_review_retry_increments_stage_attempt(self, tmp_path: Path) -> None:
        """implementation_review(retry) → implementation increments stage_attempt."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="implementation_review", stage_attempt=0
        )
        _seed_review_retry(it_dir)

        outcome = _run(state_path)

        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 1


# ---------------------------------------------------------------------------
# 2. TestStageCheckErrorRetryAtEveryStage
# ---------------------------------------------------------------------------


class TestStageCheckErrorRetryAtEveryStage:
    """For each active stage, trigger StageCheckError by omitting artifacts.

    Assert: stage_attempt increments, stage stays the same, exit_code == 1.
    """

    @pytest.mark.parametrize(
        "stage, setup_fn",
        [
            # hypothesis: omit hypothesis.md
            ("hypothesis", lambda _it, _repo: None),
            # design: omit design.yaml
            ("design", lambda _it, _repo: None),
            # implementation: omit implementation_plan.md
            ("implementation", lambda _it, _repo: None),
            # implementation_review: omit review_result.json
            ("implementation_review", lambda _it, _repo: None),
            # launch: need design.yaml + review pass but omit launch scripts
            (
                "launch",
                lambda it, _repo: (
                    _seed_design(it),
                    _seed_review_pass(it),
                ),
            ),
            # extract_results: omit metrics.json (but set last_run_id, create manifest)
            (
                "extract_results",
                lambda it, repo: (
                    _seed_slurm_manifest(
                        it,
                        "run_001",
                        host_mode="local",
                        status="completed",
                        sync_status="ok",
                    ),
                    _set_last_run_id(repo, "run_001"),
                ),
            ),
            # update_docs: omit docs_update.md
            ("update_docs", lambda _it, _repo: None),
            # decide_repeat: write malformed decision_result.json
            (
                "decide_repeat",
                lambda it, _repo: (it / "decision_result.json").write_text(
                    "{invalid json", encoding="utf-8"
                ),
            ),
        ],
        ids=[
            "hypothesis",
            "design",
            "implementation",
            "implementation_review",
            "launch",
            "extract_results",
            "update_docs",
            "decide_repeat",
        ],
    )
    def test_stage_check_error_retries(
        self, tmp_path: Path, stage: str, setup_fn
    ) -> None:
        state_kwargs: dict[str, Any] = {"stage": stage, "stage_attempt": 0}
        if stage == "extract_results":
            state_kwargs["last_run_id"] = "run_001"
        repo, state_path, it_dir = _setup_repo(tmp_path, **state_kwargs)
        setup_fn(it_dir, repo)

        outcome = _run(state_path)

        if stage == "decide_repeat":
            # decide_repeat with malformed artifact blocks (exit_code=0, no transition)
            assert not outcome.transitioned
            assert outcome.stage_after == "decide_repeat"
        else:
            assert outcome.exit_code == 1
            persisted = _read_state(repo)
            assert persisted["stage_attempt"] == 1
            assert persisted["stage"] == stage

    def test_slurm_monitor_strict_lifecycle_violation(self, tmp_path: Path) -> None:
        """slurm_monitor: SLURM manifest with sync=ok but status=running → StageCheckError."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="slurm_monitor", last_run_id="run_001"
        )
        _seed_slurm_manifest(
            it_dir, "run_001", host_mode="slurm", status="running", sync_status="ok"
        )

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 1
        assert persisted["stage"] == "slurm_monitor"


def _set_last_run_id(repo: Path, run_id: str) -> None:
    """Helper to update last_run_id in persisted state."""
    state = _read_state(repo)
    state["last_run_id"] = run_id
    _write_state(repo, state)


# ---------------------------------------------------------------------------
# 3. TestRetryBudgetExhaustionAtEveryStage
# ---------------------------------------------------------------------------


class TestRetryBudgetExhaustionAtEveryStage:
    """Same triggers as class 2 but at max_stage_attempts - 1.

    Assert escalation to human_review.
    """

    @pytest.mark.parametrize(
        "stage, setup_fn",
        [
            ("hypothesis", lambda _it, _repo: None),
            ("design", lambda _it, _repo: None),
            ("implementation", lambda _it, _repo: None),
            ("implementation_review", lambda _it, _repo: None),
            (
                "launch",
                lambda it, _repo: (
                    _seed_design(it),
                    _seed_review_pass(it),
                ),
            ),
            (
                "extract_results",
                lambda it, repo: (
                    _seed_slurm_manifest(
                        it,
                        "run_001",
                        host_mode="local",
                        status="completed",
                        sync_status="ok",
                    ),
                    _set_last_run_id(repo, "run_001"),
                ),
            ),
            ("update_docs", lambda _it, _repo: None),
        ],
        ids=[
            "hypothesis",
            "design",
            "implementation",
            "implementation_review",
            "launch",
            "extract_results",
            "update_docs",
        ],
    )
    def test_retry_budget_exhaustion_escalates(
        self, tmp_path: Path, stage: str, setup_fn
    ) -> None:
        state_kwargs: dict[str, Any] = {
            "stage": stage,
            "stage_attempt": 2,
            "max_stage_attempts": 3,
        }
        if stage == "extract_results":
            state_kwargs["last_run_id"] = "run_001"
        repo, state_path, it_dir = _setup_repo(tmp_path, **state_kwargs)
        setup_fn(it_dir, repo)

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"

    def test_slurm_monitor_budget_exhaustion(self, tmp_path: Path) -> None:
        """slurm_monitor strict lifecycle violation at max attempts → human_review."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="slurm_monitor",
            stage_attempt=2,
            max_stage_attempts=3,
            last_run_id="run_001",
        )
        _seed_slurm_manifest(
            it_dir, "run_001", host_mode="slurm", status="running", sync_status="ok"
        )

        outcome = _run(state_path)

        assert outcome.exit_code == 1
        persisted = _read_state(repo)
        assert persisted["stage"] == "human_review"


# ---------------------------------------------------------------------------
# 4. TestTerminalStageNoOps
# ---------------------------------------------------------------------------


class TestTerminalStageNoOps:
    """Terminal stages return transitioned=False, exit_code=0."""

    @pytest.mark.parametrize("stage", ["human_review", "stop"])
    def test_terminal_noop(self, tmp_path: Path, stage: str) -> None:
        repo, state_path, it_dir = _setup_repo(tmp_path, stage=stage)

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert outcome.exit_code == 0
        assert outcome.stage_after == stage


# ---------------------------------------------------------------------------
# 5. TestImplementationReviewRetryLoop
# ---------------------------------------------------------------------------


class TestImplementationReviewRetryLoop:
    """Multi-step implementation review retry scenarios."""

    def test_single_retry_then_pass(self, tmp_path: Path) -> None:
        """impl_review(retry) → implementation → impl_review(pass) → launch."""
        # Step 1: impl_review(retry) → implementation
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="implementation_review", stage_attempt=0
        )
        _seed_review_retry(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 1

        # Step 2: implementation → implementation_review (carries attempt)
        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 1  # carried

        # Step 3: impl_review(pass) → launch
        _seed_review_pass(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "launch"
        persisted = _read_state(repo)
        assert persisted["stage_attempt"] == 0  # reset on forward transition

    def test_budget_exhaustion_at_review(self, tmp_path: Path) -> None:
        """impl_review(retry) at attempt=2, max=3 → human_review."""
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

    def test_two_retries_then_pass(self, tmp_path: Path) -> None:
        """impl_review(retry) × 2 → impl_review(pass) → launch."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="implementation_review",
            stage_attempt=0,
            max_stage_attempts=5,  # enough budget
        )

        # Retry 1
        _seed_review_retry(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"
        assert _read_state(repo)["stage_attempt"] == 1

        # Back to review
        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"

        # Retry 2
        _seed_review_retry(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation"
        assert _read_state(repo)["stage_attempt"] == 2

        # Back to review
        _seed_implementation(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "implementation_review"

        # Pass
        _seed_review_pass(it_dir)
        outcome = _run(state_path)
        assert outcome.stage_after == "launch"


# ---------------------------------------------------------------------------
# 6. TestGuardrailBreaches
# ---------------------------------------------------------------------------


class TestGuardrailBreaches:
    """Guardrail breach scenarios at decide_repeat."""

    def test_same_decision_streak_breach(self, tmp_path: Path) -> None:
        """Streak exceeding max → human_review."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="decide_repeat",
            repeat_guard={
                "last_decision": "design",
                "same_decision_streak": 3,  # max=3, one more same → 4 > 3
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

    def test_no_progress_decisions_breach(self, tmp_path: Path) -> None:
        """no_progress_decisions at max → human_review."""
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
        # Write 3 open tasks to match last_open_task_count
        _write_todo_md(
            repo,
            "# Tasks\n- [ ] [hypothesis] task1\n- [ ] [design] task2\n- [ ] [implementation] task3\n",
        )
        _write_todo_state(repo, {})

        outcome = _run(
            state_path, decision="design", auto_mode=True, auto_decision=True
        )

        assert outcome.stage_after == "human_review"

    def test_update_docs_cycle_limit_breach(self, tmp_path: Path) -> None:
        """update_docs_cycle_count exceeding max → human_review."""
        repo, state_path, it_dir = _setup_repo(
            tmp_path,
            stage="extract_results",
            last_run_id="run_001",
            repeat_guard={
                "last_decision": "",
                "same_decision_streak": 0,
                "last_open_task_count": -1,
                "no_progress_decisions": 0,
                "update_docs_cycle_count": 3,  # max=3, +1 = 4 > 3
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

    def test_streak_resets_on_different_decision(self, tmp_path: Path) -> None:
        """Different decision resets streak, no breach."""
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

    def test_no_progress_resets_when_task_count_drops(self, tmp_path: Path) -> None:
        """When open task count decreases, no_progress_decisions resets to 0."""
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
        # 2 manual tasks + ~3 generated = ~5, well below 20
        _write_todo_md(
            repo, "# Tasks\n- [ ] [hypothesis] task1\n- [ ] [design] task2\n"
        )
        _write_todo_state(repo, {})

        outcome = _run(
            state_path, decision="design", auto_mode=True, auto_decision=True
        )

        assert outcome.stage_after == "design"
        persisted = _read_state(repo)
        assert persisted["repeat_guard"]["no_progress_decisions"] == 0


# ---------------------------------------------------------------------------
# 7. TestLaunchModeMatrix
# ---------------------------------------------------------------------------


class TestLaunchModeMatrix:
    """Test _execute_launch_runtime / per-mode functions for all launch modes."""

    def _make_launch_repo(
        self,
        tmp_path: Path,
        *,
        mode: str = "local",
        local_script: str = "echo ok\n",
        slurm_script: str = "echo ok\n",
    ) -> tuple[Path, Path, dict[str, Any]]:
        """Create a minimal repo + iteration for launch testing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        iteration_id = "iter_test_001"
        iteration_dir = repo / "experiments" / "plan" / iteration_id
        iteration_dir.mkdir(parents=True, exist_ok=True)

        # design.yaml
        design = {
            "id": "d1",
            "iteration_id": iteration_id,
            "hypothesis_id": "h1",
            "entrypoint": {"module": "train", "args": {}},
            "compute": {"location": mode, "cpus": 1, "gpus": 0},
            "metrics": ["loss"],
            "baselines": [{"name": "baseline1", "value": 1.0}],
        }
        (iteration_dir / "design.yaml").write_text(
            yaml.safe_dump(design, sort_keys=False), encoding="utf-8"
        )

        # launch scripts
        launch_dir = iteration_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_local.sh").write_text(
            "#!/usr/bin/env bash\nset -euo pipefail\n" + local_script, encoding="utf-8"
        )
        (launch_dir / "run_slurm.sbatch").write_text(
            "#!/usr/bin/env bash\n#SBATCH --job-name=test\n" + slurm_script,
            encoding="utf-8",
        )

        # policy
        policy_path = repo / ".autolab" / "verifier_policy.yaml"
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(
            yaml.safe_dump(
                {
                    "launch": {
                        "execute": True,
                        "local_timeout_seconds": 30,
                        "slurm_submit_timeout_seconds": 10,
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        state = {
            "iteration_id": iteration_id,
            "experiment_id": "e1",
            "stage": "launch",
            "stage_attempt": 0,
            "last_run_id": "",
            "pending_run_id": "run_001",
            "sync_status": "",
            "run_group": [],
            "max_stage_attempts": 3,
            "max_total_iterations": 20,
        }
        return repo, iteration_dir, state

    # --- Local mode ---

    def test_local_completed(self, tmp_path: Path) -> None:
        """Local script produces artifacts → status=completed."""
        repo, it_dir, state = self._make_launch_repo(
            tmp_path,
            mode="local",
            local_script=(
                'mkdir -p "runs/$AUTOLAB_RUN_ID"\n'
                'echo \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\n'
            ),
        )

        result = _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"
        assert result.run_id == "run_001"

    def test_local_failed_nonzero_exit(self, tmp_path: Path) -> None:
        """Local script exits non-zero → StageCheckError, manifest status=failed."""
        repo, it_dir, state = self._make_launch_repo(
            tmp_path, mode="local", local_script="exit 1\n"
        )

        with pytest.raises(StageCheckError, match="failed"):
            _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"

    def test_local_partial_no_artifacts(self, tmp_path: Path) -> None:
        """Local script exits 0 but produces no artifacts → StageCheckError, status=partial."""
        repo, it_dir, state = self._make_launch_repo(
            tmp_path, mode="local", local_script="echo done\n"
        )

        with pytest.raises(StageCheckError, match="partial"):
            _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "partial"

    def test_local_fatal_marker(self, tmp_path: Path) -> None:
        """Local script exits 0 with RuntimeError: in stderr → StageCheckError, status=failed."""
        repo, it_dir, state = self._make_launch_repo(
            tmp_path,
            mode="local",
            local_script=(
                'mkdir -p "runs/$AUTOLAB_RUN_ID"\n'
                'echo \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\n'
                'echo "RuntimeError: something broke" >&2\n'
            ),
        )

        with pytest.raises(StageCheckError, match="failed"):
            _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"

    def test_local_run_id_drift(self, tmp_path: Path) -> None:
        """Local script writes to wrong run dir → StageCheckError, status=failed."""
        repo, it_dir, state = self._make_launch_repo(
            tmp_path,
            mode="local",
            local_script=(
                'mkdir -p "runs/$AUTOLAB_RUN_ID"\n'
                'echo \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\n'
                # Create a sibling dir with a different name
                'mkdir -p "runs/WRONG_RUN"\n'
                'echo bad > "runs/WRONG_RUN/output.json"\n'
            ),
        )

        with pytest.raises(StageCheckError, match="failed"):
            _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"

    def test_local_timeout(self, tmp_path: Path) -> None:
        """Local subprocess timeout → StageCheckError, manifest status=failed."""
        repo, it_dir, state = self._make_launch_repo(
            tmp_path, mode="local", local_script="sleep 999\n"
        )

        # Override the timeout to something tiny
        policy_path = repo / ".autolab" / "verifier_policy.yaml"
        policy_path.write_text(
            yaml.safe_dump(
                {
                    "launch": {
                        "execute": True,
                        "local_timeout_seconds": 1,
                        "slurm_submit_timeout_seconds": 10,
                    }
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        with pytest.raises(StageCheckError, match="failed"):
            _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"

    # --- SLURM submit mode ---

    def test_slurm_submit_success(self, tmp_path: Path) -> None:
        """SLURM sbatch returns job ID → status=submitted."""
        repo, it_dir, state = self._make_launch_repo(tmp_path, mode="slurm")

        fake_proc = subprocess.CompletedProcess(
            args=["sbatch", "launch/run_slurm.sbatch"],
            returncode=0,
            stdout="Submitted batch job 12345\n",
            stderr="",
        )
        with mock.patch(
            "autolab.launch_runtime.subprocess.run", return_value=fake_proc
        ):
            with mock.patch(
                "autolab.launch_runtime._is_slurm_interactive_session",
                return_value=False,
            ):
                result = _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "submitted"
        assert manifest["job_id"] == "12345"

    def test_slurm_submit_failed(self, tmp_path: Path) -> None:
        """SLURM sbatch returns non-zero → StageCheckError, status=failed."""
        repo, it_dir, state = self._make_launch_repo(tmp_path, mode="slurm")

        fake_proc = subprocess.CompletedProcess(
            args=["sbatch", "launch/run_slurm.sbatch"],
            returncode=1,
            stdout="",
            stderr="Error: invalid partition",
        )
        with mock.patch(
            "autolab.launch_runtime.subprocess.run", return_value=fake_proc
        ):
            with mock.patch(
                "autolab.launch_runtime._is_slurm_interactive_session",
                return_value=False,
            ):
                with pytest.raises(StageCheckError, match="failed"):
                    _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"

    def test_slurm_submit_missing_job_id(self, tmp_path: Path) -> None:
        """SLURM sbatch returns 0 but no job ID → StageCheckError, status=failed."""
        repo, it_dir, state = self._make_launch_repo(tmp_path, mode="slurm")

        fake_proc = subprocess.CompletedProcess(
            args=["sbatch", "launch/run_slurm.sbatch"],
            returncode=0,
            stdout="Something unexpected\n",
            stderr="",
        )
        with mock.patch(
            "autolab.launch_runtime.subprocess.run", return_value=fake_proc
        ):
            with mock.patch(
                "autolab.launch_runtime._is_slurm_interactive_session",
                return_value=False,
            ):
                with pytest.raises(StageCheckError, match="failed"):
                    _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"

    # --- SLURM interactive mode ---

    def test_slurm_interactive_completed(self, tmp_path: Path) -> None:
        """SLURM interactive: script produces artifacts → status=completed."""
        repo, it_dir, state = self._make_launch_repo(
            tmp_path,
            mode="slurm",
            slurm_script=(
                'mkdir -p "runs/$AUTOLAB_RUN_ID"\n'
                'echo \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\n'
            ),
        )

        with mock.patch(
            "autolab.launch_runtime._is_slurm_interactive_session", return_value=True
        ):
            with mock.patch(
                "autolab.launch_runtime._get_slurm_allocation_resources",
                return_value={"cpus": 8, "memory_mb": 32768, "gpu_count": 0},
            ):
                with mock.patch.dict("os.environ", {"SLURM_JOB_ID": "99999"}):
                    result = _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "completed"

    def test_slurm_interactive_failed(self, tmp_path: Path) -> None:
        """SLURM interactive: script exits non-zero → StageCheckError, status=failed."""
        repo, it_dir, state = self._make_launch_repo(
            tmp_path, mode="slurm", slurm_script="exit 1\n"
        )

        with mock.patch(
            "autolab.launch_runtime._is_slurm_interactive_session", return_value=True
        ):
            with mock.patch(
                "autolab.launch_runtime._get_slurm_allocation_resources",
                return_value={"cpus": 8, "memory_mb": 32768, "gpu_count": 0},
            ):
                with mock.patch.dict("os.environ", {"SLURM_JOB_ID": "99999"}):
                    with pytest.raises(StageCheckError, match="failed"):
                        _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "failed"

    def test_slurm_interactive_partial(self, tmp_path: Path) -> None:
        """SLURM interactive: script exits 0, no artifacts → StageCheckError, status=partial."""
        repo, it_dir, state = self._make_launch_repo(
            tmp_path, mode="slurm", slurm_script="echo done\n"
        )

        with mock.patch(
            "autolab.launch_runtime._is_slurm_interactive_session", return_value=True
        ):
            with mock.patch(
                "autolab.launch_runtime._get_slurm_allocation_resources",
                return_value={"cpus": 8, "memory_mb": 32768, "gpu_count": 0},
            ):
                with mock.patch.dict("os.environ", {"SLURM_JOB_ID": "99999"}):
                    with pytest.raises(StageCheckError, match="partial"):
                        _execute_launch_runtime(repo, state=state)

        manifest_path = it_dir / "runs" / "run_001" / "run_manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["status"] == "partial"


# ---------------------------------------------------------------------------
# 8. TestSlurmSyncStatusMatrix
# ---------------------------------------------------------------------------


class TestSlurmSyncStatusMatrix:
    """Test slurm_monitor with various manifest (status, sync_status) combos."""

    @pytest.mark.parametrize(
        "manifest_status, sync_status, expect_transition, expected_stage",
        [
            # Completes → extract_results
            ("synced", "ok", True, "extract_results"),
            ("completed", "ok", True, "extract_results"),
            ("failed", "failed", True, "extract_results"),
            ("partial", "failed", True, "extract_results"),
            # Blocks → stays at slurm_monitor
            ("running", "pending", False, "slurm_monitor"),
            ("submitted", "pending", False, "slurm_monitor"),
            ("pending", "", False, "slurm_monitor"),
        ],
        ids=[
            "synced_ok",
            "completed_ok",
            "failed_failed",
            "partial_failed",
            "running_pending",
            "submitted_pending",
            "pending_empty",
        ],
    )
    def test_slurm_monitor_matrix(
        self,
        tmp_path: Path,
        manifest_status: str,
        sync_status: str,
        expect_transition: bool,
        expected_stage: str,
    ) -> None:
        repo, state_path, it_dir = _setup_repo(
            tmp_path, stage="slurm_monitor", last_run_id="run_001"
        )
        _seed_slurm_manifest(
            it_dir,
            "run_001",
            host_mode="slurm",
            status=manifest_status,
            sync_status=sync_status,
        )

        outcome = _run(state_path)

        assert outcome.transitioned == expect_transition
        persisted = _read_state(repo)
        assert persisted["stage"] == expected_stage


# ---------------------------------------------------------------------------
# 9. TestDecisionArtifactEdgeCases
# ---------------------------------------------------------------------------


class TestDecisionArtifactEdgeCases:
    """Test decide_repeat behavior with various decision_result.json states."""

    def test_valid_decision_transitions(self, tmp_path: Path) -> None:
        """Valid decision_result.json with all fields → transitions."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")
        _seed_decision_result(it_dir, decision="design")

        outcome = _run(state_path)

        assert outcome.transitioned
        assert outcome.stage_after == "design"

    def test_missing_rationale_blocks(self, tmp_path: Path) -> None:
        """Missing rationale field → blocks."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")
        payload = {
            "schema_version": "1.0",
            "decision": "design",
            # rationale intentionally missing
            "evidence": [
                {
                    "source": "metrics",
                    "pointer": "runs/run_001/metrics.json",
                    "summary": "Target not met",
                }
            ],
        }
        (it_dir / "decision_result.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert outcome.stage_after == "decide_repeat"
        assert "rationale" in outcome.message

    def test_invalid_json_blocks(self, tmp_path: Path) -> None:
        """Invalid JSON in decision_result.json → blocks."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")
        (it_dir / "decision_result.json").write_text("{invalid json", encoding="utf-8")

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert "Invalid decision artifact" in outcome.message

    def test_decision_not_in_decision_stages_blocks(self, tmp_path: Path) -> None:
        """decision not in DECISION_STAGES → blocks."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")
        payload = {
            "schema_version": "1.0",
            "decision": "implementation",  # not a valid decision stage
            "rationale": "Test rationale.",
            "evidence": [
                {
                    "source": "metrics",
                    "pointer": "runs/run_001/metrics.json",
                    "summary": "Target not met",
                }
            ],
        }
        (it_dir / "decision_result.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert outcome.stage_after == "decide_repeat"

    def test_empty_evidence_blocks(self, tmp_path: Path) -> None:
        """Empty evidence array → blocks (evidence is required and non-empty)."""
        repo, state_path, it_dir = _setup_repo(tmp_path, stage="decide_repeat")
        payload = {
            "schema_version": "1.0",
            "decision": "design",
            "rationale": "More refinement needed.",
            "evidence": [],  # empty
        }
        (it_dir / "decision_result.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8"
        )

        outcome = _run(state_path)

        assert not outcome.transitioned
        assert "evidence" in outcome.message
