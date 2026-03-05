from __future__ import annotations

import json
import shutil
from pathlib import Path

import autolab.commands as commands_module
import pytest
import yaml

from autolab.traceability import build_traceability_coverage


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAFFOLD_DIR = _REPO_ROOT / "src" / "autolab" / "scaffold" / ".autolab"


def _copy_scaffold(repo: Path) -> None:
    shutil.copytree(_SCAFFOLD_DIR, repo / ".autolab", dirs_exist_ok=True)


def _write_state(
    repo: Path,
    *,
    stage: str,
    iteration_id: str = "iter1",
    experiment_id: str = "e1",
    last_run_id: str = "run_001",
) -> dict[str, object]:
    payload: dict[str, object] = {
        "iteration_id": iteration_id,
        "experiment_id": experiment_id,
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": last_run_id,
        "pending_run_id": "",
        "sync_status": "completed",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
        "assistant_mode": "off",
        "current_task_id": "",
        "task_cycle_stage": "select",
        "repeat_guard": {
            "last_decision": "",
            "same_decision_streak": 0,
            "last_open_task_count": -1,
            "no_progress_decisions": 0,
            "update_docs_cycle_count": 0,
            "last_verification_passed": True,
        },
        "task_change_baseline": {},
    }
    path = repo / ".autolab" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return payload


def _write_backlog(
    repo: Path,
    *,
    iteration_id: str = "iter1",
    experiment_id: str = "e1",
    hypothesis_id: str = "h1",
) -> None:
    backlog = {
        "hypotheses": [
            {
                "id": hypothesis_id,
                "status": "open",
                "title": "Traceability hypothesis",
                "success_metric": "accuracy",
                "target_delta": 0.1,
            }
        ],
        "experiments": [
            {
                "id": experiment_id,
                "hypothesis_id": hypothesis_id,
                "status": "open",
                "iteration_id": iteration_id,
                "type": "plan",
            }
        ],
    }
    (repo / ".autolab" / "backlog.yaml").write_text(
        yaml.safe_dump(backlog, sort_keys=False),
        encoding="utf-8",
    )


def _write_iteration_core_files(
    repo: Path,
    *,
    iteration_id: str = "iter1",
    run_id: str = "run_001",
    requirements: list[dict[str, object]] | None = None,
    tasks: list[dict[str, object]] | None = None,
    task_details: list[dict[str, object]] | None = None,
    include_metrics: bool = True,
    metrics_status: str = "completed",
) -> Path:
    iteration_dir = repo / "experiments" / "plan" / iteration_id
    (iteration_dir / "runs" / run_id).mkdir(parents=True, exist_ok=True)

    (iteration_dir / "hypothesis.md").write_text(
        "\n".join(
            [
                "# Hypothesis Statement",
                "",
                "Applying augmentation schedule v2 should improve validation accuracy.",
                "",
                "## Structured Metadata (machine-parsed)",
                "- target_delta: +0.1",
                "- metric_name: accuracy",
                "- metric_mode: maximize",
            ]
        ),
        encoding="utf-8",
    )

    if requirements is None:
        requirements = [
            {
                "requirement_id": "R1",
                "description": "Implement augmentation schedule wiring.",
                "scope_kind": "experiment",
            }
        ]
    design_payload = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": iteration_id,
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
        "implementation_requirements": requirements,
        "extract_parser": {"kind": "command", "command": "echo extract"},
    }
    (iteration_dir / "design.yaml").write_text(
        yaml.safe_dump(design_payload, sort_keys=False),
        encoding="utf-8",
    )

    if tasks is None:
        tasks = [
            {
                "task_id": "T1",
                "objective": "Implement requirement R1",
                "scope_kind": "experiment",
                "depends_on": [],
                "reads": [],
                "writes": ["experiments/plan/iter1/implementation_plan.md"],
                "touches": ["experiments/plan/iter1/implementation_plan.md"],
                "conflict_group": "",
                "verification_commands": ["python -m pytest -q"],
                "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
                "failure_policy": "fail_fast",
                "can_run_in_parallel": False,
                "covers_requirements": ["R1"],
            }
        ]
    plan_contract_payload = {
        "schema_version": "1.0",
        "iteration_id": iteration_id,
        "stage": "implementation",
        "generated_at": "2026-01-01T00:00:00Z",
        "tasks": tasks,
    }
    (iteration_dir / "plan_contract.json").write_text(
        json.dumps(plan_contract_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    if task_details is None:
        task_details = [
            {
                "task_id": "T1",
                "status": "completed",
                "wave": 1,
                "attempts": 1,
                "retries_used": 0,
                "last_error": "",
                "scope_kind": "experiment",
                "files_changed": ["experiments/plan/iter1/implementation_plan.md"],
            }
        ]
    summary_payload = {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00Z",
        "stage": "implementation",
        "iteration_id": iteration_id,
        "plan_file": f"experiments/plan/{iteration_id}/implementation_plan.md",
        "contract_hash": "abc",
        "run_unit": "wave",
        "tasks_total": len(task_details),
        "tasks_completed": sum(
            1
            for row in task_details
            if str(row.get("status", "")).strip() == "completed"
        ),
        "tasks_failed": sum(
            1 for row in task_details if str(row.get("status", "")).strip() == "failed"
        ),
        "tasks_blocked": sum(
            1 for row in task_details if str(row.get("status", "")).strip() == "blocked"
        ),
        "tasks_pending": sum(
            1 for row in task_details if str(row.get("status", "")).strip() == "pending"
        ),
        "waves_total": 1,
        "waves_executed": 1,
        "wave_details": [
            {"wave": 1, "status": "completed", "attempts": 0, "tasks": []}
        ],
        "task_details": task_details,
    }
    (iteration_dir / "plan_execution_summary.json").write_text(
        json.dumps(summary_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    if include_metrics:
        metrics_payload = {
            "schema_version": "1.0",
            "iteration_id": iteration_id,
            "run_id": run_id,
            "status": metrics_status,
            "primary_metric": {
                "name": "accuracy",
                "value": 0.91 if metrics_status == "completed" else None,
                "delta_vs_baseline": 0.11 if metrics_status == "completed" else None,
            },
        }
        (iteration_dir / "runs" / run_id / "metrics.json").write_text(
            json.dumps(metrics_payload, indent=2) + "\n",
            encoding="utf-8",
        )

    (iteration_dir / "decision_result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "decision": "design",
                "rationale": "Continue iteration.",
                "evidence": [
                    {
                        "source": "metrics",
                        "pointer": f"runs/{run_id}/metrics.json",
                        "summary": "Delta available for review.",
                    }
                ],
                "risks": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (repo / ".autolab" / "verification_result.json").write_text(
        json.dumps(
            {
                "generated_at": "2026-01-01T00:00:00Z",
                "iteration_id": iteration_id,
                "experiment_id": "e1",
                "state_stage": "decide_repeat",
                "stage_requested": "decide_repeat",
                "stage_effective": "decide_repeat",
                "passed": True,
                "message": "verification passed",
                "details": {"commands": []},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    return iteration_dir


def test_build_traceability_coverage_happy_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="decide_repeat")
    _write_backlog(repo)
    iteration_dir = _write_iteration_core_files(repo)

    result = build_traceability_coverage(repo, state, write_outputs=True)

    assert result.coverage_path == iteration_dir / "traceability_coverage.json"
    assert result.coverage_path.exists()
    assert result.latest_path == repo / ".autolab" / "traceability_latest.json"
    assert result.latest_path.exists()

    payload = json.loads(result.coverage_path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "1.0"
    assert payload["claim"]["claim_id"] == "C1"
    assert payload["summary"]["rows_total"] == 1
    assert payload["summary"]["rows_covered"] == 1
    row = payload["links"][0]
    assert row["coverage_status"] == "covered"
    assert row["failure_class"] == "none"
    assert row["decision"]["decision_status"] == "linked"


def test_build_traceability_coverage_verifier_failure_prevents_covered(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="decide_repeat")
    _write_backlog(repo)
    _write_iteration_core_files(repo)
    verification_path = repo / ".autolab" / "verification_result.json"
    verification_payload = json.loads(verification_path.read_text(encoding="utf-8"))
    verification_payload["passed"] = False
    verification_payload["message"] = "verification failed"
    verification_path.write_text(
        json.dumps(verification_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    result = build_traceability_coverage(repo, state, write_outputs=True)
    payload = json.loads(result.coverage_path.read_text(encoding="utf-8"))
    row = payload["links"][0]

    assert row["verification"]["stage_verifier_passed"] is False
    assert row["coverage_status"] == "failed"
    assert row["failure_class"] == "execution"


def test_build_traceability_coverage_decision_non_match_is_unlinked(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="decide_repeat")
    _write_backlog(repo)
    iteration_dir = _write_iteration_core_files(repo)
    decision_path = iteration_dir / "decision_result.json"
    decision_payload = json.loads(decision_path.read_text(encoding="utf-8"))
    decision_payload["evidence"] = [
        {
            "source": "manual",
            "pointer": "analysis/unrelated.txt",
            "summary": "Reviewed R10 and task T10 only.",
        }
    ]
    decision_path.write_text(
        json.dumps(decision_payload, indent=2) + "\n",
        encoding="utf-8",
    )

    result = build_traceability_coverage(repo, state, write_outputs=True)
    payload = json.loads(result.coverage_path.read_text(encoding="utf-8"))
    row = payload["links"][0]

    assert row["decision"]["decision_status"] == "unlinked"
    assert row["decision"]["matched_evidence_count"] == 0


def test_build_traceability_coverage_classifies_design_execution_measurement_gaps(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="decide_repeat")
    _write_backlog(repo)

    requirements = [
        {
            "requirement_id": "R1",
            "description": "Design-only requirement with no mapped task.",
            "scope_kind": "experiment",
        },
        {
            "requirement_id": "R2",
            "description": "Execution requirement with pending task.",
            "scope_kind": "experiment",
        },
        {
            "requirement_id": "R3",
            "description": "Measurement requirement with completed task.",
            "scope_kind": "experiment",
        },
    ]
    tasks = [
        {
            "task_id": "T2",
            "objective": "Implement R2",
            "scope_kind": "experiment",
            "depends_on": [],
            "reads": [],
            "writes": ["experiments/plan/iter1/file_r2.txt"],
            "touches": ["experiments/plan/iter1/file_r2.txt"],
            "conflict_group": "",
            "verification_commands": [],
            "manual_only_rationale": "manual check",
            "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            "failure_policy": "fail_fast",
            "can_run_in_parallel": False,
            "covers_requirements": ["R2"],
        },
        {
            "task_id": "T3",
            "objective": "Implement R3",
            "scope_kind": "experiment",
            "depends_on": [],
            "reads": [],
            "writes": ["experiments/plan/iter1/file_r3.txt"],
            "touches": ["experiments/plan/iter1/file_r3.txt"],
            "conflict_group": "",
            "verification_commands": [],
            "manual_only_rationale": "manual check",
            "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            "failure_policy": "fail_fast",
            "can_run_in_parallel": False,
            "covers_requirements": ["R3"],
        },
    ]
    task_details = [
        {"task_id": "T2", "status": "pending", "wave": 1, "files_changed": []},
        {"task_id": "T3", "status": "completed", "wave": 1, "files_changed": []},
    ]
    _write_iteration_core_files(
        repo,
        requirements=requirements,
        tasks=tasks,
        task_details=task_details,
        include_metrics=False,
    )

    result = build_traceability_coverage(repo, state, write_outputs=True)
    payload = json.loads(result.coverage_path.read_text(encoding="utf-8"))
    rows = {
        row["requirement_id"]: row
        for row in payload["links"]
        if row["task_id"] in {"", "T2", "T3"}
    }

    assert rows["R1"]["coverage_status"] == "failed"
    assert rows["R1"]["failure_class"] == "design"

    assert rows["R2"]["coverage_status"] == "untested"
    assert rows["R2"]["failure_class"] == "execution"

    assert rows["R3"]["coverage_status"] == "failed"
    assert rows["R3"]["failure_class"] == "measurement"

    summary = payload["summary"]
    assert summary["requirements_total"] == 3
    assert summary["requirements_covered"] == 0
    assert summary["requirements_untested"] == 1
    assert summary["requirements_failed"] == 2


def test_build_traceability_coverage_classifies_failed_task_as_execution(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="decide_repeat")
    _write_backlog(repo)
    task_details = [
        {
            "task_id": "T1",
            "status": "failed",
            "wave": 1,
            "attempts": 1,
            "retries_used": 0,
            "last_error": "unit tests failed",
            "files_changed": [],
        }
    ]
    _write_iteration_core_files(repo, task_details=task_details)

    result = build_traceability_coverage(repo, state, write_outputs=True)
    payload = json.loads(result.coverage_path.read_text(encoding="utf-8"))
    row = payload["links"][0]

    assert row["coverage_status"] == "failed"
    assert row["failure_class"] == "execution"
    assert "task execution failed" in row["failure_reason"]


def test_build_traceability_coverage_classifies_non_completed_metrics_status(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="decide_repeat")
    _write_backlog(repo)
    _write_iteration_core_files(repo, metrics_status="partial")

    result = build_traceability_coverage(repo, state, write_outputs=True)
    payload = json.loads(result.coverage_path.read_text(encoding="utf-8"))
    row = payload["links"][0]

    assert row["coverage_status"] == "failed"
    assert row["failure_class"] == "measurement"
    assert "metrics status" in row["failure_reason"]


@pytest.mark.parametrize("metric_value", [None, "not-a-number"])
def test_build_traceability_coverage_classifies_bad_metric_value_as_measurement_failure(
    tmp_path: Path,
    metric_value: object,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="decide_repeat")
    _write_backlog(repo)
    iteration_dir = _write_iteration_core_files(repo, metrics_status="completed")
    metrics_path = iteration_dir / "runs" / "run_001" / "metrics.json"
    metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    metrics_payload["primary_metric"]["value"] = metric_value
    metrics_path.write_text(
        json.dumps(metrics_payload, indent=2) + "\n", encoding="utf-8"
    )

    result = build_traceability_coverage(repo, state, write_outputs=True)
    payload = json.loads(result.coverage_path.read_text(encoding="utf-8"))
    row = payload["links"][0]

    assert row["coverage_status"] == "failed"
    assert row["failure_class"] == "measurement"
    assert "primary metric value" in row["failure_reason"]


def test_build_traceability_coverage_raises_on_empty_iteration_id(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="decide_repeat", iteration_id="")

    with pytest.raises(ValueError, match="state\\.iteration_id"):
        build_traceability_coverage(repo, state, write_outputs=True)


def test_build_traceability_coverage_write_outputs_false_is_read_only(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state = _write_state(repo, stage="decide_repeat")
    _write_backlog(repo)
    iteration_dir = _write_iteration_core_files(repo)

    result = build_traceability_coverage(repo, state, write_outputs=False)

    assert result.coverage_path == iteration_dir / "traceability_coverage.json"
    assert not result.coverage_path.exists()
    assert not result.latest_path.exists()


def test_decide_repeat_auto_writes_traceability_artifacts(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_backlog(repo)
    state_path = repo / ".autolab" / "state.json"
    _write_state(repo, stage="decide_repeat")
    _write_iteration_core_files(repo)

    exit_code = commands_module.main(
        ["run", "--state-file", str(state_path), "--decision", "design"]
    )
    assert exit_code == 0

    iteration_trace = (
        repo / "experiments" / "plan" / "iter1" / "traceability_coverage.json"
    )
    latest_trace = repo / ".autolab" / "traceability_latest.json"
    assert iteration_trace.exists()
    assert latest_trace.exists()

    trace_payload = json.loads(iteration_trace.read_text(encoding="utf-8"))
    assert trace_payload["summary"]["rows_total"] >= 1


def test_decide_repeat_manual_decision_rewrites_decision_result_and_links_traceability(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_backlog(repo)
    state_path = repo / ".autolab" / "state.json"
    _write_state(repo, stage="decide_repeat")
    iteration_dir = _write_iteration_core_files(repo)
    decision_result_path = iteration_dir / "decision_result.json"
    decision_result_path.unlink()

    exit_code = commands_module.main(
        ["run", "--state-file", str(state_path), "--decision", "design"]
    )
    assert exit_code == 0

    assert decision_result_path.exists()
    decision_payload = json.loads(decision_result_path.read_text(encoding="utf-8"))
    assert decision_payload["decision"] == "design"
    evidence = decision_payload["evidence"]
    assert isinstance(evidence, list) and evidence
    assert any("requirement_id=R1" in row.get("summary", "") for row in evidence)
    assert any("task_id=T1" in row.get("summary", "") for row in evidence)

    trace_payload = json.loads(
        (iteration_dir / "traceability_coverage.json").read_text(encoding="utf-8")
    )
    row = trace_payload["links"][0]
    assert row["decision"]["decision_status"] == "linked"


def test_decide_repeat_succeeds_when_traceability_generation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_backlog(repo)
    state_path = repo / ".autolab" / "state.json"
    _write_state(repo, stage="decide_repeat")
    _write_iteration_core_files(repo)

    import autolab.run_standard as run_standard_module

    def _boom(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("boom")

    monkeypatch.setattr(run_standard_module, "build_traceability_coverage", _boom)

    exit_code = commands_module.main(
        ["run", "--state-file", str(state_path), "--decision", "design"]
    )
    assert exit_code == 0

    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_payload["stage"] == "design"
    agent_result_path = repo / ".autolab" / "agent_result.json"
    agent_result_payload = json.loads(agent_result_path.read_text(encoding="utf-8"))
    assert "warning: traceability generation failed" in str(
        agent_result_payload.get("summary", "")
    )


def test_trace_command_regenerates_and_supports_json_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_backlog(repo)
    state_path = repo / ".autolab" / "state.json"
    _write_state(repo, stage="design")
    _write_iteration_core_files(repo)

    exit_code = commands_module.main(
        ["trace", "--state-file", str(state_path), "--json"]
    )
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["status"] == "ok"
    assert not Path(payload["coverage_path"]).is_absolute()
    assert not Path(payload["latest_path"]).is_absolute()
    assert (repo / payload["coverage_path"]).exists()
    assert (repo / payload["latest_path"]).exists()


def test_trace_command_non_json_output(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_backlog(repo)
    state_path = repo / ".autolab" / "state.json"
    _write_state(repo, stage="design")
    _write_iteration_core_files(repo)

    exit_code = commands_module.main(["trace", "--state-file", str(state_path)])
    assert exit_code == 0

    captured = capsys.readouterr()
    assert "autolab trace" in captured.out
    assert "rows: total=" in captured.out
    assert "requirements: total=" in captured.out


def test_trace_command_supports_iteration_override(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_backlog(repo, iteration_id="iter1")
    state_path = repo / ".autolab" / "state.json"
    _write_state(repo, stage="design", iteration_id="iter1")
    _write_iteration_core_files(repo, iteration_id="iter1")
    _write_iteration_core_files(repo, iteration_id="iter2")

    exit_code = commands_module.main(
        [
            "trace",
            "--state-file",
            str(state_path),
            "--iteration-id",
            "iter2",
            "--json",
        ]
    )
    assert exit_code == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["iteration_id"] == "iter2"
    assert payload["coverage_path"].endswith("iter2/traceability_coverage.json")
