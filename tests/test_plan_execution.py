from __future__ import annotations

import json
import subprocess
from pathlib import Path

from autolab import plan_execution
from autolab.models import PlanExecutionConfig, PlanExecutionImplementationConfig
from autolab.wave_observability import build_wave_observability


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
    (autolab_dir / "plan_check_result.json").write_text(
        json.dumps({"schema_version": "1.0", "errors": []}),
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
        experiment_id="exp1",
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
        experiment_id="exp1",
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
        experiment_id="exp1",
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


def test_execute_task_passes_compact_sidecar_context_to_runner(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def _capture_runner(*_args, **kwargs):
        captured["task_context"] = kwargs.get("task_context")
        return {
            "status": "completed",
            "exit_code": 0,
            "changed_paths": [],
        }

    monkeypatch.setattr(plan_execution, "_invoke_agent_runner", _capture_runner)

    repo_root = tmp_path
    context_dir = repo_root / ".autolab" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    iteration_dir = repo_root / "experiments" / "plan" / "iter1"
    sidecar_dir = iteration_dir / "context" / "sidecars"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "sidecars" / "project_wide").mkdir(parents=True, exist_ok=True)
    (context_dir / "project_map.json").write_text(
        json.dumps({"schema_version": "1.0", "repo_root": str(repo_root)}),
        encoding="utf-8",
    )
    (context_dir / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "focus_iteration_id": "iter1",
                "focus_experiment_id": "exp1",
                "project_map_path": ".autolab/context/project_map.json",
                "selected_experiment_delta_path": "experiments/plan/iter1/context_delta.json",
            }
        ),
        encoding="utf-8",
    )
    (iteration_dir / "context_delta.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter1",
                "experiment_id": "exp1",
                "changed_paths": ["src/model.py"],
            }
        ),
        encoding="utf-8",
    )
    (context_dir / "sidecars" / "project_wide" / "research.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sidecar_kind": "research",
                "scope_kind": "project_wide",
                "scope_root": str(repo_root.resolve()),
                "generated_at": "2026-03-05T00:00:00Z",
                "derived_from": [],
                "stale_if": [],
                "questions": [],
                "findings": [
                    {
                        "id": "pw-research",
                        "summary": "Project-wide research summary",
                        "detail": "Use deterministic parser outputs.",
                    }
                ],
                "recommendations": [],
                "sources": [],
            }
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "discuss.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sidecar_kind": "discuss",
                "scope_kind": "experiment",
                "scope_root": str(iteration_dir.resolve()),
                "iteration_id": "iter1",
                "experiment_id": "exp1",
                "generated_at": "2026-03-05T00:00:00Z",
                "derived_from": [],
                "stale_if": [],
                "locked_decisions": [],
                "preferences": [
                    {
                        "id": "exp-pref",
                        "summary": "Keep the experiment patch narrow",
                        "detail": "Do not expand the scope of the implementation task.",
                    }
                ],
                "constraints": [],
                "open_questions": [],
                "promotion_candidates": [],
            }
        ),
        encoding="utf-8",
    )
    (iteration_dir / "design.yaml").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "id": "exp1",
                "iteration_id": "iter1",
                "hypothesis_id": "h1",
                "entrypoint": {"module": "pkg.train", "args": {}},
                "compute": {"location": "local", "gpu_count": 0},
                "metrics": {
                    "primary": {"name": "accuracy", "unit": "%", "mode": "maximize"},
                    "secondary": [],
                    "success_delta": "+0.1",
                    "aggregation": "mean",
                    "baseline_comparison": "vs baseline",
                },
                "baselines": [{"name": "baseline", "description": "existing"}],
                "implementation_requirements": [
                    {
                        "requirement_id": "R1",
                        "description": "Use promoted research context",
                        "scope_kind": "experiment",
                        "context_refs": ["project_wide:research:findings:pw-research"],
                    }
                ],
                "extract_parser": {
                    "kind": "command",
                    "command": "python -m tools.extract_results --run-id {run_id}",
                },
            }
        ),
        encoding="utf-8",
    )

    result = plan_execution._execute_task(
        repo_root,
        state_path=repo_root / ".autolab" / "state.json",
        iteration_id="iter1",
        experiment_id="exp1",
        iteration_path="experiments/plan/iter1",
        iteration_dir=iteration_dir,
        project_wide_root=repo_root,
        wave=1,
        task={
            "task_id": "t1",
            "verification_commands": [],
            "context_inputs": [
                "project_wide:research:findings:pw-research",
                "experiment:discuss:preferences:exp-pref",
            ],
        },
        task_retry_max=0,
        require_verification_commands=False,
    )

    assert result["status"] == "completed"
    task_context = captured["task_context"]
    assert isinstance(task_context, dict)
    sidecar_context = task_context["sidecar_context"]
    assert sidecar_context["context_inputs"] == [
        "project_wide:research:findings:pw-research",
        "experiment:discuss:preferences:exp-pref",
    ]
    assert (
        "project_wide:research:findings:pw-research: Project-wide research summary"
        in sidecar_context["resolved_inputs"]
    )
    assert (
        "experiment:discuss:preferences:exp-pref: Keep the experiment patch narrow"
        in sidecar_context["resolved_inputs"]
    )


def test_execute_task_does_not_infer_promoted_context_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    captured: dict[str, object] = {}

    def _capture_runner(*_args, **kwargs):
        captured["task_context"] = kwargs.get("task_context")
        return {
            "status": "completed",
            "exit_code": 0,
            "changed_paths": [],
        }

    monkeypatch.setattr(plan_execution, "_invoke_agent_runner", _capture_runner)

    repo_root = tmp_path
    context_dir = repo_root / ".autolab" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    iteration_dir = repo_root / "experiments" / "plan" / "iter1"
    sidecar_dir = iteration_dir / "context" / "sidecars"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "project_map.json").write_text(
        json.dumps({"schema_version": "1.0", "repo_root": str(repo_root)}),
        encoding="utf-8",
    )
    (context_dir / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "focus_iteration_id": "iter1",
                "focus_experiment_id": "exp1",
                "project_map_path": ".autolab/context/project_map.json",
                "selected_experiment_delta_path": "experiments/plan/iter1/context_delta.json",
            }
        ),
        encoding="utf-8",
    )
    (iteration_dir / "context_delta.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter1",
                "experiment_id": "exp1",
                "changed_paths": ["src/shared.py"],
            }
        ),
        encoding="utf-8",
    )
    (sidecar_dir / "discuss.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sidecar_kind": "discuss",
                "scope_kind": "experiment",
                "scope_root": str(iteration_dir.resolve()),
                "iteration_id": "iter1",
                "experiment_id": "exp1",
                "generated_at": "2026-03-05T00:00:00Z",
                "derived_from": [],
                "stale_if": [],
                "locked_decisions": [],
                "preferences": [
                    {
                        "id": "exp-pref",
                        "summary": "Keep the experiment patch narrow",
                        "detail": "Do not expand the scope of the implementation task.",
                    }
                ],
                "constraints": [],
                "open_questions": [],
                "promotion_candidates": [],
            }
        ),
        encoding="utf-8",
    )
    (iteration_dir / "design.yaml").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "id": "exp1",
                "iteration_id": "iter1",
                "hypothesis_id": "h1",
                "entrypoint": {"module": "pkg.train", "args": {}},
                "compute": {"location": "local", "gpu_count": 0},
                "metrics": {
                    "primary": {"name": "accuracy", "unit": "%", "mode": "maximize"},
                    "secondary": [],
                    "success_delta": "+0.1",
                    "aggregation": "mean",
                    "baseline_comparison": "vs baseline",
                },
                "baselines": [{"name": "baseline", "description": "existing"}],
                "implementation_requirements": [
                    {
                        "requirement_id": "R_shared",
                        "description": "Promote the experiment preference only through declared plan refs.",
                        "scope_kind": "project_wide",
                        "promoted_constraints": [
                            {
                                "id": "pc1",
                                "source_ref": "experiment:discuss:preferences:exp-pref",
                                "summary": "Carry the experiment preference into the shared code path.",
                                "rationale": "Shared work must only consume this when the plan contract declares it.",
                            }
                        ],
                    }
                ],
                "extract_parser": {
                    "kind": "command",
                    "command": "python -m tools.extract_results --run-id {run_id}",
                },
            }
        ),
        encoding="utf-8",
    )

    result = plan_execution._execute_task(
        repo_root,
        state_path=repo_root / ".autolab" / "state.json",
        iteration_id="iter1",
        experiment_id="exp1",
        iteration_path="experiments/plan/iter1",
        iteration_dir=iteration_dir,
        project_wide_root=repo_root,
        wave=1,
        task={
            "task_id": "t1",
            "scope_kind": "project_wide",
            "covers_requirements": ["R_shared"],
            "verification_commands": [],
        },
        task_retry_max=0,
        require_verification_commands=False,
    )

    assert result["status"] == "completed"
    task_context = captured["task_context"]
    assert isinstance(task_context, dict)
    sidecar_context = task_context["sidecar_context"]
    assert sidecar_context["context_inputs"] == []
    assert sidecar_context["resolved_inputs"] == []


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
        task={"expected_artifacts": ["out.txt"], "scope_kind": "project_wide"},
    )
    assert missing == []


def test_expected_artifacts_reject_project_wide_files_outside_scope_root(
    tmp_path: Path,
) -> None:
    scope_root = tmp_path / "src"
    scope_root.mkdir(parents=True)
    iteration_dir = tmp_path / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True)
    (tmp_path / "out.txt").write_text("outside\n", encoding="utf-8")

    missing = plan_execution._task_expected_artifacts_missing(
        repo_root=tmp_path,
        iteration_dir=iteration_dir,
        scope_root=scope_root,
        task={"expected_artifacts": ["out.txt"], "scope_kind": "project_wide"},
    )
    assert missing == ["out.txt"]


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
    assert execution_state["task_reason_code"]["t1"] == "runner_failed"
    assert execution_state["task_reason_code"]["t2"] == "fail_fast_skipped"
    assert execution_state["wave_attempt_history"]["1"][0]["status"] == "failed"

    execution_summary = json.loads(
        (iteration_dir / "plan_execution_summary.json").read_text(encoding="utf-8")
    )
    task_details = {row["task_id"]: row for row in execution_summary["task_details"]}
    assert execution_summary["tasks_skipped"] == 1
    assert execution_summary["observability_summary"]["tasks_skipped"] == 1
    assert task_details["t2"]["reason_code"] == "fail_fast_skipped"
    assert execution_summary["wave_details"][0]["retry_pending"] is False


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
    assert execution_state["wave_attempt_history"]["1"][0]["status"] == "failed"

    execution_summary = json.loads(
        (iteration_dir / "plan_execution_summary.json").read_text(encoding="utf-8")
    )
    assert execution_summary["tasks_completed"] == 1
    assert execution_summary["tasks_failed"] == 1
    assert execution_summary["wave_details"][0]["failed_task_ids"] == ["t1"]
    assert execution_summary["wave_details"][0]["completed_task_ids"] == ["t2"]


def test_blocked_wave_records_blocked_tasks_and_structural_critical_path(
    monkeypatch, tmp_path: Path
) -> None:
    config = _plan_config(task_retry_max=0, wave_retry_max=0)
    _patch_common_plan_execution(monkeypatch, config)
    repo_root, state_path, state, iteration_dir = _seed_plan_files(
        tmp_path,
        tasks=[
            {
                "task_id": "t1",
                "depends_on": ["missing_dependency"],
                "writes": [],
                "touches": [],
                "verification_commands": [],
                "failure_policy": "fail_fast",
            }
        ],
    )

    monkeypatch.setattr(
        plan_execution,
        "_invoke_agent_runner",
        lambda *_args, **_kwargs: {
            "status": "completed",
            "exit_code": 0,
            "report_name": "runner_execution_report.plan.json",
            "changed_paths": [],
        },
    )

    result = plan_execution.execute_implementation_plan_step(
        repo_root,
        state_path=state_path,
        state=state,
        run_agent_mode="policy",
        auto_mode=False,
    )

    assert result.agent_status == "failed"
    assert "blocked by unresolved dependencies" in result.summary

    execution_state = json.loads(
        (iteration_dir / "plan_execution_state.json").read_text(encoding="utf-8")
    )
    assert execution_state["task_status"]["t1"] == "blocked"
    assert execution_state["task_reason_code"]["t1"] == "dependency_blocked"
    assert execution_state["task_blocked_by"]["t1"] == ["missing_dependency"]

    execution_summary = json.loads(
        (iteration_dir / "plan_execution_summary.json").read_text(encoding="utf-8")
    )
    assert execution_summary["tasks_blocked"] == 1
    assert execution_summary["critical_path"]["mode"] == "structural"
    assert execution_summary["wave_details"][0]["blocked_task_ids"] == ["t1"]


def test_wave_observability_critical_path_respects_wave_barriers(
    tmp_path: Path,
) -> None:
    iteration_dir = tmp_path / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    observability = build_wave_observability(
        tmp_path,
        iteration_dir=iteration_dir,
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
                {
                    "task_id": "t3",
                    "scope_kind": "experiment",
                    "depth": 1,
                    "can_run_in_parallel": True,
                    "conflict_group": "",
                },
            ],
            "edges": [{"from": "t1", "to": "t3"}],
            "waves": [
                {"wave": 1, "tasks": ["t1", "t2"]},
                {"wave": 2, "tasks": ["t3"]},
            ],
        },
        plan_check_payload={
            "schema_version": "1.0",
            "iteration_id": "iter1",
            "errors": [],
        },
        execution_state_payload={"iteration_id": "iter1"},
        execution_summary_payload={
            "iteration_id": "iter1",
            "wave_details": [
                {
                    "wave": 1,
                    "status": "completed",
                    "tasks": ["t1", "t2"],
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:00:10Z",
                    "duration_seconds": 10.0,
                },
                {
                    "wave": 2,
                    "status": "completed",
                    "tasks": ["t3"],
                    "started_at": "2026-01-01T00:00:10Z",
                    "completed_at": "2026-01-01T00:00:11Z",
                    "duration_seconds": 1.0,
                },
            ],
            "task_details": [
                {
                    "task_id": "t1",
                    "status": "completed",
                    "wave": 1,
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:00:01Z",
                    "duration_seconds": 1.0,
                },
                {
                    "task_id": "t2",
                    "status": "completed",
                    "wave": 1,
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:00:10Z",
                    "duration_seconds": 10.0,
                },
                {
                    "task_id": "t3",
                    "status": "completed",
                    "wave": 2,
                    "started_at": "2026-01-01T00:00:10Z",
                    "completed_at": "2026-01-01T00:00:11Z",
                    "duration_seconds": 1.0,
                },
            ],
        },
    )

    assert observability["critical_path"]["mode"] == "measured_complete"
    assert observability["critical_path"]["task_ids"] == ["t2", "t3"]
    assert observability["critical_path"]["wave_ids"] == [1, 2]
    assert observability["critical_path"]["duration_seconds"] == 11.0
    assert "wave-barrier" in observability["critical_path"]["basis_note"]


def test_wave_retry_pending_tracks_current_and_historical_state(
    monkeypatch, tmp_path: Path
) -> None:
    config = _plan_config(task_retry_max=0, wave_retry_max=1)
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
            }
        ],
    )
    task_attempts = {"t1": 0}

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
            task_attempts["t1"] += 1
            if task_attempts["t1"] == 1:
                return {
                    "status": "failed",
                    "exit_code": 2,
                    "report_name": report_name,
                    "changed_paths": [],
                }
            return {
                "status": "completed",
                "exit_code": 0,
                "report_name": report_name,
                "changed_paths": [],
            }
        raise AssertionError(f"unexpected report_name: {report_name}")

    monkeypatch.setattr(plan_execution, "_invoke_agent_runner", _runner)

    first = plan_execution.execute_implementation_plan_step(
        repo_root,
        state_path=state_path,
        state=state,
        run_agent_mode="policy",
        auto_mode=False,
    )
    assert first.agent_status == "needs_retry"

    first_state = json.loads(
        (iteration_dir / "plan_execution_state.json").read_text(encoding="utf-8")
    )
    first_summary = json.loads(
        (iteration_dir / "plan_execution_summary.json").read_text(encoding="utf-8")
    )
    assert first_state["wave_retry_counts"]["1"] == 0
    assert first_summary["wave_details"][0]["retries_used"] == 0
    assert first_summary["wave_details"][0]["retry_pending"] is True
    assert first_summary["wave_details"][0]["current_retry_reasons"] == [
        "runner_failed"
    ]
    assert first_summary["wave_details"][0]["retry_reasons"] == ["runner_failed"]
    assert first_summary["observability_summary"]["retrying_waves"] == 1

    second = plan_execution.execute_implementation_plan_step(
        repo_root,
        state_path=state_path,
        state=state,
        run_agent_mode="policy",
        auto_mode=False,
    )
    assert second.agent_status == "complete"

    second_state = json.loads(
        (iteration_dir / "plan_execution_state.json").read_text(encoding="utf-8")
    )
    second_summary = json.loads(
        (iteration_dir / "plan_execution_summary.json").read_text(encoding="utf-8")
    )
    assert second_state["wave_retry_counts"]["1"] == 1
    assert second_summary["wave_details"][0]["retries_used"] == 1
    assert second_summary["wave_details"][0]["retry_pending"] is False
    assert second_summary["wave_details"][0]["current_retry_reasons"] == []
    assert second_summary["wave_details"][0]["retry_reasons"] == ["runner_failed"]
    assert second_summary["observability_summary"]["retrying_waves"] == 0


def test_wave_retry_budget_exhausted_does_not_count_unscheduled_retry(
    monkeypatch, tmp_path: Path
) -> None:
    config = _plan_config(task_retry_max=0, wave_retry_max=0)
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
            }
        ],
    )

    monkeypatch.setattr(
        plan_execution,
        "_invoke_agent_runner",
        lambda *_args, **kwargs: {
            "status": "completed"
            if str(kwargs.get("report_name", "")).strip()
            == "runner_execution_report.plan.json"
            else "failed",
            "exit_code": 0
            if str(kwargs.get("report_name", "")).strip()
            == "runner_execution_report.plan.json"
            else 2,
            "report_name": str(kwargs.get("report_name", "")).strip(),
            "changed_paths": [],
        },
    )

    result = plan_execution.execute_implementation_plan_step(
        repo_root,
        state_path=state_path,
        state=state,
        run_agent_mode="policy",
        auto_mode=False,
    )

    assert result.agent_status == "failed"
    assert "(0/0)" in result.summary

    execution_state = json.loads(
        (iteration_dir / "plan_execution_state.json").read_text(encoding="utf-8")
    )
    execution_summary = json.loads(
        (iteration_dir / "plan_execution_summary.json").read_text(encoding="utf-8")
    )
    assert execution_state["wave_retry_counts"]["1"] == 0
    assert execution_summary["wave_details"][0]["retries_used"] == 0
    assert execution_summary["wave_details"][0]["retry_pending"] is False
    assert execution_summary["wave_details"][0]["current_retry_reasons"] == []
