from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import autolab.commands as commands_module
import pytest
from autolab.models import RunOutcome


def _copy_scaffold(repo: Path) -> None:
    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "autolab"
        / "scaffold"
        / ".autolab"
    )
    target = repo / ".autolab"
    shutil.copytree(source, target, dirs_exist_ok=True)


def _write_state(repo: Path, *, stage: str = "implementation") -> Path:
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "iteration_id": "iter1",
                "experiment_id": "e1",
                "stage": stage,
                "stage_attempt": 0,
                "last_run_id": "",
                "sync_status": "idle",
                "assistant_mode": "off",
                "max_stage_attempts": 3,
                "max_total_iterations": 20,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return state_path


def _write_current_plan_artifacts(repo: Path) -> None:
    autolab_dir = repo / ".autolab"
    autolab_dir.mkdir(parents=True, exist_ok=True)
    contract_payload = {
        "schema_version": "1.0",
        "iteration_id": "iter1",
        "stage": "implementation",
        "generated_at": "2026-03-05T00:00:00Z",
        "tasks": [
            {
                "task_id": "T1",
                "objective": "Update shared code safely",
                "scope_kind": "project_wide",
                "depends_on": [],
                "reads": ["src/shared.py"],
                "writes": ["src/shared.py"],
                "touches": ["src/shared.py"],
                "conflict_group": "shared",
                "verification_commands": ["python -m pytest -q"],
                "context_inputs": [],
                "manual_only_rationale": "",
                "expected_artifacts": ["src/shared.py"],
                "failure_policy": "fail_fast",
                "can_run_in_parallel": False,
                "covers_requirements": ["R_shared"],
            }
        ],
    }
    graph_payload = {"waves": [{"wave": 1, "tasks": ["T1"]}]}
    plan_check_payload = {
        "schema_version": "1.0",
        "generated_at": "2026-03-05T00:00:00Z",
        "stage": "implementation",
        "iteration_id": "iter1",
        "passed": True,
        "error_count": 0,
        "warning_count": 0,
        "errors": [],
        "warnings": [],
        "rule_results": [],
        "plan_hash": "plan-hash-current",
        "promotion_checks": {"status": "pass", "requirements": []},
        "approval_risk": {
            "requires_approval": True,
            "trigger_reasons": ["project_wide_tasks_present"],
            "counts": {
                "tasks_total": 1,
                "waves_total": 1,
                "project_wide_tasks": 1,
                "project_wide_unique_paths": 1,
                "observed_retries": 0,
                "stage_attempt": 0,
            },
            "project_wide_task_ids": ["T1"],
            "project_wide_unique_paths": ["src/shared.py"],
            "policy": {
                "enabled": True,
                "require_for_project_wide_tasks": True,
                "max_tasks_without_approval": 6,
                "max_waves_without_approval": 2,
                "max_project_wide_paths_without_approval": 3,
                "require_after_retries": True,
            },
            "plan_hash": "plan-hash-current",
            "risk_fingerprint": "risk-current",
        },
        "artifacts": {
            "contract_path": ".autolab/plan_contract.json",
            "snapshot_path": "experiments/plan/iter1/implementation_plan.md",
            "plan_check_result_path": ".autolab/plan_check_result.json",
            "plan_graph_path": ".autolab/plan_graph.json",
        },
    }
    (autolab_dir / "plan_contract.json").write_text(
        json.dumps(contract_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (autolab_dir / "plan_graph.json").write_text(
        json.dumps(graph_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (autolab_dir / "plan_check_result.json").write_text(
        json.dumps(plan_check_payload, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_stale_plan_approval(repo: Path) -> None:
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "plan_approval.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:01:00Z",
                "iteration_id": "iter1",
                "status": "approved",
                "requires_approval": True,
                "plan_hash": "plan-hash-stale",
                "risk_fingerprint": "risk-stale",
                "trigger_reasons": ["project_wide_tasks_present"],
                "counts": {
                    "tasks_total": 1,
                    "waves_total": 1,
                    "project_wide_tasks": 1,
                    "project_wide_unique_paths": 1,
                    "observed_retries": 0,
                    "stage_attempt": 0,
                },
                "reviewed_by": "reviewer",
                "reviewed_at": "2026-03-05T00:01:30Z",
                "notes": "stale approval",
                "source_paths": {
                    "plan_contract": ".autolab/plan_contract.json",
                    "plan_graph": ".autolab/plan_graph.json",
                    "plan_check_result": ".autolab/plan_check_result.json",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_status_shows_refresh_commands_for_superseded_plan_approval(
    tmp_path: Path,
    capsys,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_current_plan_artifacts(repo)
    _write_stale_plan_approval(repo)

    exit_code = commands_module.main(["status", "--state-file", str(state_path)])
    output = capsys.readouterr().out

    assert exit_code == 0
    assert "plan_approval:" in output
    assert "status: superseded" in output
    assert "autolab run --plan-only" in output
    assert "autolab run --execute-approved-plan" not in output


def test_loop_plan_only_stops_before_execution_and_preserves_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    seen: dict[str, object] = {}

    def _run_once_stub(
        _state_path: Path,
        _decision: str | None,
        *,
        assistant: bool = False,
        plan_only: bool = False,
        execute_approved_plan: bool = False,
        **_kwargs,
    ) -> RunOutcome:
        seen["assistant"] = assistant
        seen["plan_only"] = plan_only
        seen["execute_approved_plan"] = execute_approved_plan
        return RunOutcome(
            exit_code=0,
            transitioned=False,
            stage_before="implementation",
            stage_after="implementation",
            message="implementation plan prepared",
            pause_reason="plan_only",
        )

    monkeypatch.setattr(commands_module, "_run_once", _run_once_stub)
    monkeypatch.setattr(
        commands_module, "_acquire_lock", lambda *_a, **_k: (True, "ok")
    )
    monkeypatch.setattr(commands_module, "_release_lock", lambda *_a, **_k: None)
    monkeypatch.setattr(commands_module, "_append_log", lambda *_a, **_k: None)
    monkeypatch.setattr(
        commands_module, "_collect_change_snapshot", lambda *_a, **_k: {}
    )
    monkeypatch.setattr(
        commands_module,
        "_try_auto_commit",
        lambda *_a, **_k: "auto-commit: skipped",
    )
    monkeypatch.setattr(
        commands_module,
        "_safe_refresh_handoff",
        lambda *_a, **_k: ({}, ""),
    )

    exit_code = commands_module._cmd_loop(
        argparse.Namespace(
            state_file=str(state_path),
            max_iterations=3,
            max_hours=1.0,
            auto=False,
            run_agent_mode="policy",
            assistant=True,
            verify=False,
            strict_implementation_progress=True,
            plan_only=True,
            execute_approved_plan=False,
        )
    )
    output = capsys.readouterr().out

    assert exit_code == 0
    assert seen == {
        "assistant": True,
        "plan_only": True,
        "execute_approved_plan": False,
    }
    assert "autolab loop: stop (plan-only requested)" in output
