from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

import autolab.commands as commands_module
import autolab.cli.handlers_admin as handlers_admin
import pytest
from autolab.update import UpdateResult


def _load_toml(path: Path) -> dict:
    payload: dict
    if sys.version_info >= (3, 11):
        import tomllib

        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    else:  # pragma: no cover
        import tomli  # type: ignore

        payload = tomli.loads(path.read_text(encoding="utf-8"))
    return payload


def _init_repo_state(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    return repo, state_path


def _write_discuss_research_context(
    repo: Path,
    state_path: Path,
    *,
    iteration_id: str = "iter_ctx",
    experiment_id: str = "e_ctx",
) -> Path:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["iteration_id"] = iteration_id
    state["experiment_id"] = experiment_id
    state["stage"] = "design"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    context_dir = repo / ".autolab" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    iteration_dir = repo / "experiments" / "plan" / iteration_id
    iteration_dir.mkdir(parents=True, exist_ok=True)

    (context_dir / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "focus_iteration_id": iteration_id,
                "focus_experiment_id": experiment_id,
                "project_map_path": ".autolab/context/project_map.json",
                "selected_experiment_delta_path": f"experiments/plan/{iteration_id}/context_delta.json",
                "experiment_delta_maps": [
                    {
                        "iteration_id": iteration_id,
                        "experiment_id": experiment_id,
                        "path": f"experiments/plan/{iteration_id}/context_delta.json",
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (context_dir / "project_map.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "scan_mode": "fast_heuristic",
                "repo_root": str(repo.resolve()),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (iteration_dir / "context_delta.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "iteration_id": iteration_id,
                "experiment_id": experiment_id,
                "changed_paths": ["src/model.py"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return iteration_dir


def _write_iteration_docs_fixture(repo: Path, *, iteration_id: str) -> None:
    iteration_dir = repo / "experiments" / "plan" / iteration_id
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "design.yaml").write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                "id: e_fixture",
                f"iteration_id: {iteration_id}",
                "hypothesis_id: h_fixture",
                "entrypoint:",
                "  module: pkg.train",
                "  args: {}",
                "compute:",
                "  location: local",
                "  gpu_count: 0",
                "metrics:",
                "  primary:",
                "    name: accuracy",
                "    unit: '%'",
                "    mode: maximize",
                "  secondary: []",
                "  success_delta: +0.1",
                "  aggregation: mean",
                "  baseline_comparison: vs baseline",
                "baselines:",
                "  - name: baseline",
                "    description: stable baseline",
                "implementation_requirements:",
                "  - requirement_id: R10",
                "    description: Wire parser-compatible metrics export.",
                "    scope_kind: experiment",
                "    expected_artifacts: [implementation_plan.md, plan_contract.json]",
                "extract_parser:",
                "  kind: command",
                '  command: "python -m tools.extract_results --run-id {run_id}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
    (iteration_dir / "plan_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": iteration_id,
                "stage": "implementation",
                "generated_at": "2026-01-01T00:00:00Z",
                "tasks": [
                    {
                        "task_id": "T10",
                        "objective": "Implement requirement R10",
                        "scope_kind": "experiment",
                        "depends_on": [],
                        "reads": [],
                        "writes": [
                            f"experiments/plan/{iteration_id}/implementation_plan.md"
                        ],
                        "touches": [
                            f"experiments/plan/{iteration_id}/implementation_plan.md"
                        ],
                        "verification_commands": [],
                        "expected_artifacts": [
                            "implementation_plan.md",
                            "plan_contract.json",
                        ],
                        "failure_policy": "fail_fast",
                        "can_run_in_parallel": False,
                        "covers_requirements": ["R10"],
                    }
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (iteration_dir / "traceability_coverage.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-01-01T00:00:00Z",
                "iteration_id": iteration_id,
                "experiment_id": "e_fixture",
                "run_id": "run_fixture",
                "claim": {
                    "claim_id": "C1",
                    "text": "fixture claim",
                    "status": "available",
                    "source_pointer": f"experiments/plan/{iteration_id}/hypothesis.md",
                },
                "decision": {
                    "status": "available",
                    "decision": "design",
                    "rationale": "continue",
                    "pointer": f"experiments/plan/{iteration_id}/decision_result.json",
                    "evidence_count": 1,
                },
                "links": [
                    {
                        "row_id": "C1:R10:T10",
                        "claim_id": "C1",
                        "requirement_id": "R10",
                        "task_id": "T10",
                        "coverage_status": "covered",
                        "failure_class": "none",
                        "failure_reason": "",
                    }
                ],
                "summary": {
                    "rows_total": 1,
                    "rows_covered": 1,
                    "rows_untested": 0,
                    "rows_failed": 0,
                    "requirements_total": 1,
                    "requirements_covered": 1,
                    "requirements_untested": 0,
                    "requirements_failed": 0,
                },
                "pointers": {},
                "diagnostics": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_wave_observability_fixture(
    repo: Path,
    state_path: Path,
    *,
    iteration_id: str = "iter_obs",
) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["iteration_id"] = iteration_id
    state["experiment_id"] = "e_fixture"
    state["stage"] = "implementation"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")

    _write_iteration_docs_fixture(repo, iteration_id=iteration_id)
    (repo / ".autolab" / "traceability_latest.json").write_text(
        json.dumps(
            {
                "iteration_id": iteration_id,
                "traceability_path": f"experiments/plan/{iteration_id}/traceability_coverage.json",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "backlog.yaml").write_text(
        "\n".join(
            [
                "hypotheses:",
                "  - id: h_fixture",
                "    status: open",
                "    title: Wave observability fixture",
                "    success_metric: accuracy",
                "    target_delta: 0.1",
                "experiments:",
                "  - id: e_fixture",
                "    hypothesis_id: h_fixture",
                "    status: in_progress",
                "    type: plan",
                f"    iteration_id: {iteration_id}",
                "",
            ]
        ),
        encoding="utf-8",
    )

    context_dir = repo / ".autolab" / "context"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "bundle.json").write_text(
        json.dumps(
            {
                "focus_iteration_id": iteration_id,
                "focus_experiment_id": "e_fixture",
                "project_map_path": ".autolab/context/project_map.json",
                "selected_experiment_delta_path": f"experiments/plan/{iteration_id}/context_delta.json",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (context_dir / "project_map.json").write_text(
        json.dumps(
            {
                "scan_mode": "full",
                "paths": ["src", f"experiments/plan/{iteration_id}"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    iteration_dir = repo / "experiments" / "plan" / iteration_id
    (iteration_dir / "context_delta.json").write_text(
        json.dumps(
            {
                "iteration_id": iteration_id,
                "experiment_id": "e_fixture",
                "changed_paths": ["src/model.py"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (repo / ".autolab" / "plan_graph.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {"task_id": "T1"},
                    {"task_id": "T2"},
                    {"task_id": "T3"},
                    {"task_id": "T4"},
                    {"task_id": "T5"},
                ],
                "edges": [
                    {"from": "T1", "to": "T2"},
                    {"from": "T2", "to": "T3"},
                    {"from": "T2", "to": "T4"},
                    {"from": "T2", "to": "T5"},
                ],
                "waves": [
                    {"wave": 1, "tasks": ["T1"]},
                    {"wave": 2, "tasks": ["T2", "T3", "T4", "T5"]},
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "plan_check_result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "errors": [
                    "same-wave write conflict: tasks T2 and T3 overlap in writes/touches"
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    execution_state = {
        "schema_version": "1.0",
        "generated_at": "2026-03-01T00:00:00Z",
        "updated_at": "2026-03-01T00:00:10Z",
        "stage": "implementation",
        "iteration_id": iteration_id,
        "contract_path": ".autolab/plan_contract.json",
        "contract_hash": "fixture-hash",
        "plan_file": f"experiments/plan/{iteration_id}/implementation_plan.md",
        "run_unit": "wave",
        "waves_total": 2,
        "task_status": {
            "T1": "completed",
            "T2": "failed",
            "T3": "pending",
            "T4": "blocked",
            "T5": "pending",
        },
        "task_attempt_counts": {"T1": 1, "T2": 2, "T3": 0, "T4": 0, "T5": 0},
        "task_retry_counts": {"T1": 0, "T2": 1, "T3": 0, "T4": 0, "T5": 0},
        "task_last_error": {
            "T1": "",
            "T2": "tests failed",
            "T3": "",
            "T4": "",
            "T5": "",
        },
        "task_files_changed": {
            "T1": [],
            "T2": ["src/model.py"],
            "T3": [],
            "T4": [],
            "T5": [],
        },
        "task_started_at": {
            "T1": "2026-03-01T00:00:00Z",
            "T2": "2026-03-01T00:00:12Z",
            "T3": "",
            "T4": "",
            "T5": "",
        },
        "task_completed_at": {
            "T1": "2026-03-01T00:00:12Z",
            "T2": "2026-03-01T00:00:19Z",
            "T3": "",
            "T4": "",
            "T5": "",
        },
        "task_duration_seconds": {
            "T1": 12.0,
            "T2": 7.0,
            "T3": 0.0,
            "T4": 0.0,
            "T5": 0.0,
        },
        "task_reason_code": {
            "T1": "completed",
            "T2": "verification_failed",
            "T3": "wave_retry_pending",
            "T4": "dependency_blocked",
            "T5": "fail_fast_skipped",
        },
        "task_reason_detail": {
            "T1": "",
            "T2": "tests failed",
            "T3": "waiting for retried wave",
            "T4": "waiting on dependency T2",
            "T5": "skipped after T2 failure",
        },
        "task_runner_report_path": {
            "T1": ".autolab/runner_execution_report.T1.json",
            "T2": ".autolab/runner_execution_report.T2.json",
            "T3": "",
            "T4": "",
            "T5": "",
        },
        "task_verification_status": {
            "T1": "passed",
            "T2": "failed",
            "T3": "not_run",
            "T4": "not_run",
            "T5": "not_run",
        },
        "task_verification_commands": {
            "T1": ["pytest -q tests/test_model.py"],
            "T2": ["pytest -q tests/test_model.py"],
            "T3": [],
            "T4": [],
            "T5": [],
        },
        "task_expected_artifacts_missing": {
            "T1": [],
            "T2": [],
            "T3": [],
            "T4": [],
            "T5": [],
        },
        "task_blocked_by": {
            "T1": [],
            "T2": [],
            "T3": ["T2"],
            "T4": ["T2"],
            "T5": ["T2"],
        },
        "wave_retry_counts": {"1": 0, "2": 1},
        "wave_status": {"1": "completed", "2": "failed"},
        "wave_started_at": {"1": "2026-03-01T00:00:00Z", "2": "2026-03-01T00:00:12Z"},
        "wave_completed_at": {
            "1": "2026-03-01T00:00:12Z",
            "2": "2026-03-01T00:00:19Z",
        },
        "wave_duration_seconds": {"1": 12.0, "2": 7.0},
        "wave_attempt_history": {
            "1": [
                {
                    "attempt": 1,
                    "status": "completed",
                    "started_at": "2026-03-01T00:00:00Z",
                    "completed_at": "2026-03-01T00:00:12Z",
                    "duration_seconds": 12.0,
                    "retry_reason": "",
                    "detail": "",
                }
            ],
            "2": [
                {
                    "attempt": 1,
                    "status": "failed",
                    "started_at": "2026-03-01T00:00:12Z",
                    "completed_at": "2026-03-01T00:00:19Z",
                    "duration_seconds": 7.0,
                    "retry_reason": "verification_failed",
                    "detail": "tests failed",
                }
            ],
        },
        "wave_retry_reasons": {"1": [], "2": ["verification_failed"]},
        "wave_out_of_contract_paths": {"1": [], "2": []},
        "current_wave": 2,
    }
    (iteration_dir / "plan_execution_state.json").write_text(
        json.dumps(execution_state, indent=2) + "\n",
        encoding="utf-8",
    )

    execution_summary = {
        "schema_version": "1.0",
        "generated_at": "2026-03-01T00:00:10Z",
        "stage": "implementation",
        "iteration_id": iteration_id,
        "plan_file": f"experiments/plan/{iteration_id}/implementation_plan.md",
        "contract_hash": "fixture-hash",
        "run_unit": "wave",
        "tasks_total": 5,
        "tasks_completed": 1,
        "tasks_failed": 1,
        "tasks_blocked": 1,
        "tasks_pending": 2,
        "tasks_skipped": 1,
        "tasks_deferred": 1,
        "waves_total": 2,
        "waves_executed": 2,
        "wave_details": [
            {
                "wave": 1,
                "status": "completed",
                "attempts": 1,
                "retries_used": 0,
                "tasks": ["T1"],
                "started_at": "2026-03-01T00:00:00Z",
                "completed_at": "2026-03-01T00:00:12Z",
                "duration_seconds": 12.0,
                "last_attempt_duration_seconds": 12.0,
                "timing_available": True,
                "attempt_history": execution_state["wave_attempt_history"]["1"],
                "retry_reasons": [],
                "out_of_contract_paths": [],
                "completed_task_ids": ["T1"],
                "failed_task_ids": [],
                "blocked_task_ids": [],
                "skipped_task_ids": [],
                "deferred_task_ids": [],
                "pending_task_ids": [],
                "retry_pending": False,
                "critical_path": False,
            },
            {
                "wave": 2,
                "status": "failed",
                "attempts": 1,
                "retries_used": 1,
                "tasks": ["T2", "T3", "T4", "T5"],
                "started_at": "2026-03-01T00:00:12Z",
                "completed_at": "2026-03-01T00:00:19Z",
                "duration_seconds": 7.0,
                "last_attempt_duration_seconds": 7.0,
                "timing_available": True,
                "attempt_history": execution_state["wave_attempt_history"]["2"],
                "retry_reasons": ["verification_failed"],
                "out_of_contract_paths": [],
                "completed_task_ids": [],
                "failed_task_ids": ["T2"],
                "blocked_task_ids": ["T4"],
                "skipped_task_ids": ["T5"],
                "deferred_task_ids": ["T3"],
                "pending_task_ids": ["T3", "T5"],
                "retry_pending": True,
                "critical_path": True,
            },
        ],
        "task_details": [
            {
                "task_id": "T1",
                "status": "completed",
                "wave": 1,
                "attempts": 1,
                "retries_used": 0,
                "last_error": "",
                "scope_kind": "experiment",
                "files_changed": [],
                "started_at": "2026-03-01T00:00:00Z",
                "completed_at": "2026-03-01T00:00:12Z",
                "duration_seconds": 12.0,
                "timing_available": True,
                "reason_code": "completed",
                "reason_detail": "",
                "blocked_by": [],
                "runner_report_path": ".autolab/runner_execution_report.T1.json",
                "runner_status": "completed",
                "runner_exit_code": 0,
                "verification_status": "passed",
                "verification_commands": ["pytest -q tests/test_model.py"],
                "expected_artifacts_missing": [],
                "evidence_summary": {
                    "runner_status": "completed",
                    "runner_exit_code": 0,
                    "verification_status": "passed",
                    "files_changed_count": 0,
                    "expected_artifacts_missing_count": 0,
                    "text": "done",
                },
                "critical_path": False,
            },
            {
                "task_id": "T2",
                "status": "failed",
                "wave": 2,
                "attempts": 2,
                "retries_used": 1,
                "last_error": "tests failed",
                "scope_kind": "experiment",
                "files_changed": ["src/model.py"],
                "started_at": "2026-03-01T00:00:12Z",
                "completed_at": "2026-03-01T00:00:19Z",
                "duration_seconds": 7.0,
                "timing_available": True,
                "reason_code": "verification_failed",
                "reason_detail": "tests failed",
                "blocked_by": [],
                "runner_report_path": ".autolab/runner_execution_report.T2.json",
                "runner_status": "completed",
                "runner_exit_code": 0,
                "verification_status": "failed",
                "verification_commands": ["pytest -q tests/test_model.py"],
                "expected_artifacts_missing": [],
                "evidence_summary": {
                    "runner_status": "completed",
                    "runner_exit_code": 0,
                    "verification_status": "failed",
                    "files_changed_count": 1,
                    "expected_artifacts_missing_count": 0,
                    "text": "tests failed",
                },
                "critical_path": True,
            },
            {
                "task_id": "T3",
                "status": "pending",
                "wave": 2,
                "attempts": 0,
                "retries_used": 0,
                "last_error": "",
                "scope_kind": "experiment",
                "files_changed": [],
                "started_at": "",
                "completed_at": "",
                "duration_seconds": 0.0,
                "timing_available": False,
                "reason_code": "wave_retry_pending",
                "reason_detail": "waiting for retried wave",
                "blocked_by": ["T2"],
                "runner_report_path": "",
                "runner_status": "unavailable",
                "runner_exit_code": None,
                "verification_status": "not_run",
                "verification_commands": [],
                "expected_artifacts_missing": [],
                "evidence_summary": {
                    "runner_status": "unavailable",
                    "runner_exit_code": None,
                    "verification_status": "not_run",
                    "files_changed_count": 0,
                    "expected_artifacts_missing_count": 0,
                    "text": "waiting for retry",
                },
                "critical_path": True,
            },
            {
                "task_id": "T4",
                "status": "blocked",
                "wave": 2,
                "attempts": 0,
                "retries_used": 0,
                "last_error": "",
                "scope_kind": "experiment",
                "files_changed": [],
                "started_at": "",
                "completed_at": "",
                "duration_seconds": 0.0,
                "timing_available": False,
                "reason_code": "dependency_blocked",
                "reason_detail": "waiting on dependency T2",
                "blocked_by": ["T2"],
                "runner_report_path": "",
                "runner_status": "unavailable",
                "runner_exit_code": None,
                "verification_status": "not_run",
                "verification_commands": [],
                "expected_artifacts_missing": [],
                "evidence_summary": {
                    "runner_status": "unavailable",
                    "runner_exit_code": None,
                    "verification_status": "not_run",
                    "files_changed_count": 0,
                    "expected_artifacts_missing_count": 0,
                    "text": "blocked on dependency T2",
                },
                "critical_path": False,
            },
            {
                "task_id": "T5",
                "status": "pending",
                "wave": 2,
                "attempts": 0,
                "retries_used": 0,
                "last_error": "",
                "scope_kind": "experiment",
                "files_changed": [],
                "started_at": "",
                "completed_at": "",
                "duration_seconds": 0.0,
                "timing_available": False,
                "reason_code": "fail_fast_skipped",
                "reason_detail": "skipped after T2 failure",
                "blocked_by": ["T2"],
                "runner_report_path": "",
                "runner_status": "unavailable",
                "runner_exit_code": None,
                "verification_status": "not_run",
                "verification_commands": [],
                "expected_artifacts_missing": [],
                "evidence_summary": {
                    "runner_status": "unavailable",
                    "runner_exit_code": None,
                    "verification_status": "not_run",
                    "files_changed_count": 0,
                    "expected_artifacts_missing_count": 0,
                    "text": "skipped after fail-fast",
                },
                "critical_path": False,
            },
        ],
        "critical_path": {
            "status": "available",
            "mode": "measured_partial",
            "task_ids": ["T1", "T2", "T3"],
            "wave_ids": [1, 2],
            "duration_seconds": 19.0,
            "weight": 20.0,
            "basis_note": "measured path with structural fallback for tasks lacking timing",
        },
        "file_conflicts": [
            {
                "kind": "same_wave_write_conflict",
                "wave": 2,
                "tasks": ["T2", "T3"],
                "paths": [],
                "conflict_group": "",
                "detail": "same-wave write conflict: tasks T2 and T3 overlap in writes/touches",
            }
        ],
        "diagnostics": [],
        "observability_summary": {
            "waves_total": 2,
            "waves_executed": 2,
            "tasks_total": 5,
            "tasks_completed": 1,
            "tasks_failed": 1,
            "tasks_blocked": 1,
            "tasks_pending": 2,
            "tasks_skipped": 1,
            "tasks_deferred": 1,
            "retrying_waves": 1,
            "conflict_count": 1,
        },
    }
    (iteration_dir / "plan_execution_summary.json").write_text(
        json.dumps(execution_summary, indent=2) + "\n",
        encoding="utf-8",
    )


def test_status_docs_generate_and_policy_doctor_smoke(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )
    assert commands_module.main(["status", "--state-file", str(state_path)]) == 0
    assert (
        commands_module.main(["docs", "generate", "--state-file", str(state_path)]) == 0
    )
    assert (
        commands_module.main(["policy", "doctor", "--state-file", str(state_path)]) == 0
    )


def test_docs_generate_registry_view_includes_legacy_sections(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    (repo / "src").mkdir(exist_ok=True)
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_text = policy_path.read_text(encoding="utf-8")
    policy_path.write_text(
        policy_text + "\nscope_roots:\n  project_wide_root: src\n",
        encoding="utf-8",
    )

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "registry"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "# Autolab Stage Flow" in output
    assert "## Scope Roots" in output
    assert "## Artifact Map" in output
    assert "## Token Reference" in output
    assert "## Classifications" in output
    assert "configured_project_wide_root" in output
    assert "`src`" in output


def test_docs_generate_registry_view_fails_when_workflow_registry_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    workflow_path = repo / ".autolab" / "workflow.yaml"
    assert workflow_path.exists()
    workflow_path.unlink()

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "registry"]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "could not load workflow.yaml registry" in err


@pytest.mark.parametrize("state_case", ["missing", "invalid"])
def test_docs_generate_registry_view_degrades_when_state_missing_or_invalid(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    state_case: str,
) -> None:
    _repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    if state_case == "missing":
        state_path.unlink()
    else:
        state_path.write_text("{not valid json", encoding="utf-8")

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "registry"]
        )
        == 0
    )
    captured = capsys.readouterr()
    assert "# Autolab Stage Flow" in captured.out
    assert "## Artifact Map" in captured.out
    assert "autolab docs generate: ERROR" not in captured.err


def test_docs_generate_fails_with_invalid_project_wide_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_text = policy_path.read_text(encoding="utf-8")
    policy_path.write_text(
        policy_text + "\nscope_roots:\n  project_wide_root: missing_dir\n",
        encoding="utf-8",
    )

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "registry"]
        )
        == 1
    )
    err = capsys.readouterr().err
    assert "scope_roots.project_wide_root" in err


def test_docs_generate_default_outputs_registry_view(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()

    assert (
        commands_module.main(["docs", "generate", "--state-file", str(state_path)]) == 0
    )
    output = capsys.readouterr().out
    assert "# Autolab Stage Flow" in output
    assert "## Artifact Map" in output
    assert "# Project View" not in output


def test_docs_generate_all_view_keeps_generated_views_available(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "all"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "# Project View" in output
    assert "# Roadmap View" in output
    assert "# State View" in output
    assert "# Requirements View" in output
    assert "# Sidecar View" in output


def test_docs_generate_all_view_headings_are_stable_and_unique(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "all"]
        )
        == 0
    )
    output = capsys.readouterr().out
    headings = [
        "# Project View",
        "# Roadmap View",
        "# State View",
        "# Requirements View",
        "# Sidecar View",
    ]
    if "# Autolab Stage Flow" in output:
        headings.insert(0, "# Autolab Stage Flow")
    positions: list[int] = []
    for heading in headings:
        assert output.count(heading) == 1
        positions.append(output.index(heading))
    assert positions == sorted(positions)


def test_docs_generate_project_view_reports_missing_traceability_and_context_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "project"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "## Diagnostics" in output
    assert "missing .autolab/traceability_latest.json" in output
    assert "traceability_coverage.json" in output
    assert "missing .autolab/context/bundle.json" in output
    assert "missing .autolab/context/project_map.json" in output


def test_docs_generate_state_view_reports_invalid_handoff_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    (repo / ".autolab" / "handoff.json").write_text("[]\n", encoding="utf-8")

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "state"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "## Diagnostics" in output
    assert "invalid JSON object at .autolab/handoff.json" in output


def test_docs_generate_writes_selected_view_to_output_dir(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    output_dir = repo / "generated_docs"

    assert (
        commands_module.main(
            [
                "docs",
                "generate",
                "--state-file",
                str(state_path),
                "--view",
                "state",
                "--output-dir",
                str(output_dir),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "views_written: 1" in output
    state_view_path = output_dir / "state.md"
    assert state_view_path.exists()
    assert "# State View" in state_view_path.read_text(encoding="utf-8")


def test_docs_generate_default_output_dir_writes_all_views_without_markdown_body(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    output_dir = repo / "generated_docs"

    assert (
        commands_module.main(
            [
                "docs",
                "generate",
                "--state-file",
                str(state_path),
                "--output-dir",
                str(output_dir),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    expected_files = {"registry.md"}
    written_files = {path.name for path in output_dir.glob("*.md")}
    assert expected_files.issubset(written_files)
    assert f"views_written: {len(written_files)}" in output
    assert "# Project View" not in output
    assert "# Autolab Stage Flow" not in output


def test_docs_generate_rejects_output_dir_outside_repo(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    output_dir = tmp_path / "outside_repo_output"

    assert (
        commands_module.main(
            [
                "docs",
                "generate",
                "--state-file",
                str(state_path),
                "--view",
                "state",
                "--output-dir",
                str(output_dir),
            ]
        )
        == 1
    )
    err = capsys.readouterr().err.lower()
    assert "output-dir" in err
    assert "repo" in err
    assert "outside" in err or "inside" in err or "within" in err


def test_docs_generate_sidecar_view_distinguishes_missing_and_invalid_artifact_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    (repo / ".autolab" / "handoff.json").write_text("[]\n", encoding="utf-8")

    assert (
        commands_module.main(
            [
                "docs",
                "generate",
                "--state-file",
                str(state_path),
                "--view",
                "sidecar",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "| handoff.json | `.autolab/handoff.json` | invalid |" in output
    assert (
        "| traceability_latest.json | `.autolab/traceability_latest.json` | missing |"
        in output
    )


@pytest.mark.parametrize(
    ("view", "traceability_path_snippet"),
    [
        (
            "sidecar",
            "| traceability_coverage.json | `experiments/plan/iter_target/traceability_coverage.json` |",
        ),
        (
            "project",
            "- traceability_coverage_path: `experiments/plan/iter_target/traceability_coverage.json`",
        ),
        (
            "requirements",
            "- traceability_coverage_path: `experiments/plan/iter_target/traceability_coverage.json`",
        ),
    ],
)
def test_docs_generate_iteration_override_prefers_target_traceability_with_mismatch_diagnostic(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    view: str,
    traceability_path_snippet: str,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _write_iteration_docs_fixture(repo, iteration_id="iter_old")
    _write_iteration_docs_fixture(repo, iteration_id="iter_target")
    (repo / ".autolab" / "traceability_latest.json").write_text(
        json.dumps(
            {
                "iteration_id": "iter_old",
                "traceability_path": "experiments/plan/iter_old/traceability_coverage.json",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        commands_module.main(
            [
                "docs",
                "generate",
                "--state-file",
                str(state_path),
                "--view",
                view,
                "--iteration-id",
                "iter_target",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "iteration_id: `iter_target`" in output
    assert traceability_path_snippet in output
    assert (
        "traceability_latest.traceability_path differs from selected coverage path"
        in (output)
    )


def test_docs_generate_reports_stale_handoff_iteration_mismatch(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    (repo / ".autolab" / "handoff.json").write_text(
        json.dumps(
            {
                "iteration_id": "iter_stale",
                "handoff_markdown_path": "experiments/plan/iter_stale/handoff.md",
                "safe_resume_point": {"status": "ready", "command": "autolab verify"},
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        commands_module.main(
            [
                "docs",
                "generate",
                "--state-file",
                str(state_path),
                "--view",
                "sidecar",
                "--iteration-id",
                "iter_target",
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "handoff" in output.lower()
    assert "iteration_id" in output
    assert "iter_stale" in output
    assert "iter_target" in output


def test_docs_generate_state_view_includes_wave_observability_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _write_wave_observability_fixture(repo, state_path)
    assert commands_module.main(["progress", "--state-file", str(state_path)]) == 0
    _ = capsys.readouterr()

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "state"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "## Wave Observability" in output
    assert "## Critical Path" in output
    assert "| 2 | failed | T2, T3, T4, T5 |" in output
    assert "blocked_tasks: T4" in output
    assert "deferred_tasks: T3" in output
    assert "skipped_tasks: T5" in output
    assert (
        "| T2 | 2 | failed | 2 | 1 | 7.0 | verification_failed (tests failed) | - | failed | tests failed | yes |"
        in output
    )


@pytest.mark.parametrize(
    ("view", "expected_snippet"),
    [
        ("project", "- mode: `measured_partial`"),
        (
            "sidecar",
            "| plan_execution_summary.json | `experiments/plan/iter_obs/plan_execution_summary.json` | present | critical_path=measured_partial |",
        ),
    ],
)
def test_docs_generate_project_and_sidecar_views_include_wave_observability_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    view: str,
    expected_snippet: str,
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _write_wave_observability_fixture(repo, state_path)
    assert commands_module.main(["progress", "--state-file", str(state_path)]) == 0
    _ = capsys.readouterr()

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", view]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "## Wave Observability" in output
    assert expected_snippet in output


def test_docs_generate_sidecar_view_reports_stale_observability_artifacts_as_diagnostics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _write_wave_observability_fixture(repo, state_path, iteration_id="iter_target")

    summary_path = (
        repo / "experiments" / "plan" / "iter_target" / "plan_execution_summary.json"
    )
    summary_payload = json.loads(summary_path.read_text(encoding="utf-8"))
    summary_payload["iteration_id"] = "iter_stale"
    summary_path.write_text(
        json.dumps(summary_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "plan_graph.json").write_text(
        json.dumps(
            {
                "nodes": [{"task_id": "Z9"}],
                "edges": [],
                "waves": [{"wave": 1, "tasks": ["Z9"]}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "plan_check_result.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "errors": [
                    "same-wave write conflict: tasks Z9 and Z10 overlap in writes/touches"
                ],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    assert (
        commands_module.main(
            ["docs", "generate", "--state-file", str(state_path), "--view", "sidecar"]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert "## Wave Observability" in output
    assert (
        "| plan_execution_summary.json | `experiments/plan/iter_target/plan_execution_summary.json` | stale |"
        in output
    )
    assert (
        "| plan_graph.json | `.autolab/plan_graph.json` | stale | waves=0 |" in output
    )
    assert (
        "stale plan_execution_summary.json: iteration_id differs from requested iteration_id "
        "(iter_stale != iter_target); ignoring artifact"
    ) in output
    assert (
        "stale plan_graph.json: graph tasks do not overlap selected iteration execution tasks; ignoring artifact"
        in output
    )
    assert (
        "stale plan_check_result.json: ignoring artifact because plan_graph.json was ignored as stale for the selected iteration"
        in output
    )


def test_update_command_routes_to_handler(
    monkeypatch,
) -> None:
    captured: dict[str, Path] = {}

    def _fake_run_update(cwd: Path) -> UpdateResult:
        captured["cwd"] = cwd
        return UpdateResult(
            current_version="1.1.0",
            latest_tag="v1.1.1",
            upgraded=True,
            synced_scaffold=False,
            sync_skipped_reason="outside repo",
        )

    monkeypatch.setattr(commands_module, "run_update", _fake_run_update)

    exit_code = commands_module.main(["update"])

    assert exit_code == 0
    assert captured["cwd"] == Path.cwd()


def test_update_command_propagates_failure_exit_code(
    monkeypatch,
) -> None:
    def _raise_error(_cwd: Path) -> UpdateResult:
        raise RuntimeError("simulated update failure")

    monkeypatch.setattr(commands_module, "run_update", _raise_error)

    exit_code = commands_module.main(["update"])

    assert exit_code == 1


def test_package_data_contract_includes_registry_and_golden_fixtures() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = _load_toml(pyproject_path)

    package_data = (
        pyproject.get("tool", {})
        .get("setuptools", {})
        .get("package-data", {})
        .get("autolab", [])
    )
    assert isinstance(package_data, list)

    assert "scaffold/.autolab/workflow.yaml" in package_data
    assert "example_golden_iterations/README.md" in package_data
    assert (
        "example_golden_iterations/experiments/plan/iter_golden/runs/*/*.json"
        in package_data
    )
    assert "scaffold/.autolab/parser_fixtures/*/fixture.yaml" in package_data
    assert "scaffold/.autolab/parser_fixtures/*/repo/**/*.json" in package_data
    assert "scaffold/.autolab/parser_fixtures/*/repo/**/*.yaml" in package_data
    assert "scaffold/.autolab/parser_fixtures/*/repo/**/*.py" in package_data
    assert "scaffold/.autolab/parser_fixtures/*/expected/**/*.json" in package_data
    assert "scaffold/.autolab/parser_fixtures/*/expected/**/*.md" in package_data


def test_console_script_contract_points_to_main_entrypoint() -> None:
    pyproject_path = Path(__file__).resolve().parents[1] / "pyproject.toml"
    pyproject = _load_toml(pyproject_path)
    scripts = pyproject.get("project", {}).get("scripts", {})
    assert isinstance(scripts, dict)
    assert scripts.get("autolab") == "autolab.__main__:main"


def test_installed_console_script_can_run_render(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    venv_dir = tmp_path / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", str(venv_dir)],
        check=True,
        capture_output=True,
        text=True,
    )
    bin_dir = venv_dir / ("Scripts" if os.name == "nt" else "bin")
    python_bin = bin_dir / "python"
    autolab_bin = bin_dir / "autolab"
    if os.name == "nt":
        autolab_bin = bin_dir / "autolab.exe"

    install_result = subprocess.run(
        [str(python_bin), "-m", "pip", "install", "--no-deps", "-e", str(repo_root)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert install_result.returncode == 0, install_result.stderr

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state_path = workspace / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "iteration_id": "iter1",
                "experiment_id": "e1",
                "stage": "implementation",
                "stage_attempt": 0,
                "last_run_id": "",
                "pending_run_id": "",
                "sync_status": "na",
                "max_stage_attempts": 3,
                "max_total_iterations": 20,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    prompt_path = workspace / ".autolab" / "prompts" / "stage_implementation.runner.md"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(
        "# Stage: implementation\n\nstage: {{stage}}\niteration_id: {{iteration_id}}\n",
        encoding="utf-8",
    )
    audit_prompt_path = (
        workspace / ".autolab" / "prompts" / "stage_implementation.audit.md"
    )
    audit_prompt_path.write_text(
        "# Stage: implementation (audit)\n\nstage: {{stage}}\niteration_id: {{iteration_id}}\n",
        encoding="utf-8",
    )
    brief_prompt_path = (
        workspace / ".autolab" / "prompts" / "stage_implementation.brief.md"
    )
    brief_prompt_path.write_text(
        "# Stage: implementation (brief)\n\n{{brief_summary}}\n",
        encoding="utf-8",
    )
    human_prompt_path = (
        workspace / ".autolab" / "prompts" / "stage_implementation.human.md"
    )
    human_prompt_path.write_text(
        "# Stage: implementation (human packet)\n\n{{brief_summary}}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [str(autolab_bin), "render", "--state-file", str(state_path)],
        cwd=workspace,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "stage: implementation" in result.stdout.lower()


def test_top_level_help_groups_commands_for_onboarding() -> None:
    help_text = commands_module._build_parser().format_help()
    assert "positional arguments:" in help_text
    assert "COMMAND" in help_text
    assert "  Getting started:" in help_text
    assert "  Run workflow:" in help_text
    assert "  Backlog steering:" in help_text
    assert "  Safety and policy:" in help_text
    assert "  Maintenance:" in help_text
    assert "init" in help_text
    assert "configure" in help_text
    assert "progress" in help_text
    assert "run" in help_text
    assert "loop" in help_text
    assert "handoff" in help_text
    assert "resume" in help_text
    assert "tui" in help_text
    assert "render" in help_text
    assert "parser" in help_text
    assert "todo" in help_text
    assert "policy" in help_text
    assert "update" in help_text
    assert "report" in help_text
    assert "Record a human review decision" in help_text
    assert "Recommended onboarding flow:" in help_text


def test_init_help_includes_from_existing_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = commands_module._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["init", "--help"])

    assert int(exc_info.value.code) == 0
    captured = capsys.readouterr()
    assert "--from-existing" in captured.out


def test_parser_help_includes_init_and_test(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = commands_module._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["parser", "--help"])

    assert int(exc_info.value.code) == 0
    captured = capsys.readouterr()
    assert "init" in captured.out
    assert "test" in captured.out


def test_progress_handoff_and_resume_preview_generate_handoff_artifacts(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)

    assert commands_module.main(["progress", "--state-file", str(state_path)]) == 0
    progress_out = capsys.readouterr().out
    assert "autolab progress" in progress_out
    assert "recommended_next_command:" in progress_out

    handoff_json_path = repo / ".autolab" / "handoff.json"
    assert handoff_json_path.exists()
    handoff_payload = json.loads(handoff_json_path.read_text(encoding="utf-8"))
    assert handoff_payload["current_scope"] in {"experiment", "project_wide"}
    assert handoff_payload["current_stage"] == "hypothesis"
    assert "recommended_next_command" in handoff_payload
    handoff_md_path = Path(handoff_payload["handoff_markdown_path"])
    assert handoff_md_path.exists()

    assert commands_module.main(["handoff", "--state-file", str(state_path)]) == 0
    handoff_out = capsys.readouterr().out
    assert "autolab handoff" in handoff_out
    assert "handoff_json:" in handoff_out
    assert "handoff_md:" in handoff_out

    assert commands_module.main(["resume", "--state-file", str(state_path)]) == 0
    resume_out = capsys.readouterr().out
    assert "autolab resume" in resume_out
    assert "mode: preview" in resume_out


def test_progress_reports_wave_observability_sections(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _ = capsys.readouterr()
    _write_wave_observability_fixture(repo, state_path)

    assert commands_module.main(["progress", "--state-file", str(state_path)]) == 0
    output = capsys.readouterr().out
    assert "critical_path:" in output
    assert (
        "status=available mode=measured_partial duration=19.0s weight=20.0 waves=2 tasks=3"
        in output
    )
    assert (
        "basis: measured path with structural fallback for tasks lacking timing"
        in output
    )
    assert "wave_details:" in output
    assert "retry_reasons: verification_failed" in output
    assert "failed_tasks: T2" in output
    assert "blocked_tasks: T4" in output
    assert "deferred_tasks: T3" in output
    assert "skipped_tasks: T5" in output
    assert "pending_tasks: T3, T5" in output
    assert "file_conflicts:" in output
    assert "same_wave_write_conflict" in output
    assert "task_evidence:" in output
    assert (
        "task=T2 wave=2 status=failed attempts=2 retries=1 critical_path=yes" in output
    )
    assert "reason: verification_failed (tests failed)" in output
    assert (
        "task=T4 wave=2 status=blocked attempts=0 retries=0 critical_path=no" in output
    )
    assert "blocked_by: T2" in output


def test_handoff_uses_configured_project_wide_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    (repo / "src").mkdir(exist_ok=True)
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_text = policy_path.read_text(encoding="utf-8")
    policy_path.write_text(
        policy_text + "\nscope_roots:\n  project_wide_root: src\n",
        encoding="utf-8",
    )
    plan_contract = {
        "schema_version": "1.0",
        "tasks": [
            {"task_id": "T1", "scope_kind": "project_wide"},
        ],
    }
    (repo / ".autolab" / "plan_contract.json").write_text(
        json.dumps(plan_contract, indent=2) + "\n",
        encoding="utf-8",
    )

    assert commands_module.main(["progress", "--state-file", str(state_path)]) == 0
    _ = capsys.readouterr()
    payload = json.loads(
        (repo / ".autolab" / "handoff.json").read_text(encoding="utf-8")
    )
    assert payload["current_scope"] == "project_wide"
    assert payload["scope_root"] == str((repo / "src").resolve())
    assert payload["handoff_markdown_path"] == str(
        (repo / "src" / "handoff.md").resolve()
    )


def test_resume_apply_executes_recommended_command_when_ready(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = tmp_path / "repo" / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")
    resolved_state_path = state_path.expanduser().resolve()
    payload = {
        "recommended_next_command": {
            "command": "autolab verify --stage design",
            "reason": "retry verification",
            "executable": True,
        },
        "safe_resume_point": {
            "command": "autolab verify --stage design",
            "status": "ready",
            "preconditions": [],
        },
    }
    captured: dict[str, list[str]] = {}

    monkeypatch.setattr(
        commands_module,
        "_safe_refresh_handoff",
        lambda _state_path: (payload, ""),
    )

    def _fake_main(argv: list[str] | None = None) -> int:
        captured["argv"] = list(argv or [])
        return 17

    monkeypatch.setattr(commands_module, "main", _fake_main)

    exit_code = commands_module._cmd_resume(
        argparse.Namespace(state_file=str(state_path), apply=True)
    )

    assert exit_code == 17
    assert captured["argv"] == [
        "verify",
        "--stage",
        "design",
        "--state-file",
        str(resolved_state_path),
    ]


def test_resume_apply_rejects_non_autolab_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_path = tmp_path / "repo" / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")
    payload = {
        "recommended_next_command": {
            "command": "python -m pytest -q",
            "reason": "not allowed by resume executor",
            "executable": True,
        },
        "safe_resume_point": {
            "command": "python -m pytest -q",
            "status": "ready",
            "preconditions": [],
        },
    }
    monkeypatch.setattr(
        commands_module,
        "_safe_refresh_handoff",
        lambda _state_path: (payload, ""),
    )

    exit_code = commands_module._cmd_resume(
        argparse.Namespace(state_file=str(state_path), apply=True)
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "only autolab commands are executable" in captured.err


def test_review_subcommand_help_uses_human_review_terminology(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = commands_module._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["review", "--help"])

    assert int(exc_info.value.code) == 0
    captured = capsys.readouterr()
    assert "--status {pass,retry,stop}" in captured.out
    assert "Human review decision:" in captured.out


def test_experiment_subcommand_help_includes_create(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = commands_module._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["experiment", "--help"])

    assert int(exc_info.value.code) == 0
    captured = capsys.readouterr()
    assert "create" in captured.out
    assert "move" in captured.out


def test_experiment_create_help_shows_required_flags(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parser = commands_module._build_parser()

    with pytest.raises(SystemExit) as exc_info:
        parser.parse_args(["experiment", "create", "--help"])

    assert int(exc_info.value.code) == 0
    captured = capsys.readouterr()
    assert "--experiment-id EXPERIMENT_ID" in captured.out
    assert "--iteration-id ITERATION_ID" in captured.out
    assert "--hypothesis-id HYPOTHESIS_ID" in captured.out


def test_status_human_review_banner_mentions_human_review_decision(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"

    assert (
        commands_module.main(
            ["init", "--state-file", str(state_path), "--no-interactive"]
        )
        == 0
    )

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    payload["stage"] = "human_review"
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    assert commands_module.main(["status", "--state-file", str(state_path)]) == 0
    captured = capsys.readouterr()
    assert "*** HUMAN REVIEW REQUIRED ***" in captured.out
    assert "record the human review decision" in captured.out


def test_discuss_answers_file_writes_experiment_sidecar(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    iteration_dir = _write_discuss_research_context(repo, state_path)
    _ = capsys.readouterr()
    answers_path = repo / "answers.json"
    question_pack_path = repo / "question_pack.json"
    answers_path.write_text(
        json.dumps(
            {
                "responses": {
                    "locked_decisions": [
                        {
                            "id": "ld1",
                            "summary": "Keep the baseline trainer unchanged",
                            "detail": "baseline stays fixed",
                            "status": "bogus",
                        }
                    ],
                    "preferences": ["Prefer narrow diffs | keep reviewable patches"],
                    "constraints": [
                        "Do not change benchmark definitions | benchmark contract stays fixed"
                    ],
                    "open_questions": [
                        "Which parser fields are still missing? | parser coverage gap"
                    ],
                    "promotion_candidates": [
                        "Promote parser compatibility | R_shared | needed for shared parser path | project_wide"
                    ],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = commands_module.main(
        [
            "discuss",
            "--state-file",
            str(state_path),
            "--scope",
            "experiment",
            "--answers-file",
            str(answers_path),
            "--write-question-pack",
            str(question_pack_path),
            "--json",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["scope_kind"] == "experiment"
    assert question_pack_path.exists()
    sidecar_path = iteration_dir / "context" / "sidecars" / "discuss.json"
    markdown_path = iteration_dir / "context" / "sidecars" / "discuss.md"
    assert sidecar_path.exists()
    assert markdown_path.exists()
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert payload["scope_kind"] == "experiment"
    assert payload["iteration_id"] == "iter_ctx"
    assert payload["experiment_id"] == "e_ctx"
    assert payload["locked_decisions"][0]["status"] == "locked"
    assert payload["promotion_candidates"][0]["target_scope_kind"] == "project_wide"
    assert payload["promotion_candidates"][0]["requirement_hint"] == "R_shared"


def test_discuss_non_interactive_uses_existing_or_default_answers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    iteration_dir = _write_discuss_research_context(repo, state_path)
    _ = capsys.readouterr()

    exit_code = commands_module.main(
        [
            "discuss",
            "--state-file",
            str(state_path),
            "--scope",
            "experiment",
            "--non-interactive",
            "--json",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    sidecar_path = iteration_dir / "context" / "sidecars" / "discuss.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert payload["locked_decisions"] == []
    assert payload["open_questions"] == []


def test_discuss_project_wide_ignores_promotion_candidates_from_answers_file(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _write_discuss_research_context(repo, state_path)
    _ = capsys.readouterr()
    answers_path = repo / "project_answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "responses": {
                    "locked_decisions": [
                        "Keep the shared parser contract stable | no drift"
                    ],
                    "promotion_candidates": [
                        "This should be ignored | R_ignore | invalid for project-wide discuss | project_wide"
                    ],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = commands_module.main(
        [
            "discuss",
            "--state-file",
            str(state_path),
            "--scope",
            "project_wide",
            "--answers-file",
            str(answers_path),
            "--json",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["scope_kind"] == "project_wide"
    sidecar_path = (
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "discuss.json"
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert (
        payload["locked_decisions"][0]["summary"]
        == "Keep the shared parser contract stable"
    )
    assert payload["promotion_candidates"] == []


def test_discuss_iteration_override_resolves_target_experiment_identity(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _write_discuss_research_context(
        repo,
        state_path,
        iteration_id="iter_active",
        experiment_id="exp_active",
    )
    other_iteration_dir = _write_discuss_research_context(
        repo,
        state_path,
        iteration_id="iter_other",
        experiment_id="exp_other",
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["iteration_id"] = "iter_active"
    state["experiment_id"] = "exp_active"
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    (repo / ".autolab" / "backlog.yaml").write_text(
        "\n".join(
            [
                "hypotheses:",
                "  - id: h_active",
                "    status: open",
                "    title: Active hypothesis",
                "    success_metric: accuracy",
                "    target_delta: 0.1",
                "experiments:",
                "  - id: exp_active",
                "    hypothesis_id: h_active",
                "    status: in_progress",
                "    type: plan",
                "    iteration_id: iter_active",
                "  - id: exp_other",
                "    hypothesis_id: h_active",
                "    status: open",
                "    type: plan",
                "    iteration_id: iter_other",
                "",
            ]
        ),
        encoding="utf-8",
    )
    _ = capsys.readouterr()
    answers_path = repo / "override_answers.json"
    answers_path.write_text(
        json.dumps(
            {
                "responses": {
                    "locked_decisions": [
                        "Target the other iteration identity | ensure experiment_id follows iteration"
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = commands_module.main(
        [
            "discuss",
            "--state-file",
            str(state_path),
            "--scope",
            "experiment",
            "--iteration-id",
            "iter_other",
            "--answers-file",
            str(answers_path),
            "--json",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["iteration_id"] == "iter_other"
    assert result["experiment_id"] == "exp_other"
    sidecar_path = other_iteration_dir / "context" / "sidecars" / "discuss.json"
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert payload["iteration_id"] == "iter_other"
    assert payload["experiment_id"] == "exp_other"


def test_research_noops_without_questions(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    iteration_dir = _write_discuss_research_context(repo, state_path)
    _ = capsys.readouterr()

    exit_code = commands_module.main(
        [
            "research",
            "--state-file",
            str(state_path),
            "--scope",
            "experiment",
            "--json",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "noop"
    assert result["reason"] == "no unresolved questions"
    assert not (iteration_dir / "context" / "sidecars" / "research.json").exists()


def test_research_writes_sidecar_with_mocked_local_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    iteration_dir = _write_discuss_research_context(repo, state_path)
    _ = capsys.readouterr()
    discuss_sidecar_path = iteration_dir / "context" / "sidecars" / "discuss.json"
    discuss_sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    discuss_sidecar_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sidecar_kind": "discuss",
                "scope_kind": "experiment",
                "scope_root": str(iteration_dir.resolve()),
                "iteration_id": "iter_ctx",
                "experiment_id": "e_ctx",
                "generated_at": "2026-03-05T00:00:00Z",
                "derived_from": [],
                "stale_if": [],
                "locked_decisions": [],
                "preferences": [],
                "constraints": [],
                "open_questions": [
                    {
                        "id": "oq_parser",
                        "summary": "Which parser outputs still need evidence?",
                        "detail": "Determine the minimum supported parser outputs.",
                        "status": "unresolved",
                    }
                ],
                "promotion_candidates": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    def _fake_run_local_agent(*_args, **_kwargs):
        return (
            0,
            json.dumps(
                {
                    "findings": [
                        {
                            "id": "finding_parser",
                            "summary": "The parser should emit deterministic metric keys.",
                            "detail": "This keeps extraction and design contracts aligned.",
                            "question_ids": ["oq_parser"],
                            "source_ids": ["project_map", "context_delta"],
                        }
                    ],
                    "recommendations": [
                        {
                            "id": "rec_parser",
                            "summary": "Reference the parser compatibility finding in design.",
                            "detail": "Keep the runner packet compact by referencing the sidecar item.",
                            "question_ids": ["oq_parser"],
                            "finding_ids": ["finding_parser"],
                            "source_ids": ["project_map"],
                            "applies_to_stages": ["design", "implementation"],
                        }
                    ],
                }
            ),
            "",
            "mock-agent",
        )

    monkeypatch.setattr(handlers_admin, "_run_local_agent", _fake_run_local_agent)
    monkeypatch.setattr(commands_module, "_run_local_agent", _fake_run_local_agent)

    exit_code = commands_module.main(
        [
            "research",
            "--state-file",
            str(state_path),
            "--scope",
            "experiment",
            "--json",
        ]
    )

    assert exit_code == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "ok"
    assert result["question_count"] == 1
    assert result["finding_count"] == 1
    assert result["recommendation_count"] == 1
    assert result["llm_command"] == "mock-agent"
    research_path = iteration_dir / "context" / "sidecars" / "research.json"
    research_payload = json.loads(research_path.read_text(encoding="utf-8"))
    assert research_payload["questions"][0]["id"] == "oq_parser"
    assert research_payload["questions"][0]["status"] == "answered"
    assert research_payload["findings"][0]["question_ids"] == ["oq_parser"]
    assert research_payload["recommendations"][0]["finding_ids"] == ["finding_parser"]
    assert research_payload["recommendations"][0]["applies_to_stages"] == [
        "design",
        "implementation",
    ]


def test_discuss_rejects_invalid_question_pack_target(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _write_discuss_research_context(repo, state_path)
    _ = capsys.readouterr()

    exit_code = commands_module.main(
        [
            "discuss",
            "--state-file",
            str(state_path),
            "--scope",
            "experiment",
            "--non-interactive",
            "--write-question-pack",
            str(repo / ".autolab"),
        ]
    )

    assert exit_code == 1
    assert "failed writing question pack" in capsys.readouterr().err


def test_research_returns_cli_error_for_invalid_agent_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    _write_discuss_research_context(repo, state_path)
    _ = capsys.readouterr()
    monkeypatch.setenv("AUTOLAB_RESEARCH_AGENT_COMMAND", '"')

    exit_code = commands_module.main(
        [
            "research",
            "--state-file",
            str(state_path),
            "--scope",
            "experiment",
            "--question",
            "What evidence is still missing?",
        ]
    )

    assert exit_code == 1
    assert "could not be parsed" in capsys.readouterr().err


def test_research_rejects_invalid_provenance_from_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo, state_path = _init_repo_state(tmp_path)
    iteration_dir = _write_discuss_research_context(repo, state_path)
    _ = capsys.readouterr()

    def _fake_run_local_agent(*_args, **_kwargs):
        return (
            0,
            json.dumps(
                {
                    "findings": [
                        {
                            "id": "finding_parser",
                            "summary": "Missing provenance",
                            "detail": "This should fail normalization.",
                            "question_ids": ["unknown_question"],
                            "source_ids": ["unknown_source"],
                        }
                    ],
                    "recommendations": [],
                }
            ),
            "",
            "mock-agent",
        )

    monkeypatch.setattr(handlers_admin, "_run_local_agent", _fake_run_local_agent)
    monkeypatch.setattr(commands_module, "_run_local_agent", _fake_run_local_agent)

    exit_code = commands_module.main(
        [
            "research",
            "--state-file",
            str(state_path),
            "--scope",
            "experiment",
            "--question",
            "What evidence is still missing?",
        ]
    )

    assert exit_code == 1
    assert "must reference at least one known question_id" in capsys.readouterr().err
    assert not (iteration_dir / "context" / "sidecars" / "research.json").exists()


def test_packaged_golden_iteration_fixture_contract() -> None:
    repo_root = Path(__file__).resolve().parents[1]
    packaged_root = repo_root / "src" / "autolab" / "example_golden_iterations"
    assert packaged_root.is_dir()

    packaged_files = sorted(
        path.relative_to(packaged_root)
        for path in packaged_root.rglob("*")
        if path.is_file()
    )
    expected_files = sorted(
        [
            Path(".autolab/backlog.yaml"),
            Path(".autolab/plan_contract.json"),
            Path(".autolab/state.json"),
            Path("README.md"),
            Path("experiments/plan/iter_golden/analysis/summary.md"),
            Path("experiments/plan/iter_golden/decision_result.json"),
            Path("experiments/plan/iter_golden/design.yaml"),
            Path("experiments/plan/iter_golden/docs_update.md"),
            Path("experiments/plan/iter_golden/hypothesis.md"),
            Path("experiments/plan/iter_golden/implementation_plan.md"),
            Path("experiments/plan/iter_golden/plan_contract.json"),
            Path("experiments/plan/iter_golden/implementation_review.md"),
            Path("experiments/plan/iter_golden/launch/run_local.sh"),
            Path("experiments/plan/iter_golden/review_result.json"),
            Path(
                "experiments/plan/iter_golden/runs/20260201T120000Z_demo/metrics.json"
            ),
            Path(
                "experiments/plan/iter_golden/runs/20260201T120000Z_demo/run_manifest.json"
            ),
            Path("paper/results.md"),
        ]
    )

    assert packaged_files == expected_files
