from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autolab import plan_execution
from autolab.models import PlanExecutionConfig, PlanExecutionImplementationConfig


def _plan_config(
    *,
    failure_mode: str = "finish_wave_then_stop",
    max_parallel_tasks: int = 4,
    task_retry_max: int = 0,
    wave_retry_max: int = 0,
    require_verification_commands: bool = False,
) -> PlanExecutionConfig:
    return PlanExecutionConfig(
        implementation=PlanExecutionImplementationConfig(
            enabled=True,
            run_unit="wave",
            max_parallel_tasks=max_parallel_tasks,
            task_retry_max=task_retry_max,
            wave_retry_max=wave_retry_max,
            failure_mode=failure_mode,
            on_wave_retry_exhausted="human_review",
            require_verification_commands=require_verification_commands,
        )
    )


def _seed_plan_files(
    tmp_path: Path,
    *,
    tasks: list[dict[str, object]],
    iteration_id: str = "iter1",
) -> tuple[Path, Path, dict[str, str], Path]:
    repo_root = tmp_path
    autolab_dir = repo_root / ".autolab"
    autolab_dir.mkdir(parents=True, exist_ok=True)
    state = {"iteration_id": iteration_id, "experiment_id": "exp1"}
    state_path = autolab_dir / "state.json"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    iteration_dir = repo_root / "experiments" / "plan" / iteration_id
    iteration_dir.mkdir(parents=True, exist_ok=True)

    contract_payload = {"schema_version": "1.0", "tasks": tasks}
    graph_payload = {
        "waves": [{"wave": 1, "tasks": [str(task["task_id"]) for task in tasks]}]
    }
    (autolab_dir / "plan_contract.json").write_text(
        json.dumps(contract_payload),
        encoding="utf-8",
    )
    (autolab_dir / "plan_graph.json").write_text(
        json.dumps(graph_payload),
        encoding="utf-8",
    )
    return repo_root, state_path, state, iteration_dir


def _patch_common_plan_execution(monkeypatch, config: PlanExecutionConfig) -> None:
    monkeypatch.setattr(
        plan_execution, "_load_plan_execution_config", lambda _repo: config
    )
    monkeypatch.setattr(
        plan_execution,
        "check_implementation_plan_contract",
        lambda *_args, **_kwargs: (True, "ok", {}),
    )
    monkeypatch.setattr(plan_execution, "_collect_change_snapshot", lambda _repo: {})
    monkeypatch.setattr(
        plan_execution,
        "_snapshot_delta_paths",
        lambda _before, _after: [],
    )


def test_execute_task_treats_zero_exit_code_as_success(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        plan_execution,
        "_invoke_agent_runner",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "exit_code": 0,
            "changed_paths": [],
        },
    )
    result = plan_execution._execute_task(
        tmp_path,
        state_path=tmp_path / "state.json",
        iteration_id="iter1",
        iteration_path="experiments/plan/iter1",
        iteration_dir=tmp_path / "experiments" / "plan" / "iter1",
        project_wide_root=tmp_path,
        wave=1,
        task={"task_id": "t1", "verification_commands": []},
        task_retry_max=0,
        require_verification_commands=False,
    )
    assert result["status"] == "completed"
    assert result["attempts"] == 1


def test_execute_task_runner_timeout_becomes_structured_failure(
    monkeypatch, tmp_path: Path
) -> None:
    def _raise_timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="runner", timeout=5)

    monkeypatch.setattr(plan_execution, "_invoke_agent_runner", _raise_timeout)
    result = plan_execution._execute_task(
        tmp_path,
        state_path=tmp_path / "state.json",
        iteration_id="iter1",
        iteration_path="experiments/plan/iter1",
        iteration_dir=tmp_path / "experiments" / "plan" / "iter1",
        project_wide_root=tmp_path,
        wave=1,
        task={"task_id": "t1", "verification_commands": []},
        task_retry_max=1,
        require_verification_commands=False,
    )
    assert result["status"] == "failed"
    assert result["attempts"] == 2
    assert result["retries_used"] == 1
    assert "timed out" in str(result["error"]).lower()


def test_execute_task_verification_timeout_becomes_structured_failure(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(
        plan_execution,
        "_invoke_agent_runner",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "exit_code": 0,
            "changed_paths": [],
        },
    )

    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="verify", timeout=300)

    monkeypatch.setattr(plan_execution.subprocess, "run", _timeout)
    result = plan_execution._execute_task(
        tmp_path,
        state_path=tmp_path / "state.json",
        iteration_id="iter1",
        iteration_path="experiments/plan/iter1",
        iteration_dir=tmp_path / "experiments" / "plan" / "iter1",
        project_wide_root=tmp_path,
        wave=1,
        task={"task_id": "t1", "verification_commands": ["python -m pytest -q"]},
        task_retry_max=1,
        require_verification_commands=False,
    )
    assert result["status"] == "failed"
    assert result["attempts"] == 2
    assert result["retries_used"] == 1
    assert "timed out" in str(result["error"]).lower()


def test_planning_pass_accepts_zero_exit_code(monkeypatch, tmp_path: Path) -> None:
    config = _plan_config(task_retry_max=0, wave_retry_max=0)
    _patch_common_plan_execution(monkeypatch, config)
    repo_root, state_path, state, _iteration_dir = _seed_plan_files(
        tmp_path,
        tasks=[
            {
                "task_id": "t1",
                "depends_on": [],
                "writes": [],
                "touches": [],
                "verification_commands": [],
                "failure_policy": "fail_fast",
            }
        ],
    )

    def _runner(*_args, **kwargs):
        report_name = str(kwargs.get("report_name", "")).strip()
        if report_name == "runner_execution_report.plan.json":
            return {
                "status": "completed",
                "exit_code": 0,
                "report_name": report_name,
                "changed_paths": [],
            }
        if report_name == "runner_execution_report.t1.json":
            return {
                "status": "completed",
                "exit_code": 0,
                "report_name": report_name,
                "changed_paths": [],
            }
        raise AssertionError(f"unexpected report_name: {report_name}")

    monkeypatch.setattr(plan_execution, "_invoke_agent_runner", _runner)
    result = plan_execution.execute_implementation_plan_step(
        repo_root,
        state_path=state_path,
        state=state,
        run_agent_mode="policy",
        auto_mode=False,
    )
    assert result.agent_status == "complete"
    assert result.exit_code == 0
    assert "planning pass failed" not in result.summary


def test_substitute_task_command_supports_scope_root_token() -> None:
    rendered = plan_execution._substitute_task_command(
        "echo {scope_root} {{scope_root}} <SCOPE_ROOT>",
        iteration_id="iter1",
        iteration_path="experiments/plan/iter1",
        task_id="T1",
        scope_root="/tmp/scope",
    )
    assert rendered == "echo /tmp/scope /tmp/scope /tmp/scope"


def test_expected_artifacts_accept_scope_root_relative_paths(tmp_path: Path) -> None:
    scope_root = tmp_path / "src"
    scope_root.mkdir(parents=True)
    (scope_root / "out.txt").write_text("ok\n", encoding="utf-8")
    iteration_dir = tmp_path / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True)

    missing = plan_execution._task_expected_artifacts_missing(
        repo_root=tmp_path,
        iteration_dir=iteration_dir,
        scope_root=scope_root,
        task={"expected_artifacts": ["out.txt"]},
    )
    assert missing == []


def test_failure_mode_fail_fast_halts_remaining_wave_tasks(
    monkeypatch, tmp_path: Path
) -> None:
    config = _plan_config(
        failure_mode="fail_fast",
        max_parallel_tasks=4,
        task_retry_max=0,
        wave_retry_max=0,
    )
    _patch_common_plan_execution(monkeypatch, config)
    repo_root, state_path, state, iteration_dir = _seed_plan_files(
        tmp_path,
        tasks=[
            {
                "task_id": "t1",
                "depends_on": [],
                "writes": [],
                "touches": [],
                "verification_commands": [],
                "failure_policy": "fail_fast",
            },
            {
                "task_id": "t2",
                "depends_on": [],
                "writes": [],
                "touches": [],
                "verification_commands": [],
                "failure_policy": "fail_fast",
            },
        ],
    )
    seen_reports: list[str] = []

    def _runner(*_args, **kwargs):
        report_name = str(kwargs.get("report_name", "")).strip()
        seen_reports.append(report_name)
        if report_name == "runner_execution_report.plan.json":
            return {
                "status": "completed",
                "exit_code": 0,
                "report_name": report_name,
                "changed_paths": [],
            }
        if report_name == "runner_execution_report.t1.json":
            return {
                "status": "failed",
                "exit_code": 2,
                "report_name": report_name,
                "changed_paths": [],
            }
        if report_name == "runner_execution_report.t2.json":
            return {
                "status": "completed",
                "exit_code": 0,
                "report_name": report_name,
                "changed_paths": [],
            }
        raise AssertionError(f"unexpected report_name: {report_name}")

    monkeypatch.setattr(plan_execution, "_invoke_agent_runner", _runner)
    result = plan_execution.execute_implementation_plan_step(
        repo_root,
        state_path=state_path,
        state=state,
        run_agent_mode="policy",
        auto_mode=False,
    )
    assert result.agent_status == "failed"
    assert "runner_execution_report.t2.json" not in seen_reports

    execution_state = json.loads(
        (iteration_dir / "plan_execution_state.json").read_text(encoding="utf-8")
    )
    assert execution_state["task_attempt_counts"]["t1"] == 1
    assert execution_state["task_attempt_counts"]["t2"] == 0
    assert execution_state["task_status"]["t2"] == "pending"


def test_failure_mode_finish_wave_then_stop_runs_remaining_tasks(
    monkeypatch, tmp_path: Path
) -> None:
    config = _plan_config(
        failure_mode="finish_wave_then_stop",
        max_parallel_tasks=4,
        task_retry_max=0,
        wave_retry_max=0,
    )
    _patch_common_plan_execution(monkeypatch, config)
    repo_root, state_path, state, iteration_dir = _seed_plan_files(
        tmp_path,
        tasks=[
            {
                "task_id": "t1",
                "depends_on": [],
                "writes": [],
                "touches": [],
                "verification_commands": [],
                "failure_policy": "fail_fast",
            },
            {
                "task_id": "t2",
                "depends_on": [],
                "writes": [],
                "touches": [],
                "verification_commands": [],
                "failure_policy": "fail_fast",
            },
        ],
    )
    seen_reports: list[str] = []

    def _runner(*_args, **kwargs):
        report_name = str(kwargs.get("report_name", "")).strip()
        seen_reports.append(report_name)
        if report_name == "runner_execution_report.plan.json":
            return {
                "status": "completed",
                "exit_code": 0,
                "report_name": report_name,
                "changed_paths": [],
            }
        if report_name == "runner_execution_report.t1.json":
            return {
                "status": "failed",
                "exit_code": 2,
                "report_name": report_name,
                "changed_paths": [],
            }
        if report_name == "runner_execution_report.t2.json":
            return {
                "status": "completed",
                "exit_code": 0,
                "report_name": report_name,
                "changed_paths": [],
            }
        raise AssertionError(f"unexpected report_name: {report_name}")

    monkeypatch.setattr(plan_execution, "_invoke_agent_runner", _runner)
    result = plan_execution.execute_implementation_plan_step(
        repo_root,
        state_path=state_path,
        state=state,
        run_agent_mode="policy",
        auto_mode=False,
    )
    assert result.agent_status == "failed"
    assert "runner_execution_report.t2.json" in seen_reports

    execution_state = json.loads(
        (iteration_dir / "plan_execution_state.json").read_text(encoding="utf-8")
    )
    assert execution_state["task_attempt_counts"]["t2"] == 1
    assert execution_state["task_status"]["t2"] == "completed"
