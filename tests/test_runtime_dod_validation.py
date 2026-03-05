from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
import yaml

from autolab import handoff, plan_execution, run_standard
from autolab.models import PlanExecutionConfig, PlanExecutionImplementationConfig
from autolab.validators import _validate_stage_readiness
from autolab.wave_observability import build_wave_observability


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_policy(repo: Path) -> None:
    payload: dict[str, object] = {
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
        "agent_runner": {"enabled": False, "stages": []},
        "autorun": {
            "auto_commit": {"mode": "off"},
            "guardrails": {
                "max_same_decision_streak": 3,
                "max_no_progress_decisions": 2,
                "max_update_docs_cycles": 3,
                "on_breach": "human_review",
            },
        },
    }
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _write_backlog(repo: Path, *, iteration_id: str = "iter1") -> None:
    backlog: dict[str, object] = {
        "hypotheses": [
            {
                "id": "h1",
                "status": "open",
                "title": "Runtime DoD validation",
                "success_metric": "metric",
                "target_delta": 0.0,
            }
        ],
        "experiments": [
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "iteration_id": iteration_id,
            }
        ],
    }
    backlog_path = repo / ".autolab" / "backlog.yaml"
    backlog_path.parent.mkdir(parents=True, exist_ok=True)
    backlog_path.write_text(
        yaml.safe_dump(backlog, sort_keys=False),
        encoding="utf-8",
    )


def _write_deterministic_workflow(repo: Path) -> None:
    workflow: dict[str, object] = {
        "schema_version": "1.0",
        "stages": {
            "launch": {
                "prompt_file": "missing_launch_prompt.audit.md",
                "required_tokens": ["iteration_id", "iteration_path", "run_id"],
                "required_outputs": ["runs/<RUN_ID>/run_manifest.json"],
                "next_stage": "slurm_monitor",
                "verifier_categories": {},
                "classifications": {
                    "active": True,
                    "terminal": False,
                    "decision": False,
                    "runner_eligible": False,
                },
            },
            "slurm_monitor": {
                "prompt_file": "missing_slurm_monitor_prompt.audit.md",
                "required_tokens": ["iteration_id", "iteration_path", "run_id"],
                "required_outputs": ["runs/<RUN_ID>/run_manifest.json"],
                "next_stage": "extract_results",
                "verifier_categories": {},
                "classifications": {
                    "active": True,
                    "terminal": False,
                    "decision": False,
                    "runner_eligible": False,
                },
            },
        },
    }
    workflow_path = repo / ".autolab" / "workflow.yaml"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False),
        encoding="utf-8",
    )


def _base_state(*, stage: str, iteration_id: str = "iter1") -> dict[str, object]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": "e1",
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": "",
        "pending_run_id": "",
        "sync_status": "",
        "max_stage_attempts": 3,
        "max_total_iterations": 10,
        "assistant_mode": "off",
        "current_task_id": "",
        "task_cycle_stage": "select",
        "repeat_guard": {
            "last_decision": "",
            "same_decision_streak": 0,
            "last_open_task_count": -1,
            "no_progress_decisions": 0,
            "update_docs_cycle_count": 0,
            "last_verification_passed": False,
        },
        "task_change_baseline": {},
    }


def _seed_deterministic_runtime_repo(
    tmp_path: Path, *, stage: str
) -> tuple[Path, Path, dict[str, object]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_policy(repo)
    _write_backlog(repo)
    _write_deterministic_workflow(repo)
    _write_json(
        repo / ".autolab" / "todo_state.json",
        {"version": 1, "next_order": 1, "tasks": {}},
    )

    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)

    state = _base_state(stage=stage)
    if stage == "launch":
        design_payload = {
            "schema_version": "1.0",
            "id": "e1",
            "iteration_id": "iter1",
            "hypothesis_id": "h1",
            "entrypoint": {"module": "pkg.train", "args": {}},
            "compute": {"location": "local", "cpus": 1, "gpus": 0},
            "metrics": {"primary": {"name": "accuracy", "mode": "maximize"}},
            "baselines": [{"name": "baseline", "value": 0.0}],
            "implementation_requirements": [
                {
                    "requirement_id": "R1",
                    "description": "Validate deterministic launch runtime",
                    "scope_kind": "experiment",
                    "expected_artifacts": ["implementation_plan.md"],
                }
            ],
        }
        (iteration_dir / "design.yaml").write_text(
            yaml.safe_dump(design_payload, sort_keys=False),
            encoding="utf-8",
        )
        launch_dir = iteration_dir / "launch"
        launch_dir.mkdir(parents=True, exist_ok=True)
        (launch_dir / "run_local.sh").write_text(
            (
                "#!/usr/bin/env bash\n"
                "set -euo pipefail\n"
                'mkdir -p "runs/$AUTOLAB_RUN_ID"\n'
                'echo \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\n'
            ),
            encoding="utf-8",
        )
    elif stage == "slurm_monitor":
        run_dir = iteration_dir / "runs" / "run_001"
        run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(
            run_dir / "run_manifest.json",
            {
                "schema_version": "1.0",
                "run_id": "run_001",
                "iteration_id": "iter1",
                "host_mode": "local",
                "launch_mode": "local",
                "status": "completed",
                "command": "bash launch/run_local.sh",
                "resource_request": {"cpus": 1, "memory": "4GB", "gpu_count": 0},
                "artifact_sync_to_local": {"status": "ok"},
                "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
            },
        )
        state["last_run_id"] = "run_001"
    else:
        raise AssertionError(f"unsupported test stage: {stage}")

    state_path = repo / ".autolab" / "state.json"
    _write_json(state_path, state)
    return repo, state_path, state


def _plan_config() -> PlanExecutionConfig:
    return PlanExecutionConfig(
        implementation=PlanExecutionImplementationConfig(
            enabled=True,
            run_unit="wave",
            max_parallel_tasks=1,
            task_retry_max=0,
            wave_retry_max=0,
            failure_mode="finish_wave_then_stop",
            on_wave_retry_exhausted="human_review",
            require_verification_commands=False,
        )
    )


def _seed_plan_execution_repo(
    tmp_path: Path,
) -> tuple[Path, Path, dict[str, object], Path]:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    iteration_id = "iter1"
    state: dict[str, object] = {"iteration_id": iteration_id, "experiment_id": "e1"}
    state_path = repo_root / ".autolab" / "state.json"
    _write_json(state_path, state)

    iteration_dir = repo_root / "experiments" / "plan" / iteration_id
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "implementation_plan.md").write_text(
        "# Implementation Plan\n",
        encoding="utf-8",
    )
    design_payload = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": iteration_id,
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": {"location": "local", "cpus": 1, "gpus": 0},
        "metrics": {"primary": {"name": "accuracy", "mode": "maximize"}},
        "baselines": [{"name": "baseline", "value": 0.0}],
        "implementation_requirements": [
            {
                "requirement_id": "R1",
                "description": "Implement the runtime change",
                "scope_kind": "experiment",
                "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            }
        ],
    }
    (iteration_dir / "design.yaml").write_text(
        yaml.safe_dump(design_payload, sort_keys=False),
        encoding="utf-8",
    )

    tasks: list[dict[str, object]] = [
        {
            "task_id": "t1",
            "objective": "wave one task",
            "scope_kind": "experiment",
            "depends_on": [],
            "reads": [f"experiments/plan/{iteration_id}/design.yaml"],
            "writes": [f"experiments/plan/{iteration_id}/artifacts/t1.txt"],
            "touches": [f"experiments/plan/{iteration_id}/artifacts/t1.txt"],
            "conflict_group": "",
            "verification_commands": [],
            "manual_only_rationale": "runtime-only validation",
            "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            "failure_policy": "fail_fast",
            "can_run_in_parallel": True,
            "covers_requirements": ["R1"],
        },
        {
            "task_id": "t2",
            "objective": "wave two task",
            "scope_kind": "experiment",
            "depends_on": ["t1"],
            "reads": [f"experiments/plan/{iteration_id}/artifacts/t1.txt"],
            "writes": [f"experiments/plan/{iteration_id}/artifacts/t2.txt"],
            "touches": [f"experiments/plan/{iteration_id}/artifacts/t2.txt"],
            "conflict_group": "",
            "verification_commands": [],
            "manual_only_rationale": "runtime-only validation",
            "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            "failure_policy": "fail_fast",
            "can_run_in_parallel": True,
            "covers_requirements": ["R1"],
        },
    ]
    contract_payload: dict[str, object] = {
        "schema_version": "1.0",
        "iteration_id": iteration_id,
        "stage": "implementation",
        "generated_at": "2026-01-01T00:00:00Z",
        "tasks": tasks,
    }
    _write_json(repo_root / ".autolab" / "plan_contract.json", contract_payload)
    _write_json(iteration_dir / "plan_contract.json", contract_payload)
    return repo_root, state_path, state, iteration_dir


def test_plan_contract_executes_real_waves(monkeypatch, tmp_path: Path) -> None:
    repo_root, state_path, state, iteration_dir = _seed_plan_execution_repo(tmp_path)
    monkeypatch.setattr(
        plan_execution, "_load_plan_execution_config", lambda _repo: _plan_config()
    )
    monkeypatch.setattr(plan_execution, "_collect_change_snapshot", lambda _repo: {})
    monkeypatch.setattr(plan_execution, "_snapshot_delta_paths", lambda *_args: [])

    runner_reports: list[str] = []

    def _runner(*_args, **kwargs):
        report_name = str(kwargs.get("report_name", "")).strip()
        runner_reports.append(report_name)
        return {
            "status": "completed",
            "exit_code": 0,
            "report_name": report_name,
            "changed_paths": [],
        }

    monkeypatch.setattr(plan_execution, "_invoke_agent_runner", _runner)

    first = plan_execution.execute_implementation_plan_step(
        repo_root,
        state_path=state_path,
        state=state,
        run_agent_mode="policy",
        auto_mode=False,
    )
    second = plan_execution.execute_implementation_plan_step(
        repo_root,
        state_path=state_path,
        state=state,
        run_agent_mode="policy",
        auto_mode=False,
    )

    assert first.proceed_to_evaluate is False
    assert "next wave is 2" in first.summary
    assert second.proceed_to_evaluate is True
    assert second.summary == "implementation plan execution complete"
    assert runner_reports == [
        "runner_execution_report.plan.json",
        "runner_execution_report.t1.json",
        "runner_execution_report.t2.json",
    ]

    graph_payload = json.loads(
        (repo_root / ".autolab" / "plan_graph.json").read_text(encoding="utf-8")
    )
    assert graph_payload["waves"] == [
        {"wave": 1, "tasks": ["t1"]},
        {"wave": 2, "tasks": ["t2"]},
    ]

    state_payload = json.loads(
        (iteration_dir / "plan_execution_state.json").read_text(encoding="utf-8")
    )
    assert state_payload["wave_status"] == {"1": "completed", "2": "completed"}
    assert state_payload["task_status"] == {"t1": "completed", "t2": "completed"}

    summary_payload = json.loads(
        (iteration_dir / "plan_execution_summary.json").read_text(encoding="utf-8")
    )
    assert summary_payload["waves_total"] == 2
    assert summary_payload["waves_executed"] == 2
    assert summary_payload["tasks_completed"] == 2
    assert summary_payload["tasks_failed"] == 0
    assert summary_payload["critical_path"]["status"] == "available"
    assert summary_payload["observability_summary"]["waves_executed"] == 2
    assert [row["status"] for row in summary_payload["wave_details"]] == [
        "completed",
        "completed",
    ]
    assert [row["task_id"] for row in summary_payload["task_details"]] == ["t1", "t2"]
    assert all(
        row["reason_code"] == "completed" for row in summary_payload["task_details"]
    )


def test_wave_observability_ignores_stale_root_iteration_artifacts(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    iteration_dir = repo_root / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    autolab_dir = repo_root / ".autolab"
    autolab_dir.mkdir(parents=True, exist_ok=True)

    (autolab_dir / "plan_graph.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter2",
                "nodes": [
                    {
                        "task_id": "stale",
                        "scope_kind": "experiment",
                        "depth": 0,
                        "can_run_in_parallel": True,
                        "conflict_group": "",
                    }
                ],
                "edges": [],
                "waves": [{"wave": 1, "tasks": ["stale"]}],
            }
        ),
        encoding="utf-8",
    )
    (autolab_dir / "plan_check_result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter2",
                "errors": [
                    "same-wave write conflict: tasks stale and other overlap in writes/touches"
                ],
            }
        ),
        encoding="utf-8",
    )
    (autolab_dir / "plan_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter2",
                "tasks": [
                    {
                        "task_id": "stale",
                        "writes": ["src/stale.py"],
                        "touches": ["src/stale.py"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    observability = build_wave_observability(
        repo_root,
        iteration_dir=iteration_dir,
        execution_state_payload={"iteration_id": "iter1"},
        execution_summary_payload={
            "iteration_id": "iter1",
            "wave_details": [
                {
                    "wave": 1,
                    "status": "completed",
                    "tasks": ["t1"],
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:00:01Z",
                    "duration_seconds": 1.0,
                }
            ],
            "task_details": [
                {
                    "task_id": "t1",
                    "status": "completed",
                    "wave": 1,
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:00:01Z",
                    "duration_seconds": 1.0,
                }
            ],
        },
    )

    assert observability["status"] == "available"
    assert observability["critical_path"]["task_ids"] == ["t1"]
    assert observability["file_conflicts"] == []
    assert any(
        "plan_graph payload ignored" in item for item in observability["diagnostics"]
    )
    assert any(
        "plan_check_result payload ignored" in item
        for item in observability["diagnostics"]
    )


def test_wave_observability_conflicts_include_paths_when_contract_matches(
    tmp_path: Path,
) -> None:
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    iteration_dir = repo_root / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)

    observability = build_wave_observability(
        repo_root,
        iteration_dir=iteration_dir,
        contract_payload={
            "schema_version": "1.0",
            "iteration_id": "iter1",
            "tasks": [
                {
                    "task_id": "t1",
                    "writes": ["src/shared.py"],
                    "touches": ["src/shared.py"],
                },
                {
                    "task_id": "t2",
                    "writes": ["src/shared.py"],
                    "touches": ["src/shared.py"],
                },
            ],
        },
        graph_payload={
            "schema_version": "1.0",
            "iteration_id": "iter1",
            "nodes": [
                {
                    "task_id": "t1",
                    "scope_kind": "experiment",
                    "depth": 0,
                    "can_run_in_parallel": True,
                    "conflict_group": "",
                },
                {
                    "task_id": "t2",
                    "scope_kind": "experiment",
                    "depth": 0,
                    "can_run_in_parallel": True,
                    "conflict_group": "",
                },
            ],
            "edges": [],
            "waves": [{"wave": 1, "tasks": ["t1", "t2"]}],
        },
        plan_check_payload={
            "schema_version": "1.0",
            "iteration_id": "iter1",
            "errors": [
                "same-wave write conflict: tasks t1 and t2 overlap in writes/touches"
            ],
        },
        execution_state_payload={"iteration_id": "iter1"},
        execution_summary_payload={
            "iteration_id": "iter1",
            "task_details": [
                {
                    "task_id": "t1",
                    "status": "completed",
                    "wave": 1,
                    "duration_seconds": 1.0,
                    "files_changed": ["src/shared.py"],
                },
                {
                    "task_id": "t2",
                    "status": "completed",
                    "wave": 1,
                    "duration_seconds": 1.0,
                    "files_changed": ["src/shared.py"],
                },
            ],
        },
    )

    conflict = observability["file_conflicts"][0]
    assert conflict["paths"] == ["src/shared.py"]
    assert "src/shared.py" in conflict["detail"]


def test_handoff_markdown_surfaces_current_retry_and_conflict_path_detail() -> None:
    payload = {
        "current_scope": "experiment",
        "current_stage": "implementation",
        "wave": {"status": "available", "current": 1, "executed": 1, "total": 1},
        "task_status": {
            "status": "available",
            "total": 1,
            "completed": 0,
            "failed": 0,
            "blocked": 0,
            "pending": 1,
        },
        "latest_verifier_summary": {
            "generated_at": "",
            "stage_effective": "",
            "passed": True,
            "message": "",
        },
        "blocking_failures": [],
        "pending_human_decisions": [],
        "files_changed_since_last_green_point": [],
        "recommended_next_command": {
            "command": "autolab run",
            "reason": "ok",
            "executable": True,
        },
        "safe_resume_point": {
            "command": "autolab run",
            "status": "ready",
            "preconditions": [],
        },
        "wave_observability": {
            "critical_path": {},
            "waves": [
                {
                    "wave": 1,
                    "status": "failed",
                    "duration_seconds": 1.0,
                    "retries_used": 0,
                    "retry_pending": True,
                    "critical_path": True,
                    "tasks": ["t1"],
                    "current_retry_reasons": ["runner_failed"],
                    "retry_reasons": ["runner_failed"],
                    "blocked_task_ids": [],
                    "deferred_task_ids": ["t1"],
                    "skipped_task_ids": [],
                    "out_of_contract_paths": [],
                }
            ],
            "file_conflicts": [
                {
                    "wave": 1,
                    "kind": "same_wave_write_conflict",
                    "tasks": ["t1", "t2"],
                    "paths": ["src/shared.py"],
                    "conflict_group": "",
                    "detail": "same-wave write conflict",
                }
            ],
            "tasks": [],
            "diagnostics": [],
        },
    }

    markdown = handoff._render_handoff_markdown(payload)

    assert "retry_reasons.current: runner_failed" in markdown
    assert "paths=src/shared.py" in markdown


@pytest.mark.parametrize(
    ("stage", "expected_next"),
    (
        ("launch", "slurm_monitor"),
        ("slurm_monitor", "extract_results"),
    ),
)
def test_deterministic_runtime_stages_skip_prompts_and_runner(
    monkeypatch,
    tmp_path: Path,
    stage: str,
    expected_next: str,
) -> None:
    repo_root, state_path, seeded_state = _seed_deterministic_runtime_repo(
        tmp_path, stage=stage
    )
    assert not (repo_root / ".autolab" / "prompts").exists()

    ready, _message, details = _validate_stage_readiness(repo_root, dict(seeded_state))
    assert ready is True
    assert details.get("reason") == "runner_disabled"

    runner_spy = mock.Mock(
        side_effect=AssertionError("agent runner must not be invoked")
    )
    monkeypatch.setattr(run_standard, "_invoke_agent_runner", runner_spy)
    monkeypatch.setattr(run_standard, "_generate_run_id", lambda: "run_001")

    outcome = run_standard._run_once_standard(
        state_path,
        decision=None,
        run_agent_mode="policy",
        strict_implementation_progress=False,
    )

    assert outcome.exit_code == 0
    assert outcome.stage_after == expected_next
    assert runner_spy.call_count == 0
