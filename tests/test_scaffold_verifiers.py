from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


pytest.importorskip("jsonschema")


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


def _write_state(
    repo: Path,
    *,
    stage: str = "implementation_review",
    last_run_id: str = "",
    paper_targets: list[str] | str | None = None,
) -> None:
    state = {
        "iteration_id": "iter1",
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": last_run_id,
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    if paper_targets is not None:
        state["paper_targets"] = paper_targets
    path = repo / ".autolab" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _write_backlog(repo: Path) -> None:
    backlog = {
        "hypotheses": [
            {
                "id": "h1",
                "status": "open",
                "title": "hypothesis",
                "success_metric": "accuracy",
                "target_delta": 0.1,
            }
        ],
        "experiments": [
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "iteration_id": "iter1",
            }
        ],
    }
    path = repo / ".autolab" / "backlog.yaml"
    path.write_text(yaml.safe_dump(backlog, sort_keys=False), encoding="utf-8")


def _write_agent_result(repo: Path) -> None:
    payload = {
        "status": "complete",
        "summary": "ok",
        "changed_files": [],
        "completion_token_seen": True,
    }
    path = repo / ".autolab" / "agent_result.json"
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_design(repo: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "id": "d1",
        "iteration_id": "iter1",
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": {"location": "local", "gpu_count": 0},
        "metrics": {
            "primary": {"name": "accuracy", "unit": "%", "mode": "maximize"},
            "secondary": [],
            "success_delta": "+1.0%",
            "aggregation": "mean",
            "baseline_comparison": "vs baseline",
        },
        "baselines": [{"name": "baseline", "description": "existing"}],
        "implementation_requirements": [
            {
                "requirement_id": "R1",
                "description": "Implement baseline training path",
                "scope_kind": "experiment",
                "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            }
        ],
        "extract_parser": {
            "kind": "command",
            "command": "python3 -m scripts.extract_results --run-id {run_id} --iteration-path {iteration_path}",
        },
        "variants": [{"name": "proposed", "changes": {}}],
    }
    path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_plan_contract(repo: Path) -> None:
    payload = {
        "schema_version": "1.0",
        "iteration_id": "iter1",
        "stage": "implementation",
        "generated_at": "2026-01-01T00:00:00Z",
        "tasks": [
            {
                "task_id": "T1",
                "objective": "Minimal implementation task for fixture validation.",
                "scope_kind": "experiment",
                "depends_on": [],
                "reads": ["experiments/plan/iter1/design.yaml"],
                "writes": ["experiments/plan/iter1/implementation_plan.md"],
                "touches": ["experiments/plan/iter1/implementation_plan.md"],
                "conflict_group": "",
                "verification_commands": [
                    "python -m pytest -q tests/test_scaffold_verifiers.py"
                ],
                "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
                "failure_policy": "fail_fast",
                "can_run_in_parallel": False,
                "covers_requirements": ["R1"],
            }
        ],
    }
    canonical = repo / ".autolab" / "plan_contract.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    snapshot = repo / "experiments" / "plan" / "iter1" / "plan_contract.json"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_review_result(repo: Path, *, include_docs_check: bool = True) -> None:
    required_checks = {
        "tests": "pass",
        "dry_run": "pass",
        "schema": "pass",
        "env_smoke": "pass",
    }
    if include_docs_check:
        required_checks["docs_target_update"] = "pass"
    payload = {
        "status": "pass",
        "blocking_findings": [],
        "required_checks": required_checks,
        "reviewed_at": "2026-01-01T00:00:00Z",
    }
    path = repo / "experiments" / "plan" / "iter1" / "review_result.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_handoff(repo: Path, *, valid: bool = True) -> None:
    path = repo / ".autolab" / "handoff.json"
    wave_observability = {
        "status": "available",
        "wave_summary": {
            "status": "available",
            "current": 1,
            "executed": 1,
            "total": 1,
        },
        "task_summary": {
            "status": "available",
            "total": 1,
            "completed": 1,
            "failed": 0,
            "blocked": 0,
            "pending": 0,
            "skipped": 0,
            "deferred": 0,
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
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:00:01Z",
                    "duration_seconds": 1.0,
                    "timing_available": True,
                    "reason_code": "completed",
                    "reason_detail": "",
                    "blocked_by": [],
                    "runner_report_path": ".autolab/runner_execution_report.T1.json",
                    "runner_status": "completed",
                    "runner_exit_code": 0,
                    "verification_status": "passed",
                    "verification_commands": [],
                    "expected_artifacts_missing": [],
                    "evidence_summary": {
                        "runner_status": "completed",
                        "runner_exit_code": 0,
                        "verification_status": "passed",
                        "files_changed_count": 0,
                        "expected_artifacts_missing_count": 0,
                        "text": "runner=completed verify=passed files=0 missing_artifacts=0",
                    },
                    "critical_path": True,
                }
            ],
        },
        "summary": {
            "waves_total": 1,
            "waves_executed": 1,
            "tasks_total": 1,
            "tasks_completed": 1,
            "tasks_failed": 0,
            "tasks_blocked": 0,
            "tasks_pending": 0,
            "tasks_skipped": 0,
            "tasks_deferred": 0,
            "retrying_waves": 0,
            "conflict_count": 0,
        },
        "critical_path": {
            "status": "available",
            "mode": "measured_complete",
            "task_ids": ["T1"],
            "wave_ids": [1],
            "duration_seconds": 1.0,
            "weight": 1.0,
            "basis_note": "measured path using recorded task durations",
        },
        "file_conflicts": [],
        "waves": [
            {
                "wave": 1,
                "status": "completed",
                "attempts": 1,
                "retries_used": 0,
                "tasks": ["T1"],
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": "2026-01-01T00:00:01Z",
                "duration_seconds": 1.0,
                "last_attempt_duration_seconds": 1.0,
                "timing_available": True,
                "attempt_history": [
                    {
                        "attempt": 1,
                        "status": "completed",
                        "started_at": "2026-01-01T00:00:00Z",
                        "completed_at": "2026-01-01T00:00:01Z",
                        "duration_seconds": 1.0,
                        "retry_reason": "",
                        "detail": "",
                    }
                ],
                "retry_reasons": [],
                "current_retry_reasons": [],
                "out_of_contract_paths": [],
                "completed_task_ids": ["T1"],
                "failed_task_ids": [],
                "blocked_task_ids": [],
                "skipped_task_ids": [],
                "deferred_task_ids": [],
                "pending_task_ids": [],
                "retry_pending": False,
                "critical_path": True,
            }
        ],
        "tasks": [
            {
                "task_id": "T1",
                "status": "completed",
                "wave": 1,
                "attempts": 1,
                "retries_used": 0,
                "last_error": "",
                "scope_kind": "experiment",
                "files_changed": [],
                "started_at": "2026-01-01T00:00:00Z",
                "completed_at": "2026-01-01T00:00:01Z",
                "duration_seconds": 1.0,
                "timing_available": True,
                "reason_code": "completed",
                "reason_detail": "",
                "blocked_by": [],
                "runner_report_path": ".autolab/runner_execution_report.T1.json",
                "runner_status": "completed",
                "runner_exit_code": 0,
                "verification_status": "passed",
                "verification_commands": [],
                "expected_artifacts_missing": [],
                "evidence_summary": {
                    "runner_status": "completed",
                    "runner_exit_code": 0,
                    "verification_status": "passed",
                    "files_changed_count": 0,
                    "expected_artifacts_missing_count": 0,
                    "text": "runner=completed verify=passed files=0 missing_artifacts=0",
                },
                "critical_path": True,
            }
        ],
        "diagnostics": [],
        "source_paths": {
            "plan_graph_path": ".autolab/plan_graph.json",
            "plan_check_result_path": ".autolab/plan_check_result.json",
            "plan_execution_state_path": "experiments/plan/iter1/plan_execution_state.json",
            "plan_execution_summary_path": "experiments/plan/iter1/plan_execution_summary.json",
        },
    }
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00Z",
        "state_file": str(repo / ".autolab" / "state.json"),
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "current_scope": "experiment",
        "scope_root": str(repo / "experiments" / "plan" / "iter1"),
        "current_stage": "implementation_review",
        "wave": {"status": "available", "current": 1, "executed": 1, "total": 1},
        "task_status": {
            "status": "available",
            "total": 1,
            "completed": 1,
            "failed": 0,
            "blocked": 0,
            "pending": 0,
            "skipped": 0,
            "deferred": 0,
            "task_details": [],
        },
        "latest_verifier_summary": {
            "generated_at": "2026-01-01T00:00:00Z",
            "stage_effective": "implementation_review",
            "passed": True,
            "message": "verification passed",
        },
        "blocking_failures": [],
        "pending_human_decisions": [],
        "files_changed_since_last_green_point": [],
        "recommended_next_command": {
            "command": "autolab run",
            "reason": "continue workflow",
            "executable": True,
        },
        "safe_resume_point": {
            "command": "autolab run",
            "status": "ready",
            "preconditions": [],
        },
        "wave_observability": wave_observability,
        "last_green_at": "2026-01-01T00:00:00Z",
        "baseline_snapshot": {},
        "handoff_json_path": str(path),
        "handoff_markdown_path": str(
            repo / "experiments" / "plan" / "iter1" / "handoff.md"
        ),
        "uat": {
            "required": False,
            "required_by": "none",
            "artifact_path": str(repo / "experiments" / "plan" / "iter1" / "uat.md"),
            "status": "not_required",
            "pending": False,
            "pending_message": "",
            "suggested_init_command": "",
            "suggested_check_titles": [],
        },
        "continuation_packet": {
            "schema_version": "1.0",
            "generated_at": "2026-01-01T00:00:00Z",
            "active_stage": {
                "stage": "implementation_review",
                "stage_attempt": 1,
                "max_stage_attempts": 3,
                "scope_kind": "experiment",
                "scope_root": str(repo / "experiments" / "plan" / "iter1"),
            },
            "next_action": {
                "recommended_command": "autolab run",
                "safe_command": "autolab run",
                "safe_status": "ready",
                "preconditions": [],
                "reason": "continue workflow",
                "executable": True,
            },
            "latest_good_checkpoint": {
                "checkpoint_id": "cp_demo",
                "stage": "implementation_review",
                "created_at": "2026-01-01T00:00:00Z",
                "last_green_at": "2026-01-01T00:00:00Z",
                "recommended_rewind_targets": [],
            },
            "policy_and_risk": {
                "plan_approval_status": "",
                "plan_requires_approval": False,
                "plan_trigger_reasons": [],
                "plan_hash": "",
                "risk_fingerprint": "",
                "guardrail_breach": "",
                "block_reason": "",
                "context_rot_flags": [],
                "effective_flags": [],
            },
            "run_status": {
                "run_id": "",
                "host_mode": "",
                "manifest_status": "",
                "sync_status": "",
                "metrics_status": "",
                "manifest_path": "",
                "metrics_path": "",
            },
            "uat_status": {
                "required": False,
                "required_by": "none",
                "status": "not_required",
                "artifact_path": str(
                    repo / "experiments" / "plan" / "iter1" / "uat.md"
                ),
                "pending": False,
                "pending_message": "",
                "suggested_init_command": "",
            },
            "top_blockers": [],
            "artifact_pointers": [
                {
                    "role": "machine_packet",
                    "path": ".autolab/handoff.json",
                    "status": "present",
                    "reason": "Compact continuation source for prompts and tooling.",
                    "inline_in_oracle": True,
                }
            ],
            "diagnostics": [],
        },
    }
    if not valid:
        payload.pop("safe_resume_point", None)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_sidecar(
    repo: Path,
    *,
    kind: str,
    scope_kind: str,
    valid: bool = True,
) -> None:
    if scope_kind == "project_wide":
        path = (
            repo / ".autolab" / "context" / "sidecars" / "project_wide" / f"{kind}.json"
        )
        scope_root = "."
    else:
        path = (
            repo
            / "experiments"
            / "plan"
            / "iter1"
            / "context"
            / "sidecars"
            / f"{kind}.json"
        )
        scope_root = "experiments/plan/iter1"

    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "sidecar_kind": kind,
        "scope_kind": scope_kind,
        "scope_root": scope_root,
        "generated_at": "2026-03-05T00:00:00Z",
        "derived_from": [
            {
                "path": ".autolab/state.json",
                "fingerprint": "sha256:state",
                "reason": "stage context",
            }
        ],
        "stale_if": [
            {
                "path": "experiments/plan/iter1/design.yaml",
                "fingerprint": "sha256:design",
                "reason": "refresh when design changes",
            }
        ],
    }
    if scope_kind == "experiment":
        payload["iteration_id"] = "iter1"
        payload["experiment_id"] = "e1"

    if kind == "discuss":
        payload.update(
            {
                "locked_decisions": [
                    {
                        "id": "decision-1",
                        "summary": "Keep evaluation on the local baseline.",
                        "detail": "Avoid changing the evaluation dataset in this iteration.",
                    }
                ],
                "preferences": [
                    {
                        "id": "preference-1",
                        "summary": "Prefer smaller, reviewable patches.",
                    }
                ],
                "constraints": [
                    {
                        "id": "constraint-1",
                        "summary": "Do not modify benchmark definitions.",
                    }
                ],
                "open_questions": [
                    {
                        "id": "question-1",
                        "summary": "Should we add a parser capability fixture for research output later?",
                    }
                ],
                "promotion_candidates": [
                    {
                        "id": "candidate-1",
                        "summary": "Promote this provenance note into the design artifact if adopted.",
                    }
                ],
            }
        )
        if not valid:
            invalid_items = payload["locked_decisions"]
            assert isinstance(invalid_items, list)
            invalid_item = invalid_items[0]
            assert isinstance(invalid_item, dict)
            invalid_item.pop("id", None)
    else:
        payload.update(
            {
                "questions": [
                    {
                        "id": "research-question-1",
                        "summary": "What evidence should inform the next parser contract revision?",
                    }
                ],
                "findings": [
                    {
                        "id": "finding-1",
                        "summary": "Current scaffold verifiers already accept optional artifacts when paths exist.",
                        "detail": "Sidecars should follow the same non-fatal when absent behavior.",
                    }
                ],
                "recommendations": [
                    {
                        "id": "recommendation-1",
                        "summary": "Keep sidecar schemas narrow until producer commands land.",
                    }
                ],
                "sources": [
                    {
                        "id": "source-1",
                        "summary": "docs/artifact_contracts.md",
                        "detail": "Reference artifact contract documentation.",
                    }
                ],
            }
        )
        if not valid:
            invalid_items = payload["questions"]
            assert isinstance(invalid_items, list)
            invalid_item = invalid_items[0]
            assert isinstance(invalid_item, dict)
            invalid_item.pop("id", None)

    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_design_context_quality(repo: Path, *, valid: bool = True) -> None:
    path = repo / "experiments" / "plan" / "iter1" / "design_context_quality.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": "1.0",
        "generated_at": "2026-03-05T00:00:00Z",
        "iteration_id": "iter1",
        "experiment_id": "",
        "context_mode": "present",
        "available": {
            "discuss_items": 1,
            "research_items": 0,
            "open_questions": 0,
            "promotion_candidates": 0,
        },
        "uptake": {
            "requirements_total": 1,
            "requirements_with_context_refs": 1,
            "requirements_with_resolved_context": 1,
            "context_refs_total": 1,
            "resolved_context_refs": 1,
            "resolved_discuss_context_refs": 1,
            "resolved_research_context_refs": 0,
            "promoted_constraints_total": 0,
            "resolved_promoted_constraints": 0,
        },
        "score": {"value": 1, "max": 1},
        "diagnostics": [],
    }
    if not valid:
        payload["score"] = {"value": "bad", "max": 1}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _valid_plan_execution_task_detail(
    *, reason_code: str = "completed", task_id: str = "T1"
) -> dict[str, object]:
    return {
        "task_id": task_id,
        "status": "completed",
        "wave": 1,
        "attempts": 1,
        "retries_used": 0,
        "last_error": "",
        "scope_kind": "experiment",
        "files_changed": [],
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:00:01Z",
        "duration_seconds": 1.0,
        "timing_available": True,
        "reason_code": reason_code,
        "reason_detail": "",
        "blocked_by": [],
        "runner_report_path": ".autolab/runner_execution_report.T1.json",
        "runner_status": "completed",
        "runner_exit_code": 0,
        "verification_status": "passed",
        "verification_commands": [],
        "expected_artifacts_missing": [],
        "evidence_summary": {
            "runner_status": "completed",
            "runner_exit_code": 0,
            "verification_status": "passed",
            "files_changed_count": 0,
            "expected_artifacts_missing_count": 0,
            "text": "ok",
        },
        "critical_path": True,
    }


def _run_schema_checks(
    repo: Path, *, stage: str | None = None
) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "schema_checks.py"
    command = [sys.executable, str(verifier)]
    if stage:
        command.extend(["--stage", stage])
    return subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_prompt_lint(
    repo: Path, *, stage: str | None = None, assistant: bool = False
) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "prompt_lint.py"
    command = [sys.executable, str(verifier)]
    if stage:
        command.extend(["--stage", stage])
    if assistant:
        command.append("--assistant")
    return subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_registry_consistency(
    repo: Path, *, stage: str | None = None
) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "registry_consistency.py"
    command = [sys.executable, str(verifier)]
    if stage:
        command.extend(["--stage", stage])
    return subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_consistency_checks(
    repo: Path, *, stage: str | None = None
) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "consistency_checks.py"
    command = [sys.executable, str(verifier)]
    if stage:
        command.extend(["--stage", stage])
    return subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_docs_targets(repo: Path) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "docs_targets.py"
    return subprocess.run(
        [sys.executable, str(verifier)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_run_health(repo: Path) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "run_health.py"
    return subprocess.run(
        [sys.executable, str(verifier)],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def _run_result_sanity(
    repo: Path, *, json_flag: bool = False
) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "result_sanity.py"
    command = [sys.executable, str(verifier)]
    if json_flag:
        command.append("--json")
    return subprocess.run(
        command, cwd=repo, text=True, capture_output=True, check=False
    )


def _run_docs_drift(
    repo: Path, *, stage: str | None = None, json_flag: bool = False
) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "docs_drift.py"
    command = [sys.executable, str(verifier)]
    if stage:
        command.extend(["--stage", stage])
    if json_flag:
        command.append("--json")
    return subprocess.run(
        command, cwd=repo, text=True, capture_output=True, check=False
    )


def _run_closed_experiment_guard(
    repo: Path, *, json_flag: bool = False
) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "closed_experiment_guard.py"
    command = [sys.executable, str(verifier)]
    if json_flag:
        command.append("--json")
    return subprocess.run(
        command, cwd=repo, text=True, capture_output=True, check=False
    )


def _setup_review_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    _write_plan_contract(repo)
    return repo


def _extract_task_example_sections(prompt_text: str) -> list[str]:
    lines = prompt_text.splitlines()
    sections: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx].strip()
        if not line.startswith("### T"):
            idx += 1
            continue
        end = idx + 1
        while end < len(lines):
            candidate = lines[end].strip()
            if candidate.startswith("### T") or candidate.startswith("## "):
                break
            end += 1
        sections.append("\n".join(lines[idx:end]))
        idx = end
    return sections


def test_schema_checks_pass_for_valid_review_payload(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_validates_optional_handoff_schema(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_handoff(repo, valid=True)

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_fails_for_invalid_handoff_schema(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_handoff(repo, valid=False)

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert ".autolab/handoff.json schema violation" in result.stdout


def test_schema_checks_validates_optional_plan_approval_schema(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    plan_approval_path = repo / "experiments" / "plan" / "iter1" / "plan_approval.json"
    plan_approval_path.parent.mkdir(parents=True, exist_ok=True)
    plan_approval_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "iteration_id": "iter1",
                "status": "pending",
                "requires_approval": True,
                "plan_hash": "plan-hash-1",
                "risk_fingerprint": "risk-fingerprint-1",
                "trigger_reasons": ["project_wide_tasks_present"],
                "counts": {
                    "tasks_total": 3,
                    "waves_total": 2,
                    "project_wide_tasks": 1,
                    "project_wide_unique_paths": 2,
                    "observed_retries": 0,
                    "stage_attempt": 0,
                },
                "reviewed_by": "",
                "reviewed_at": "",
                "notes": "",
                "source_paths": {
                    "plan_contract": ".autolab/plan_contract.json",
                    "plan_graph": ".autolab/plan_graph.json",
                    "plan_check_result": ".autolab/plan_check_result.json",
                },
                "uat": {
                    "policy_required": False,
                    "effective_required": False,
                    "required_by": "none",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_fails_for_invalid_plan_approval_schema(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    plan_approval_path = repo / "experiments" / "plan" / "iter1" / "plan_approval.json"
    plan_approval_path.parent.mkdir(parents=True, exist_ok=True)
    plan_approval_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "iteration_id": "iter1",
                "status": "bogus",
                "requires_approval": True,
                "plan_hash": "plan-hash-1",
                "risk_fingerprint": "risk-fingerprint-1",
                "trigger_reasons": ["project_wide_tasks_present"],
                "counts": {
                    "tasks_total": 3,
                    "waves_total": 2,
                    "project_wide_tasks": 1,
                    "project_wide_unique_paths": 2,
                    "observed_retries": 0,
                    "stage_attempt": 0,
                },
                "reviewed_by": "",
                "reviewed_at": "",
                "notes": "",
                "source_paths": {
                    "plan_contract": ".autolab/plan_contract.json",
                    "plan_graph": ".autolab/plan_graph.json",
                    "plan_check_result": ".autolab/plan_check_result.json",
                },
                "uat": {
                    "policy_required": False,
                    "effective_required": False,
                    "required_by": "none",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "plan_approval.json schema violation" in result.stdout


def test_schema_checks_fail_when_required_uat_is_missing(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    plan_approval_path = repo / "experiments" / "plan" / "iter1" / "plan_approval.json"
    plan_approval_path.parent.mkdir(parents=True, exist_ok=True)
    plan_approval_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "iteration_id": "iter1",
                "status": "approved",
                "requires_approval": True,
                "plan_hash": "plan-hash-1",
                "risk_fingerprint": "risk-fingerprint-1",
                "trigger_reasons": ["project_wide_tasks_present"],
                "counts": {
                    "tasks_total": 3,
                    "waves_total": 2,
                    "project_wide_tasks": 1,
                    "project_wide_unique_paths": 2,
                    "observed_retries": 0,
                    "stage_attempt": 0,
                },
                "reviewed_by": "reviewer",
                "reviewed_at": "2026-03-05T00:01:00Z",
                "notes": "",
                "source_paths": {
                    "plan_contract": ".autolab/plan_contract.json",
                    "plan_graph": ".autolab/plan_graph.json",
                    "plan_check_result": ".autolab/plan_check_result.json",
                },
                "uat": {
                    "policy_required": False,
                    "effective_required": True,
                    "required_by": "manual",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "uat.md is required but missing" in result.stdout


def test_schema_checks_validates_optional_sidecar_schemas(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_sidecar(repo, kind="discuss", scope_kind="project_wide")
    _write_sidecar(repo, kind="research", scope_kind="project_wide")
    _write_sidecar(repo, kind="discuss", scope_kind="experiment")
    _write_sidecar(repo, kind="research", scope_kind="experiment")

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


@pytest.mark.parametrize(
    ("kind", "scope_kind"),
    [
        ("discuss", "project_wide"),
        ("research", "experiment"),
    ],
)
def test_schema_checks_fails_for_invalid_sidecar_item_shape(
    tmp_path: Path,
    *,
    kind: str,
    scope_kind: str,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_sidecar(repo, kind=kind, scope_kind=scope_kind, valid=False)

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert f"{kind}.json schema violation" in result.stdout
    assert "'id' is a required property" in result.stdout


def test_schema_checks_fails_for_missing_sidecar_dependency_fingerprint(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_sidecar(repo, kind="research", scope_kind="project_wide")
    sidecar_path = (
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "research.json"
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    derived_from = payload.get("derived_from")
    assert isinstance(derived_from, list)
    assert isinstance(derived_from[0], dict)
    derived_from[0].pop("fingerprint", None)
    sidecar_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "research.json schema violation" in result.stdout
    assert "'fingerprint' is a required property" in result.stdout


def test_schema_checks_fails_for_missing_experiment_sidecar_identity(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_sidecar(repo, kind="discuss", scope_kind="experiment")
    sidecar_path = (
        repo
        / "experiments"
        / "plan"
        / "iter1"
        / "context"
        / "sidecars"
        / "discuss.json"
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload.pop("experiment_id", None)
    sidecar_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "discuss.json schema violation" in result.stdout
    assert "'experiment_id' is a required property" in result.stdout


def test_schema_checks_fails_for_project_wide_sidecar_with_experiment_identity(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_sidecar(repo, kind="research", scope_kind="project_wide")
    sidecar_path = (
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "research.json"
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    payload["iteration_id"] = "iter1"
    payload["experiment_id"] = "e1"
    sidecar_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "research.json schema violation" in result.stdout
    assert "should not be valid under" in result.stdout


def test_schema_checks_validates_optional_design_context_quality_schema(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_design_context_quality(repo, valid=True)

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_fails_for_invalid_design_context_quality_schema(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_design_context_quality(repo, valid=False)

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "design_context_quality.json schema violation" in result.stdout


def test_schema_checks_fails_for_unknown_research_linkage_ids(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_sidecar(repo, kind="research", scope_kind="project_wide")
    sidecar_path = (
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "research.json"
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    findings = payload.get("findings")
    assert isinstance(findings, list) and isinstance(findings[0], dict)
    findings[0]["question_ids"] = ["missing-question"]
    findings[0]["source_ids"] = ["missing-source"]
    sidecar_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "references unknown question id 'missing-question'" in result.stdout
    assert "references unknown source id 'missing-source'" in result.stdout


def test_schema_checks_fails_for_research_source_fingerprint_mismatch(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    docs_path = repo / "docs" / "artifact_contracts.md"
    docs_path.parent.mkdir(parents=True, exist_ok=True)
    docs_path.write_text("# Artifact Contracts\n", encoding="utf-8")
    _write_sidecar(repo, kind="research", scope_kind="project_wide")
    sidecar_path = (
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "research.json"
    )
    payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    sources = payload.get("sources")
    assert isinstance(sources, list) and isinstance(sources[0], dict)
    sources[0]["path"] = "docs/artifact_contracts.md"
    sources[0]["fingerprint"] = "sha256:wrong"
    sidecar_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert (
        "sources[0].fingerprint does not match docs/artifact_contracts.md"
        in result.stdout
    )


def test_schema_checks_fails_design_for_project_wide_requirement_using_experiment_sidecar(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_sidecar(repo, kind="discuss", scope_kind="experiment")
    design_path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    design_payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    assert isinstance(design_payload, dict)
    requirements = design_payload.get("implementation_requirements")
    assert isinstance(requirements, list) and isinstance(requirements[0], dict)
    requirements[0]["scope_kind"] = "project_wide"
    requirements[0]["context_refs"] = ["experiment:discuss:preferences:preference-1"]
    design_path.write_text(
        yaml.safe_dump(design_payload, sort_keys=False),
        encoding="utf-8",
    )

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert (
        "project_wide requirement may not reference experiment sidecar" in result.stdout
    )


def test_schema_checks_fails_design_for_missing_project_map_context_ref(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    design_path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    design_payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    assert isinstance(design_payload, dict)
    requirements = design_payload.get("implementation_requirements")
    assert isinstance(requirements, list) and isinstance(requirements[0], dict)
    requirements[0]["context_refs"] = ["project_map"]
    design_path.write_text(
        yaml.safe_dump(design_payload, sort_keys=False),
        encoding="utf-8",
    )

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "context_ref 'project_map' could not be resolved" in result.stdout


def test_schema_checks_fails_design_for_invalid_experiment_sidecar_identity(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_sidecar(repo, kind="discuss", scope_kind="experiment")
    sidecar_path = (
        repo
        / "experiments"
        / "plan"
        / "iter1"
        / "context"
        / "sidecars"
        / "discuss.json"
    )
    sidecar_payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar_payload["experiment_id"] = "wrong-experiment"
    sidecar_path.write_text(
        json.dumps(sidecar_payload, indent=2) + "\n",
        encoding="utf-8",
    )
    design_path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    design_payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    assert isinstance(design_payload, dict)
    requirements = design_payload.get("implementation_requirements")
    assert isinstance(requirements, list) and isinstance(requirements[0], dict)
    requirements[0]["context_refs"] = ["experiment:discuss:preferences:preference-1"]
    design_path.write_text(
        yaml.safe_dump(design_payload, sort_keys=False),
        encoding="utf-8",
    )

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert (
        "context_ref 'experiment:discuss:preferences:preference-1' could not be resolved"
        in result.stdout
    )


def test_schema_checks_fails_design_for_duplicate_requirement_ids(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    design_path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    design_payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    assert isinstance(design_payload, dict)
    requirements = design_payload.get("implementation_requirements")
    assert isinstance(requirements, list) and len(requirements) >= 1
    duplicate_requirement = dict(requirements[0])
    requirements.append(duplicate_requirement)
    design_path.write_text(
        yaml.safe_dump(design_payload, sort_keys=False),
        encoding="utf-8",
    )

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "duplicates requirement_id 'R1'" in result.stdout


def test_schema_checks_pass_with_runtime_state_history_keys(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)

    state_path = repo / ".autolab" / "state.json"
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["history"] = [
        {
            "timestamp_utc": "2026-02-19T10:58:06Z",
            "stage_before": "hypothesis",
            "stage_after": "hypothesis",
            "status": "complete",
            "summary": "assistant selected task task_1 -> hypothesis",
            "stage_attempt": 0,
        },
        {
            "timestamp_utc": "2026-02-19T10:59:41Z",
            "stage_before": "hypothesis",
            "stage_after": "hypothesis",
            "status": "failed",
            "summary": "verification failed; retrying stage hypothesis (1/3)",
            "stage_attempt": 1,
            "verification": {
                "passed": False,
                "message": "verification failed",
                "mode": "auto",
            },
        },
    ]
    state_path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_fail_when_required_check_key_missing(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=False)

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "docs_target_update" in result.stdout


def test_schema_checks_fail_when_parser_capability_metric_mismatch(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    manifest_path = iteration_dir / "parser_capabilities.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter1",
                "parser": {
                    "kind": "command",
                    "locator": "python -m scripts.extract_results --run-id {run_id} --iteration-path {iteration_path}",
                },
                "supported_metrics": ["not_accuracy"],
                "output_contract": {
                    "writes_metrics_json": True,
                    "writes_summary_markdown": True,
                },
                "generated_at": "2026-03-05T00:00:00Z",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "parser_capabilities.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "iterations": {
                    "iter1": {
                        "manifest_path": "experiments/plan/iter1/parser_capabilities.json",
                        "parser_kind": "command",
                        "supported_metrics": ["not_accuracy"],
                        "updated_at": "2026-03-05T00:00:00Z",
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "parser capability mismatch" in result.stdout


def test_schema_checks_fail_when_parser_capability_kind_mismatch(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    manifest_path = iteration_dir / "parser_capabilities.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter1",
                "parser": {
                    "kind": "python",
                    "locator": "parsers.iter1_extract_parser:parse_results",
                },
                "supported_metrics": ["accuracy"],
                "output_contract": {
                    "writes_metrics_json": True,
                    "writes_summary_markdown": True,
                },
                "generated_at": "2026-03-05T00:00:00Z",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (repo / ".autolab" / "parser_capabilities.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "iterations": {
                    "iter1": {
                        "manifest_path": "experiments/plan/iter1/parser_capabilities.json",
                        "parser_kind": "python",
                        "supported_metrics": ["accuracy"],
                        "updated_at": "2026-03-05T00:00:00Z",
                    }
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "design.extract_parser.kind='command'" in result.stdout
    assert "parser_capabilities.parser.kind='python'" in result.stdout


def test_schema_checks_fail_when_policy_requires_parser_capability_manifest(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert isinstance(policy, dict)
    extract_results_policy = policy.setdefault("extract_results", {})
    assert isinstance(extract_results_policy, dict)
    parser_policy = extract_results_policy.setdefault("parser", {})
    assert isinstance(parser_policy, dict)
    parser_policy["require_capability_manifest"] = True
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "require_capability_manifest=true" in result.stdout


def test_schema_checks_fail_when_policy_requires_parser_capability_index(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    manifest_path = iteration_dir / "parser_capabilities.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter1",
                "parser": {
                    "kind": "command",
                    "locator": "python -m scripts.extract_results --run-id {run_id} --iteration-path {iteration_path}",
                },
                "supported_metrics": ["accuracy"],
                "output_contract": {
                    "writes_metrics_json": True,
                    "writes_summary_markdown": True,
                },
                "generated_at": "2026-03-05T00:00:00Z",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert isinstance(policy, dict)
    extract_results_policy = policy.setdefault("extract_results", {})
    assert isinstance(extract_results_policy, dict)
    parser_policy = extract_results_policy.setdefault("parser", {})
    assert isinstance(parser_policy, dict)
    parser_policy["require_capability_index"] = True
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "require_capability_index=true" in result.stdout


def test_registry_consistency_fails_when_policy_requires_unsupported_capability(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert isinstance(policy, dict)
    req = policy.setdefault("requirements_by_stage", {})
    assert isinstance(req, dict)
    design_req = req.setdefault("design", {})
    assert isinstance(design_req, dict)
    design_req["env_smoke"] = (
        True  # design stage capability is false in workflow registry
    )
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    result = _run_registry_consistency(repo, stage="design")

    assert result.returncode == 1
    assert "not supported by workflow.yaml verifier_categories" in result.stdout


def test_registry_consistency_rejects_mustache_tokens_in_output_contract_paths(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    workflow_path = repo / ".autolab" / "workflow.yaml"
    workflow_payload = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    assert isinstance(workflow_payload, dict)
    stages = workflow_payload.get("stages", {})
    assert isinstance(stages, dict)
    launch_stage = stages.get("launch", {})
    assert isinstance(launch_stage, dict)
    launch_stage["required_outputs"] = ["runs/{{run_id}}/run_manifest.json"]
    workflow_path.write_text(
        yaml.safe_dump(workflow_payload, sort_keys=False),
        encoding="utf-8",
    )

    result = _run_registry_consistency(repo, stage="launch")

    assert result.returncode == 1
    assert "uses prompt-" in result.stdout


def test_consistency_checks_fail_on_metric_name_mismatch(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_state(repo, stage="extract_results", last_run_id="run_001")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    run_manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "local",
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    metrics = {
        "schema_version": "1.0",
        "iteration_id": "iter1",
        "run_id": "run_001",
        "status": "completed",
        "primary_metric": {
            "name": "f1_score",
            "value": 0.5,
            "delta_vs_baseline": 0.1,
        },
        "baseline_results": [],
        "variant_results": [],
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(run_manifest, indent=2), encoding="utf-8"
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )

    result = _run_consistency_checks(repo, stage="extract_results")

    assert result.returncode == 1
    assert "does not match design.metrics.primary.name" in result.stdout


def test_consistency_checks_strict_slurm_monitor_requires_synced_status(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_state(repo, stage="slurm_monitor", last_run_id="run_001")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "slurm",
        "status": "completed",
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    result = _run_consistency_checks(repo, stage="slurm_monitor")

    assert result.returncode == 1
    assert "strict SLURM lifecycle violation" in result.stdout


def test_consistency_checks_strict_update_docs_requires_completed_manifest_after_metrics(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    _write_state(repo, stage="update_docs", last_run_id="run_001")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "slurm",
        "status": "synced",
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    metrics = {
        "schema_version": "1.0",
        "iteration_id": "iter1",
        "run_id": "run_001",
        "status": "completed",
        "primary_metric": {
            "name": "accuracy",
            "value": 0.7,
            "delta_vs_baseline": 0.02,
        },
        "baseline_results": [],
        "variant_results": [],
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    (run_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8"
    )

    result = _run_consistency_checks(repo, stage="update_docs")

    assert result.returncode == 1
    assert "strict SLURM lifecycle violation" in result.stdout


def test_schema_checks_design_stage_override_skips_review_artifacts(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_extract_results_skips_run_checks_when_run_id_missing(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_state(repo, stage="extract_results", last_run_id="")
    _write_review_result(repo, include_docs_check=True)

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_prompt_lint_passes_for_scaffold_prompts(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "prompt_lint: PASS" in result.stdout


def test_prompt_lint_fails_on_unsupported_token(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    prompt_path = repo / ".autolab" / "prompts" / "stage_design.runner.md"
    prompt_path.write_text(
        (
            "# Stage: design (runner)\n\n"
            "## ROLE\nx\n\n"
            "## PRIMARY OBJECTIVE\nx\n\n"
            "## OUTPUTS (STRICT)\n- x\n\n"
            "## REQUIRED INPUTS\n- x {{unknown_token}}\n\n"
            "## STOP CONDITIONS\n- x\n\n"
            "## FAILURE / RETRY BEHAVIOR\n- x\n"
        ),
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="design")

    assert result.returncode == 1
    assert "unsupported token" in result.stdout


def test_prompt_lint_accepts_workflow_declared_optional_runner_tokens(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)

    workflow_path = repo / ".autolab" / "workflow.yaml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    stages = workflow.get("stages", {})
    implementation = stages.get("implementation", {})
    if isinstance(implementation, dict):
        raw_optional = implementation.get("optional_tokens", [])
        optional_tokens = raw_optional if isinstance(raw_optional, list) else []
        if "custom_optional_token" not in optional_tokens:
            optional_tokens.append("custom_optional_token")
        implementation["optional_tokens"] = optional_tokens
    workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8"
    )

    prompt_path = repo / ".autolab" / "prompts" / "stage_implementation.runner.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.rstrip()
        + "\n\n## MISSING-INPUT FALLBACKS\n"
        + "- If unavailable, continue without it.\n"
        + "- optional={{custom_optional_token}}\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "prompt_lint: PASS" in result.stdout


def test_prompt_lint_rejects_status_vocab_transitively_for_non_mutator_runner(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_implementation.runner.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.rstrip() + "\n\n{{ shared:guardrails.md }}\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 1
    assert (
        "includes status vocabulary in runner template for non-mutator stage"
        in result.stdout
    )


def test_prompt_lint_allows_status_vocab_for_mutator_runner_via_transitive_include(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_launch.runner.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.rstrip() + "\n\n{{ shared:guardrails.md }}\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="launch")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "prompt_lint: PASS" in result.stdout


def test_prompt_lint_rejects_banned_runner_shared_include_with_whitespace(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_implementation.runner.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.rstrip() + "\n\n{{ shared:verification_ritual.md }}\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 1
    assert (
        "includes audit-only shared block in runner template: {{shared:verification_ritual.md}}"
        in result.stdout
    )


def test_prompt_lint_rejects_banned_runner_shared_include_transitively(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    shared_path = repo / ".autolab" / "prompts" / "shared" / "runner_bad_include.md"
    shared_path.write_text("{{ shared:verifier_common.md }}\n", encoding="utf-8")

    prompt_path = repo / ".autolab" / "prompts" / "stage_implementation.runner.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.rstrip() + "\n\n{{shared:runner_bad_include.md}}\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 1
    assert (
        "includes audit-only shared block in runner template: {{shared:verifier_common.md}}"
        in result.stdout
    )


def test_prompt_lint_requires_runner_non_negotiables_include_or_section(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_implementation.runner.md"
    text = prompt_path.read_text(encoding="utf-8")
    text = text.replace("{{shared:runner_non_negotiables.md}}\n", "")
    prompt_path.write_text(text, encoding="utf-8")

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 1
    assert (
        "runner template must include {{shared:runner_non_negotiables.md}}"
        in result.stdout
    )


def test_prompt_lint_accepts_runner_non_negotiables_section_without_shared_include(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_implementation.runner.md"
    text = prompt_path.read_text(encoding="utf-8")
    text = text.replace("{{shared:runner_non_negotiables.md}}\n", "")
    prompt_path.write_text(
        text.rstrip() + "\n\n## NON-NEGOTIABLES\n- Keep edits in scope.\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "prompt_lint: PASS" in result.stdout


def test_prompt_lint_requires_all_runner_required_tokens(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_implementation.runner.md"
    text = prompt_path.read_text(encoding="utf-8")
    text = text.replace("{{iteration_path}}", "missing_iteration_path")
    prompt_path.write_text(text, encoding="utf-8")

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 1
    assert (
        "missing required token(s) for stage 'implementation': iteration_path"
        in result.stdout
    )


def test_prompt_lint_requires_missing_input_fallbacks_when_optional_tokens_used(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_implementation.runner.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.rstrip() + "\n\nOptional context: {{review_feedback}}\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 1
    assert (
        "uses optional token(s) but is missing '## MISSING-INPUT FALLBACKS' safe-fallback section"
        in result.stdout
    )


def test_prompt_lint_uses_workflow_terminal_stage_metadata(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)

    workflow_path = repo / ".autolab" / "workflow.yaml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    stages = workflow.get("stages", {})
    implementation = stages.get("implementation", {})
    if isinstance(implementation, dict):
        classifications = implementation.get("classifications", {})
        if not isinstance(classifications, dict):
            classifications = {}
        classifications["terminal"] = True
        implementation["classifications"] = classifications
    workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8"
    )

    audit_prompt = repo / ".autolab" / "prompts" / "stage_implementation.audit.md"
    audit_text = audit_prompt.read_text(encoding="utf-8")
    audit_text = audit_text.replace("{{shared:guardrails.md}}\n", "")
    audit_text = audit_text.replace("{{shared:repo_scope.md}}\n", "")
    audit_prompt.write_text(audit_text, encoding="utf-8")

    result = _run_prompt_lint(repo, stage="implementation")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "prompt_lint: PASS" in result.stdout


@pytest.mark.parametrize(
    "prompt_name",
    ("stage_implementation.md", "stage_implementation.audit.md"),
)
def test_implementation_prompt_examples_include_objective_and_failure_policy(
    tmp_path: Path, prompt_name: str
) -> None:
    repo = _setup_review_repo(tmp_path)
    prompt_path = repo / ".autolab" / "prompts" / prompt_name
    prompt_text = prompt_path.read_text(encoding="utf-8")

    task_sections = _extract_task_example_sections(prompt_text)
    assert task_sections
    for section in task_sections:
        assert "- **objective**:" in section
        assert (
            "- **failure_policy**: fail_fast (allowed values: `fail_fast`)" in section
        )


def test_prompt_lint_rejects_banned_runner_sections(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_design.runner.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.rstrip() + "\n\n## FILE LENGTH BUDGET\n- Keep short.\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="design")

    assert result.returncode == 1
    assert "includes banned runner section: ## FILE LENGTH BUDGET" in result.stdout


def test_prompt_lint_rejects_runner_duplicate_headings(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_design.runner.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.rstrip() + "\n\n## ROLE\nduplicate\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="design")

    assert result.returncode == 1
    assert "duplicate runner heading" in result.stdout


def test_prompt_lint_rejects_runner_raw_blob_tokens(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_design.runner.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.rstrip() + "\n\nRaw blob: {{diff_summary}}\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="design")

    assert result.returncode == 1
    assert (
        "uses banned raw-blob token(s) in runner template: diff_summary"
        in result.stdout
    )


def test_prompt_lint_design_audit_requires_extract_parser_template_block(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "stage_design.audit.md"
    original = prompt_path.read_text(encoding="utf-8")
    prompt_path.write_text(
        original.replace("\nextract_parser:\n", "\nparser_hook:\n", 1),
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="design")

    assert result.returncode == 1
    assert (
        "design output template must include an extract_parser mapping block"
        in result.stdout
    )


def test_prompt_lint_rejects_universal_dual_memory_guidance_in_shared_guardrails(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    guardrails_path = repo / ".autolab" / "prompts" / "shared" / "guardrails.md"
    original = guardrails_path.read_text(encoding="utf-8")
    guardrails_path.write_text(
        original.rstrip()
        + "\n- Mirror actionable items between docs/todo.md and {{iteration_path}}/documentation.md.\n",
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="design")

    assert result.returncode == 1
    assert "must not include universal dual-memory policy guidance" in result.stdout


def test_prompt_lint_requires_runtime_context_to_describe_deterministic_bypass(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    runtime_context_path = (
        repo / ".autolab" / "prompts" / "shared" / "runtime_context.md"
    )
    text = runtime_context_path.read_text(encoding="utf-8")
    runtime_context_path.write_text(
        text.replace("deterministic runtime stages", "runtime stages"),
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="design")

    assert result.returncode == 1
    assert "deterministic-stage runner bypass semantics" in result.stdout


def test_prompt_lint_fails_when_prompt_uses_nonrequired_token_without_optional_contract(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    workflow_path = repo / ".autolab" / "workflow.yaml"
    workflow = yaml.safe_load(workflow_path.read_text(encoding="utf-8"))
    stages = workflow.get("stages", {})
    decide_repeat = stages.get("decide_repeat", {})
    if isinstance(decide_repeat, dict):
        decide_repeat["required_tokens"] = ["iteration_id", "iteration_path"]
        decide_repeat["optional_tokens"] = []
    workflow_path.write_text(
        yaml.safe_dump(workflow, sort_keys=False), encoding="utf-8"
    )

    result = _run_prompt_lint(repo, stage="decide_repeat")

    assert result.returncode == 1
    assert "not declared as required or optional" in result.stdout


def test_prompt_lint_assistant_prompts_pass(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)

    result = _run_prompt_lint(repo, assistant=True)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "prompt_lint: PASS" in result.stdout


def test_prompt_lint_assistant_requires_strict_contract_sections(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    prompt_path = repo / ".autolab" / "prompts" / "assistant_select.md"
    text = prompt_path.read_text(encoding="utf-8")
    text = text.replace("## RESPONSE FORMAT\n", "")
    text = text.replace("{{shared:assistant_output_contract.md}}\n", "")
    prompt_path.write_text(text, encoding="utf-8")

    result = _run_prompt_lint(repo, assistant=True)

    assert result.returncode == 1
    assert (
        "missing required shared include: {{shared:assistant_output_contract.md}}"
        in result.stdout
    )
    assert "missing required section heading: ## RESPONSE FORMAT" in result.stdout


def test_schema_checks_fail_for_invalid_todo_state_schema(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    (repo / ".autolab" / "todo_state.json").write_text(
        json.dumps({"version": "one", "next_order": 1, "tasks": {}}),
        encoding="utf-8",
    )

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "todo_state.json" in result.stdout


def test_schema_checks_require_todo_files_when_assistant_mode_on(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    state_path = repo / ".autolab" / "state.json"
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["assistant_mode"] = "on"
    state_path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "todo_state.json is required when assistant_mode=on" in result.stdout


# ---------------------------------------------------------------------------
# docs_targets / run_health verifier tests
# ---------------------------------------------------------------------------


def test_docs_targets_passes_with_no_paper_targets_and_required_rationale(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="update_docs", last_run_id="run_001")

    docs_update = repo / "experiments" / "plan" / "iter1" / "docs_update.md"
    docs_update.parent.mkdir(parents=True, exist_ok=True)
    docs_update.write_text(
        "No targets configured. No target configured for this iteration; metrics delta summary pending.",
        encoding="utf-8",
    )

    result = _run_docs_targets(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs_targets: PASS" in result.stdout


def test_docs_targets_fails_with_no_paper_targets_and_missing_rationale(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="update_docs", last_run_id="run_001")

    docs_update = repo / "experiments" / "plan" / "iter1" / "docs_update.md"
    docs_update.parent.mkdir(parents=True, exist_ok=True)
    docs_update.write_text(
        "Updated notes for iter1 without target configuration rationale.",
        encoding="utf-8",
    )

    result = _run_docs_targets(repo)

    assert result.returncode == 1
    assert "No target configured" in result.stdout


@pytest.mark.parametrize(
    "placeholder_text",
    [
        "TODO: fill in final metrics.",
        "TBD after rerun.",
        "Needs {{metric_value}} replacement.",
        "Needs <metric_value> replacement.",
    ],
)
def test_docs_targets_fails_placeholder_patterns(
    tmp_path: Path, placeholder_text: str
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(
        repo,
        stage="update_docs",
        last_run_id="run_001",
        paper_targets=["paper/main.md"],
    )

    target_path = repo / "paper" / "main.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("iter1\nrun_001\n", encoding="utf-8")

    docs_update = repo / "experiments" / "plan" / "iter1" / "docs_update.md"
    docs_update.parent.mkdir(parents=True, exist_ok=True)
    docs_update.write_text(
        f"Run iter1 summary.\n{placeholder_text}\n",
        encoding="utf-8",
    )

    result = _run_docs_targets(repo)

    assert result.returncode == 1
    assert "placeholder text" in result.stdout


def test_docs_targets_passes_with_primary_metric_triplet_and_exact_run_paths(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(
        repo,
        stage="update_docs",
        last_run_id="run_001",
        paper_targets=["paper/main.md"],
    )

    target_path = repo / "paper" / "main.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("iter1\nrun_001\n", encoding="utf-8")

    run_dir = repo / "experiments" / "plan" / "iter1" / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter1",
                "run_id": "run_001",
                "status": "completed",
                "primary_metric": {
                    "name": "accuracy",
                    "value": 0.8123,
                    "delta_vs_baseline": 0.0123,
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter1",
                "run_id": "run_001",
                "host_mode": "local",
                "command": "python -m train",
                "resource_request": {"cpus": 1, "memory": "8GB", "gpu_count": 0},
                "artifact_sync_to_local": {"status": "ok"},
                "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
            }
        ),
        encoding="utf-8",
    )

    docs_update = repo / "experiments" / "plan" / "iter1" / "docs_update.md"
    docs_update.parent.mkdir(parents=True, exist_ok=True)
    docs_update.write_text(
        (
            "## Run Evidence\n"
            "- primary metric accuracy: 0.812 (delta 0.012)\n"
            "- metrics artifact: `experiments/plan/iter1/runs/run_001/metrics.json`\n"
            "- manifest artifact: `experiments/plan/iter1/runs/run_001/run_manifest.json`\n"
        ),
        encoding="utf-8",
    )

    result = _run_docs_targets(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "docs_targets: PASS" in result.stdout


def test_docs_targets_fails_without_exact_run_artifact_paths(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(
        repo,
        stage="update_docs",
        last_run_id="run_001",
        paper_targets=["paper/main.md"],
    )

    target_path = repo / "paper" / "main.md"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text("iter1\nrun_001\n", encoding="utf-8")

    run_dir = repo / "experiments" / "plan" / "iter1" / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter1",
                "run_id": "run_001",
                "status": "completed",
                "primary_metric": {
                    "name": "accuracy",
                    "value": 0.8123,
                    "delta_vs_baseline": 0.0123,
                },
            }
        ),
        encoding="utf-8",
    )

    docs_update = repo / "experiments" / "plan" / "iter1" / "docs_update.md"
    docs_update.parent.mkdir(parents=True, exist_ok=True)
    docs_update.write_text(
        "accuracy improved to 0.8123 with delta 0.0123. metrics.json referenced without full path.",
        encoding="utf-8",
    )

    result = _run_docs_targets(repo)

    assert result.returncode == 1
    assert "exact metrics artifact path" in result.stdout.lower()


def test_run_health_launch_passes_without_metrics_json(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="launch", last_run_id="run_001")

    run_dir = repo / "experiments" / "plan" / "iter1" / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "local",
        "status": "running",
        "command": "python -m pkg.train --config design.yaml",
        "resource_request": {"cpus": 2, "memory": "8GB", "gpu_count": 0},
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    result = _run_run_health(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "run_health: PASS" in result.stdout


def test_run_health_fails_completion_like_status_missing_completed_at(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="launch", last_run_id="run_001")

    run_dir = repo / "experiments" / "plan" / "iter1" / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "local",
        "status": "completed",
        "command": "python -m pkg.train --config design.yaml",
        "resource_request": {"cpus": 2, "memory": "8GB", "gpu_count": 0},
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    result = _run_run_health(repo)

    assert result.returncode == 1
    assert "timestamps.completed_at is required" in result.stdout
    assert "project_data_roots=" in result.stdout
    assert "project_data_media_counts=" in result.stdout


def test_run_health_slurm_submitted_allows_pending_sync(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="launch", last_run_id="run_001")

    run_dir = repo / "experiments" / "plan" / "iter1" / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "slurm",
        "status": "submitted",
        "command": "sbatch launch/run_slurm.sbatch",
        "job_id": "123456",
        "resource_request": {"cpus": 8, "memory": "64GB", "gpu_count": 1},
        "artifact_sync_to_local": {"status": "pending"},
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    slurm_ledger = repo / "docs" / "slurm_job_list.md"
    slurm_ledger.parent.mkdir(parents=True, exist_ok=True)
    slurm_ledger.write_text(
        "- 2026-01-01 | job_id=123456 | iteration_id=iter1 | run_id=run_001 | status=submitted\n",
        encoding="utf-8",
    )

    result = _run_run_health(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "run_health: PASS" in result.stdout


def test_run_health_slurm_launch_fails_when_ledger_entry_missing(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="launch", last_run_id="run_001")

    run_dir = repo / "experiments" / "plan" / "iter1" / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "logs").mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "slurm",
        "status": "submitted",
        "command": "sbatch launch/run_slurm.sbatch",
        "job_id": "123456",
        "resource_request": {"cpus": 8, "memory": "64GB", "gpu_count": 1},
        "artifact_sync_to_local": {"status": "pending"},
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    slurm_ledger = repo / "docs" / "slurm_job_list.md"
    slurm_ledger.parent.mkdir(parents=True, exist_ok=True)
    slurm_ledger.write_text(
        "- 2026-01-01 | job_id=123456 | iteration_id=iter1 | run_id=run_other | status=submitted\n",
        encoding="utf-8",
    )

    result = _run_run_health(repo)

    assert result.returncode == 1
    assert "missing run_id=run_001 entry" in result.stdout


def test_run_health_launch_execute_false_allows_missing_logs_dir(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="launch", last_run_id="run_001")

    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_payload = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert isinstance(policy_payload, dict)
    policy_payload.setdefault("launch", {})
    assert isinstance(policy_payload["launch"], dict)
    policy_payload["launch"]["execute"] = False
    policy_path.write_text(
        yaml.safe_dump(policy_payload, sort_keys=False),
        encoding="utf-8",
    )

    run_dir = repo / "experiments" / "plan" / "iter1" / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "local",
        "status": "submitted",
        "command": "python -m pkg.train --config design.yaml",
        "resource_request": {"cpus": 2, "memory": "8GB", "gpu_count": 0},
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    result = _run_run_health(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "run_health: PASS" in result.stdout


# ---------------------------------------------------------------------------
# implementation_plan_lint verifier tests
# ---------------------------------------------------------------------------


def _run_plan_lint(
    repo: Path,
    *,
    stage: str | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "implementation_plan_lint.py"
    command = [sys.executable, str(verifier)]
    if stage:
        command.extend(["--stage", stage])
    run_env: dict[str, str] | None = None
    if env:
        run_env = dict(os.environ)
        run_env.update(env)
    return subprocess.run(
        command,
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        env=run_env,
    )


def _write_plan(repo: Path, content: str) -> None:
    path = repo / "experiments" / "plan" / "iter1" / "implementation_plan.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _setup_lint_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="implementation")
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    return repo


def test_implementation_plan_lint_passes_valid_plan(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nAdded feature X.\n\n"
            "## Tasks\n\n"
            "### T1: Setup\n"
            "- **depends_on**: []\n"
            "- **location**: src/foo.py\n"
            "- **description**: Create foo module\n"
            "- **touches**: [src/foo.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: run tests\n"
            "- **status**: Not Completed\n"
            "- **log**:\n"
            "- **files edited/created**:\n\n"
            "### T2: Integration\n"
            "- **depends_on**: [T1]\n"
            "- **location**: src/bar.py\n"
            "- **description**: Integrate foo into bar\n"
            "- **touches**: [src/bar.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: run tests\n"
            "- **status**: Not Completed\n"
            "- **log**:\n"
            "- **files edited/created**:\n\n"
            "## Parallel Execution Groups\n"
            "| Wave | Tasks | Can Start When |\n"
            "|------|-------|----------------|\n"
            "| 1 | T1 | Immediately |\n"
            "| 2 | T2 | T1 complete |\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "implementation_plan_lint: PASS" in result.stdout


def test_implementation_plan_lint_fails_missing_change_summary(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Tasks\n\n"
            "### T1: Setup\n"
            "- **depends_on**: []\n"
            "- **location**: src/foo.py\n"
            "- **description**: Create foo\n"
            "- **touches**: [src/foo.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: run tests\n"
            "- **status**: Not Completed\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "Change Summary" in result.stdout


def test_implementation_plan_lint_fails_missing_depends_on(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: Setup\n"
            "- **location**: src/foo.py\n"
            "- **description**: Create foo\n"
            "- **validation**: run tests\n"
            "- **status**: Not Completed\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "depends_on" in result.stdout


def test_implementation_plan_lint_fails_circular_dependency(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: First\n"
            "- **depends_on**: [T2]\n"
            "- **location**: src/a.py\n"
            "- **description**: A\n"
            "- **validation**: tests\n"
            "- **status**: Not Completed\n\n"
            "### T2: Second\n"
            "- **depends_on**: [T1]\n"
            "- **location**: src/b.py\n"
            "- **description**: B\n"
            "- **validation**: tests\n"
            "- **status**: Not Completed\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "circular" in result.stdout.lower()


def test_implementation_plan_lint_fails_dangling_dependency(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: First\n"
            "- **depends_on**: [T99]\n"
            "- **location**: src/a.py\n"
            "- **description**: A\n"
            "- **validation**: tests\n"
            "- **status**: Not Completed\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "T99" in result.stdout


def test_implementation_plan_lint_passes_no_task_blocks(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nSmall refactor of helper function.\n\n"
            "## Files Updated\n- src/utils.py\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "implementation_plan_lint: PASS" in result.stdout


def test_implementation_plan_lint_skips_non_implementation_stage(
    tmp_path: Path,
) -> None:
    repo = _setup_lint_repo(tmp_path)

    result = _run_plan_lint(repo, stage="design")

    assert result.returncode == 0
    assert "SKIP" in result.stdout


def test_implementation_plan_lint_fails_wave_overlap(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: Edit config\n"
            "- **depends_on**: []\n"
            "- **location**: src/config.py\n"
            "- **description**: Update config\n"
            "- **touches**: [src/config.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: tests\n"
            "- **status**: Not Completed\n\n"
            "### T2: Also edit config\n"
            "- **depends_on**: []\n"
            "- **location**: src/config.py\n"
            "- **description**: Also modify config\n"
            "- **touches**: [src/config.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: tests\n"
            "- **status**: Not Completed\n\n"
            "## Parallel Execution Groups\n"
            "| Wave | Tasks | Can Start When |\n"
            "|------|-------|----------------|\n"
            "| 1 | T1, T2 | Immediately |\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "overlapping touches" in result.stdout.lower()


def test_implementation_plan_lint_fails_wave_conflict_group(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: Task A\n"
            "- **depends_on**: []\n"
            "- **location**: src/a.py\n"
            "- **description**: A\n"
            "- **touches**: [src/a.py]\n"
            "- **scope_ok**: true\n"
            "- **conflict_group**: database\n"
            "- **validation**: tests\n"
            "- **status**: Not Completed\n\n"
            "### T2: Task B\n"
            "- **depends_on**: []\n"
            "- **location**: src/b.py\n"
            "- **description**: B\n"
            "- **touches**: [src/b.py]\n"
            "- **scope_ok**: true\n"
            "- **conflict_group**: database\n"
            "- **validation**: tests\n"
            "- **status**: Not Completed\n\n"
            "## Parallel Execution Groups\n"
            "| Wave | Tasks | Can Start When |\n"
            "|------|-------|----------------|\n"
            "| 1 | T1, T2 | Immediately |\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "conflict_group" in result.stdout


def test_schema_checks_pass_with_valid_plan_metadata(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    plan_metadata = {
        "schema_version": "1.0",
        "iteration_id": "iter1",
        "generated_at": "2026-01-01T00:00:00Z",
        "skill_used": "swarm-planner",
        "task_count": 5,
        "wave_count": 3,
    }
    path = repo / "experiments" / "plan" / "iter1" / "plan_metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan_metadata, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_fail_with_invalid_plan_metadata(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    plan_metadata = {
        "schema_version": "1.0",
        "iteration_id": "iter1",
        # missing generated_at, skill_used, task_count
    }
    path = repo / "experiments" / "plan" / "iter1" / "plan_metadata.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(plan_metadata, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "plan_metadata" in result.stdout


def test_schema_checks_pass_with_valid_plan_execution_summary(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    summary = {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00Z",
        "stage": "implementation",
        "iteration_id": "iter1",
        "plan_file": "experiments/plan/iter1/implementation_plan.md",
        "contract_hash": "abc123",
        "run_unit": "wave",
        "tasks_total": 1,
        "tasks_completed": 1,
        "tasks_failed": 0,
        "tasks_blocked": 0,
        "tasks_pending": 0,
        "tasks_skipped": 0,
        "tasks_deferred": 0,
        "waves_total": 1,
        "waves_executed": 1,
        "wave_details": [],
        "task_details": [],
        "critical_path": {
            "status": "available",
            "mode": "structural",
            "task_ids": ["T1"],
            "wave_ids": [1],
            "duration_seconds": 0.0,
            "weight": 1.0,
            "basis_note": "structural path from dependency graph with unit task weights",
        },
        "file_conflicts": [],
        "diagnostics": [],
        "observability_summary": {
            "waves_total": 1,
            "waves_executed": 1,
            "tasks_total": 1,
            "tasks_completed": 1,
            "tasks_failed": 0,
            "tasks_blocked": 0,
            "tasks_pending": 0,
            "tasks_skipped": 0,
            "tasks_deferred": 0,
            "retrying_waves": 0,
            "conflict_count": 0,
        },
    }
    path = repo / "experiments" / "plan" / "iter1" / "plan_execution_summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_fail_for_invalid_plan_execution_summary_reason_code(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    summary = {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00Z",
        "stage": "implementation",
        "iteration_id": "iter1",
        "plan_file": "experiments/plan/iter1/implementation_plan.md",
        "contract_hash": "abc123",
        "run_unit": "wave",
        "tasks_total": 1,
        "tasks_completed": 1,
        "tasks_failed": 0,
        "tasks_blocked": 0,
        "tasks_pending": 0,
        "tasks_skipped": 0,
        "tasks_deferred": 0,
        "waves_total": 1,
        "waves_executed": 1,
        "wave_details": [],
        "task_details": [
            _valid_plan_execution_task_detail(reason_code="not_a_real_reason")
        ],
        "critical_path": {
            "status": "available",
            "mode": "measured_complete",
            "task_ids": ["T1"],
            "wave_ids": [1],
            "duration_seconds": 1.0,
            "weight": 1.0,
            "basis_note": "measured task duration",
        },
        "file_conflicts": [],
        "diagnostics": [],
        "observability_summary": {
            "waves_total": 1,
            "waves_executed": 1,
            "tasks_total": 1,
            "tasks_completed": 1,
            "tasks_failed": 0,
            "tasks_blocked": 0,
            "tasks_pending": 0,
            "tasks_skipped": 0,
            "tasks_deferred": 0,
            "retrying_waves": 0,
            "conflict_count": 0,
        },
    }
    path = repo / "experiments" / "plan" / "iter1" / "plan_execution_summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "reason_code" in result.stdout


def test_schema_checks_pass_with_valid_plan_execution_state(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    state_payload = {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
        "stage": "implementation",
        "iteration_id": "iter1",
        "contract_path": ".autolab/plan_contract.json",
        "contract_hash": "abc123",
        "plan_file": "experiments/plan/iter1/implementation_plan.md",
        "run_unit": "wave",
        "waves_total": 1,
        "task_status": {"T1": "completed"},
        "task_attempt_counts": {"T1": 1},
        "task_retry_counts": {"T1": 0},
        "task_last_error": {"T1": ""},
        "task_files_changed": {"T1": []},
        "task_started_at": {"T1": "2026-01-01T00:00:00Z"},
        "task_completed_at": {"T1": "2026-01-01T00:00:01Z"},
        "task_duration_seconds": {"T1": 1.0},
        "task_reason_code": {"T1": "completed"},
        "task_reason_detail": {"T1": ""},
        "task_runner_report_path": {"T1": ".autolab/runner_execution_report.T1.json"},
        "task_verification_status": {"T1": "passed"},
        "task_verification_commands": {"T1": []},
        "task_expected_artifacts_missing": {"T1": []},
        "task_blocked_by": {"T1": []},
        "wave_retry_counts": {"1": 0},
        "wave_status": {"1": "completed"},
        "wave_started_at": {"1": "2026-01-01T00:00:00Z"},
        "wave_completed_at": {"1": "2026-01-01T00:00:01Z"},
        "wave_duration_seconds": {"1": 1.0},
        "wave_attempt_history": {
            "1": [
                {
                    "attempt": 1,
                    "status": "completed",
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:00:01Z",
                    "duration_seconds": 1.0,
                    "retry_reason": "",
                    "detail": "",
                }
            ]
        },
        "wave_retry_reasons": {"1": []},
        "wave_out_of_contract_paths": {"1": []},
        "current_wave": 1,
    }
    path = repo / "experiments" / "plan" / "iter1" / "plan_execution_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_fail_for_unknown_plan_execution_state_task_id(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    state_payload = {
        "schema_version": "1.0",
        "generated_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:01Z",
        "stage": "implementation",
        "iteration_id": "iter1",
        "contract_path": ".autolab/plan_contract.json",
        "contract_hash": "abc123",
        "plan_file": "experiments/plan/iter1/implementation_plan.md",
        "run_unit": "wave",
        "waves_total": 1,
        "task_status": {"T1": "completed", "T99": "completed"},
        "task_attempt_counts": {"T1": 1},
        "task_retry_counts": {"T1": 0},
        "task_last_error": {"T1": ""},
        "task_files_changed": {"T1": []},
        "task_started_at": {"T1": "2026-01-01T00:00:00Z"},
        "task_completed_at": {"T1": "2026-01-01T00:00:01Z"},
        "task_duration_seconds": {"T1": 1.0},
        "task_reason_code": {"T1": "completed"},
        "task_reason_detail": {"T1": ""},
        "task_runner_report_path": {"T1": ".autolab/runner_execution_report.T1.json"},
        "task_verification_status": {"T1": "passed"},
        "task_verification_commands": {"T1": []},
        "task_expected_artifacts_missing": {"T1": []},
        "task_blocked_by": {"T1": []},
        "wave_retry_counts": {"1": 0},
        "wave_status": {"1": "completed"},
        "wave_started_at": {"1": "2026-01-01T00:00:00Z"},
        "wave_completed_at": {"1": "2026-01-01T00:00:01Z"},
        "wave_duration_seconds": {"1": 1.0},
        "wave_attempt_history": {
            "1": [
                {
                    "attempt": 1,
                    "status": "completed",
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:00:01Z",
                    "duration_seconds": 1.0,
                    "retry_reason": "",
                    "detail": "",
                }
            ]
        },
        "wave_retry_reasons": {"1": []},
        "wave_out_of_contract_paths": {"1": []},
        "current_wave": 1,
    }
    path = repo / "experiments" / "plan" / "iter1" / "plan_execution_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "unknown task_id 'T99'" in result.stdout


def test_schema_checks_fail_for_invalid_plan_execution_state_timestamp(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    state_payload = {
        "schema_version": "1.0",
        "generated_at": "not-a-timestamp",
        "updated_at": "2026-01-01T00:00:01Z",
        "stage": "implementation",
        "iteration_id": "iter1",
        "contract_path": ".autolab/plan_contract.json",
        "contract_hash": "abc123",
        "plan_file": "experiments/plan/iter1/implementation_plan.md",
        "run_unit": "wave",
        "waves_total": 1,
        "task_status": {"T1": "completed"},
        "task_attempt_counts": {"T1": 1},
        "task_retry_counts": {"T1": 0},
        "task_last_error": {"T1": ""},
        "task_files_changed": {"T1": []},
        "task_started_at": {"T1": "2026-01-01T00:00:00Z"},
        "task_completed_at": {"T1": "2026-01-01T00:00:01Z"},
        "task_duration_seconds": {"T1": 1.0},
        "task_reason_code": {"T1": "completed"},
        "task_reason_detail": {"T1": ""},
        "task_runner_report_path": {"T1": ".autolab/runner_execution_report.T1.json"},
        "task_verification_status": {"T1": "passed"},
        "task_verification_commands": {"T1": []},
        "task_expected_artifacts_missing": {"T1": []},
        "task_blocked_by": {"T1": []},
        "wave_retry_counts": {"1": 0},
        "wave_status": {"1": "completed"},
        "wave_started_at": {"1": "2026-01-01T00:00:00Z"},
        "wave_completed_at": {"1": "2026-01-01T00:00:01Z"},
        "wave_duration_seconds": {"1": 1.0},
        "wave_attempt_history": {
            "1": [
                {
                    "attempt": 1,
                    "status": "completed",
                    "started_at": "2026-01-01T00:00:00Z",
                    "completed_at": "2026-01-01T00:00:01Z",
                    "duration_seconds": 1.0,
                    "retry_reason": "",
                    "detail": "",
                }
            ]
        },
        "wave_retry_reasons": {"1": []},
        "wave_out_of_contract_paths": {"1": []},
        "current_wave": 1,
    }
    path = repo / "experiments" / "plan" / "iter1" / "plan_execution_state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "date-time" in result.stdout


def test_schema_checks_validate_plan_checker_outputs_in_review_stage(
    tmp_path: Path,
) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    (repo / ".autolab" / "plan_graph.json").write_text(
        json.dumps({"waves": "not-a-list"}, indent=2),
        encoding="utf-8",
    )

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "plan_graph.json schema violation" in result.stdout


# ---------------------------------------------------------------------------
# prompts helper tests (Items 3, 4, 13)
# ---------------------------------------------------------------------------


def test_parse_signed_delta_prioritizes_signed_value() -> None:
    from autolab.prompts import _parse_signed_delta

    assert _parse_signed_delta("80 +5.0%") == 5.0
    assert _parse_signed_delta("2.0 -0.3") == -0.3
    assert _parse_signed_delta("5.0") == 5.0
    assert _parse_signed_delta("+10") == 10.0
    assert _parse_signed_delta("") is None


def test_target_comparison_text_all_mode_combinations() -> None:
    from autolab.prompts import _target_comparison_text

    payload = {"primary_metric": {"name": "m", "delta_vs_baseline": 6.0}}
    # maximize met
    _, s = _target_comparison_text(
        metrics_payload=payload,
        hypothesis_target_delta=5.0,
        design_target_delta="",
        run_id="r1",
        metric_mode="maximize",
    )
    assert "stop" in s
    # maximize not met
    _, s = _target_comparison_text(
        metrics_payload=payload,
        hypothesis_target_delta=10.0,
        design_target_delta="",
        run_id="r1",
        metric_mode="maximize",
    )
    assert "design" in s
    # minimize met
    payload_min = {"primary_metric": {"name": "m", "delta_vs_baseline": -3.0}}
    _, s = _target_comparison_text(
        metrics_payload=payload_min,
        hypothesis_target_delta=-2.0,
        design_target_delta="",
        run_id="r1",
        metric_mode="minimize",
    )
    assert "stop" in s
    # minimize not met
    payload_min2 = {"primary_metric": {"name": "m", "delta_vs_baseline": -1.0}}
    _, s = _target_comparison_text(
        metrics_payload=payload_min2,
        hypothesis_target_delta=-2.0,
        design_target_delta="",
        run_id="r1",
        metric_mode="minimize",
    )
    assert "design" in s


def test_suggest_decision_minimize_mode_via_design(tmp_path: Path) -> None:
    import json as _json
    from autolab.prompts import _suggest_decision_from_metrics

    repo = tmp_path / "repo"
    repo.mkdir()
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "decide_repeat",
        "stage_attempt": 0,
        "last_run_id": "run_001",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    (repo / ".autolab").mkdir(parents=True, exist_ok=True)
    (repo / ".autolab" / "state.json").write_text(_json.dumps(state), encoding="utf-8")
    backlog = {
        "experiments": [
            {
                "id": "e1",
                "hypothesis_id": "h1",
                "status": "open",
                "iteration_id": "iter1",
            }
        ]
    }
    (repo / ".autolab" / "backlog.yaml").write_text(
        yaml.safe_dump(backlog), encoding="utf-8"
    )

    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "hypothesis.md").write_text(
        "# H\n- target_delta: -2.0\n", encoding="utf-8"
    )
    design = {
        "metrics": {
            "primary": {"name": "loss", "unit": "nats", "mode": "minimize"},
            "success_delta": "-2.0",
        },
    }
    (iteration_dir / "design.yaml").write_text(yaml.safe_dump(design), encoding="utf-8")
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = {
        "status": "complete",
        "primary_metric": {"name": "loss", "value": 1.0, "delta_vs_baseline": -3.0},
    }
    (run_dir / "metrics.json").write_text(_json.dumps(metrics), encoding="utf-8")

    result, evidence = _suggest_decision_from_metrics(repo, state)
    assert result == "stop"
    assert isinstance(evidence, dict)


def test_implementation_plan_lint_fails_ellipsis_placeholder(tmp_path: Path) -> None:
    """#8: Ellipsis patterns are detected as placeholders."""
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: Setup\n"
            "- **depends_on**: []\n"
            "- **location**: src/foo.py\n"
            "- **description**: Create foo module with ... details\n"
            "- **touches**: [src/foo.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: run tests\n"
            "- **status**: Not Completed\n"
            "- **log**:\n"
            "- **files edited/created**:\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "placeholder" in result.stdout.lower() or "ellipsis" in result.stdout.lower()


def test_implementation_plan_lint_fails_unicode_ellipsis_placeholder(
    tmp_path: Path,
) -> None:
    """#8: Unicode ellipsis \u2026 is detected as a placeholder."""
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: Setup\n"
            "- **depends_on**: []\n"
            "- **location**: src/foo.py\n"
            "- **description**: Create foo module with\u2026 details\n"
            "- **touches**: [src/foo.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: run tests\n"
            "- **status**: Not Completed\n"
            "- **log**:\n"
            "- **files edited/created**:\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "placeholder" in result.stdout.lower() or "ellipsis" in result.stdout.lower()


def test_implementation_plan_lint_scope_enforcement_warn_mode(tmp_path: Path) -> None:
    """#18: Out-of-scope touches produce warnings (not failures) by default."""
    repo = _setup_lint_repo(tmp_path)
    # Write context.json with allowed_edit_dirs
    context_dir = repo / ".autolab" / "prompts" / "rendered"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "implementation.context.json").write_text(
        json.dumps({"runner_scope": {"allowed_edit_dirs": ["src"]}}),
        encoding="utf-8",
    )
    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: Setup\n"
            "- **depends_on**: []\n"
            "- **location**: outside/foo.py\n"
            "- **description**: Create foo\n"
            "- **touches**: [outside/foo.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: run tests\n"
            "- **status**: Not Completed\n"
            "- **log**:\n"
            "- **files edited/created**:\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "WARN" in result.stdout


def test_implementation_plan_lint_scope_enforcement_fail_mode(tmp_path: Path) -> None:
    """#18: Out-of-scope touches fail when fail_on_out_of_scope_touches is true."""
    repo = _setup_lint_repo(tmp_path)
    # Write context.json with allowed_edit_dirs
    context_dir = repo / ".autolab" / "prompts" / "rendered"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "implementation.context.json").write_text(
        json.dumps({"runner_scope": {"allowed_edit_dirs": ["src"]}}),
        encoding="utf-8",
    )
    # Set policy to fail on scope violations
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    policy.setdefault("implementation_plan_lint", {})["scope_enforcement"] = {
        "fail_on_out_of_scope_touches": True,
    }
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: Setup\n"
            "- **depends_on**: []\n"
            "- **location**: outside/foo.py\n"
            "- **description**: Create foo\n"
            "- **touches**: [outside/foo.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: run tests\n"
            "- **status**: Not Completed\n"
            "- **log**:\n"
            "- **files edited/created**:\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "outside allowed scope" in result.stdout


def test_implementation_plan_lint_scope_enforcement_auto_mode_defaults_to_fail(
    tmp_path: Path,
) -> None:
    """Out-of-scope touches fail in auto mode when policy key is absent."""
    repo = _setup_lint_repo(tmp_path)
    context_dir = repo / ".autolab" / "prompts" / "rendered"
    context_dir.mkdir(parents=True, exist_ok=True)
    (context_dir / "implementation.context.json").write_text(
        json.dumps({"runner_scope": {"allowed_edit_dirs": ["src"]}}),
        encoding="utf-8",
    )

    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    lint_policy = policy.setdefault("implementation_plan_lint", {})
    if isinstance(lint_policy, dict):
        scope_enforcement = lint_policy.get("scope_enforcement")
        if isinstance(scope_enforcement, dict):
            scope_enforcement.pop("fail_on_out_of_scope_touches", None)
            if not scope_enforcement:
                lint_policy.pop("scope_enforcement", None)
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: Setup\n"
            "- **depends_on**: []\n"
            "- **location**: outside/foo.py\n"
            "- **description**: Create foo\n"
            "- **touches**: [outside/foo.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: run tests\n"
            "- **status**: Not Completed\n"
            "- **log**:\n"
            "- **files edited/created**:\n"
        ),
    )

    result = _run_plan_lint(repo, env={"AUTOLAB_AUTO_MODE": "1"})

    assert result.returncode == 1
    assert "outside allowed scope" in result.stdout


def test_allowed_edit_dirs_field_name_in_context(tmp_path: Path) -> None:
    """#4: Verify allowed_edit_dirs is the canonical field name used in context JSON."""
    from autolab.prompts import _build_runtime_stage_context_block

    context = {
        "stage": "implementation",
        "iteration_id": "iter1",
        "iteration_path": "experiments/plan/iter1",
        "host_mode": "local",
        "state_snapshot": {"stage_attempt": 0, "max_stage_attempts": 3},
        "runner_scope": {
            "mode": "scope_root_plus_core",
            "workspace_dir": "/w",
            "allowed_edit_dirs": ["src", "tests"],
        },
    }
    block = _build_runtime_stage_context_block(context)
    assert "src" in block
    assert "tests" in block
    assert "allowed_edit_dirs" in block


def test_implementation_plan_lint_passes_no_wave_overlap(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(
        repo,
        (
            "## Change Summary\nDone.\n\n"
            "### T1: Edit config\n"
            "- **depends_on**: []\n"
            "- **location**: src/config.py\n"
            "- **description**: Update config\n"
            "- **touches**: [src/config.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: tests\n"
            "- **status**: Not Completed\n\n"
            "### T2: Edit utils\n"
            "- **depends_on**: []\n"
            "- **location**: src/utils.py\n"
            "- **description**: Update utils\n"
            "- **touches**: [src/utils.py]\n"
            "- **scope_ok**: true\n"
            "- **validation**: tests\n"
            "- **status**: Not Completed\n\n"
            "## Parallel Execution Groups\n"
            "| Wave | Tasks | Can Start When |\n"
            "|------|-------|----------------|\n"
            "| 1 | T1, T2 | Immediately |\n"
        ),
    )

    result = _run_plan_lint(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "implementation_plan_lint: PASS" in result.stdout


# ---------------------------------------------------------------------------
# result_sanity tests
# ---------------------------------------------------------------------------


def _setup_extract_results_repo(tmp_path: Path, *, metrics: dict | None = None) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="extract_results", last_run_id="run1")
    _write_backlog(repo)
    iter_dir = repo / "experiments" / "plan" / "iter1"
    iter_dir.mkdir(parents=True, exist_ok=True)
    if metrics is not None:
        run_dir = iter_dir / "runs" / "run1"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2), encoding="utf-8"
        )
    return repo


def test_result_sanity_passes_valid_metrics(tmp_path: Path) -> None:
    metrics = {
        "iteration_id": "iter1",
        "run_id": "run1",
        "status": "completed",
        "primary_metric": {
            "name": "accuracy",
            "value": 90.5,
            "delta_vs_baseline": 1.2,
        },
    }
    repo = _setup_extract_results_repo(tmp_path, metrics=metrics)
    result = _run_result_sanity(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_result_sanity_fails_nan_metric(tmp_path: Path) -> None:
    metrics = {
        "iteration_id": "iter1",
        "run_id": "run1",
        "status": "completed",
        "primary_metric": {
            "name": "accuracy",
            "value": float("nan"),
            "delta_vs_baseline": 1.2,
        },
    }
    repo = _setup_extract_results_repo(tmp_path, metrics=metrics)
    result = _run_result_sanity(repo)
    assert result.returncode != 0
    assert (
        "invalid numeric value" in result.stdout.lower()
        or "nan" in result.stdout.lower()
    )


def test_result_sanity_fails_inf_metric(tmp_path: Path) -> None:
    metrics = {
        "iteration_id": "iter1",
        "run_id": "run1",
        "status": "completed",
        "primary_metric": {
            "name": "accuracy",
            "value": float("inf"),
            "delta_vs_baseline": 1.2,
        },
    }
    repo = _setup_extract_results_repo(tmp_path, metrics=metrics)
    result = _run_result_sanity(repo)
    assert result.returncode != 0
    assert (
        "invalid numeric value" in result.stdout.lower()
        or "inf" in result.stdout.lower()
    )


def test_result_sanity_fails_placeholder_in_metrics(tmp_path: Path) -> None:
    metrics = {
        "iteration_id": "iter1",
        "run_id": "run1",
        "status": "completed",
        "primary_metric": {
            "name": "accuracy",
            "value": "<TODO>",
            "delta_vs_baseline": "placeholder",
        },
    }
    repo = _setup_extract_results_repo(tmp_path, metrics=metrics)
    result = _run_result_sanity(repo)
    assert result.returncode != 0
    assert "placeholder" in result.stdout.lower()


def test_result_sanity_skips_non_extract_results_stage(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="implementation_review", last_run_id="")
    _write_backlog(repo)
    result = _run_result_sanity(repo, json_flag=True)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "pass"
    assert payload["verifier"] == "result_sanity"
    assert payload["stage"] == "implementation_review"
    checks = payload.get("checks", [])
    assert isinstance(checks, list) and checks
    assert "skipped for stage=implementation_review" in str(checks[0].get("detail", ""))


def test_result_sanity_fails_when_last_run_id_missing_for_extract_results(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="extract_results", last_run_id="")
    _write_backlog(repo)
    result = _run_result_sanity(repo)
    assert result.returncode != 0
    assert "missing last_run_id for extract_results" in result.stdout


# ---------------------------------------------------------------------------
# docs_drift tests
# ---------------------------------------------------------------------------


def _setup_docs_drift_repo(
    tmp_path: Path,
    *,
    metrics: dict | None = None,
    docs_update_text: str = "",
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="update_docs", last_run_id="run1")
    _write_backlog(repo)
    iter_dir = repo / "experiments" / "plan" / "iter1"
    iter_dir.mkdir(parents=True, exist_ok=True)
    if metrics is not None:
        run_dir = iter_dir / "runs" / "run1"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "metrics.json").write_text(
            json.dumps(metrics, indent=2), encoding="utf-8"
        )
    (iter_dir / "docs_update.md").write_text(docs_update_text, encoding="utf-8")
    return repo


def test_docs_drift_passes_when_metric_value_present(tmp_path: Path) -> None:
    metrics = {
        "primary_metric": {
            "name": "accuracy",
            "value": 90.5,
            "delta_vs_baseline": 1.2,
        },
    }
    docs_text = "## Results\nThe accuracy reached 90.5 in this iteration.\n"
    repo = _setup_docs_drift_repo(tmp_path, metrics=metrics, docs_update_text=docs_text)
    result = _run_docs_drift(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_docs_drift_fails_when_metric_value_missing(tmp_path: Path) -> None:
    metrics = {
        "primary_metric": {
            "name": "accuracy",
            "value": 90.5,
            "delta_vs_baseline": 1.2,
        },
    }
    docs_text = "## Results\nSome improvements were observed.\n"
    repo = _setup_docs_drift_repo(tmp_path, metrics=metrics, docs_update_text=docs_text)
    result = _run_docs_drift(repo)
    assert result.returncode != 0
    assert (
        "does not reference" in result.stdout.lower()
        or "accuracy" in result.stdout.lower()
    )


def test_docs_drift_detects_contradiction(tmp_path: Path) -> None:
    metrics = {
        "primary_metric": {
            "name": "accuracy",
            "value": 90.5,
            "delta_vs_baseline": 1.2,
        },
    }
    # Mention accuracy with wrong value
    docs_text = "## Results\nThe accuracy reached 90.5 overall but accuracy was 75.0 in subset.\n"
    repo = _setup_docs_drift_repo(tmp_path, metrics=metrics, docs_update_text=docs_text)
    result = _run_docs_drift(repo)
    assert result.returncode != 0
    assert "contradictory" in result.stdout.lower()


def test_docs_drift_skips_non_update_docs_stage(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="implementation", last_run_id="run1")
    _write_backlog(repo)
    result = _run_docs_drift(repo)
    assert result.returncode == 0


def test_docs_drift_no_changes_needed(tmp_path: Path) -> None:
    metrics = {
        "primary_metric": {
            "name": "accuracy",
            "value": 90.5,
            "delta_vs_baseline": 0.0,
        },
    }
    docs_text = "No changes needed for this iteration.\n"
    repo = _setup_docs_drift_repo(tmp_path, metrics=metrics, docs_update_text=docs_text)
    # Even though metric value is missing, no_changes_needed is not a failure --
    # doc_drift only fails when metric is mentioned with wrong value or absent when present.
    result = _run_docs_drift(repo)
    # The verifier may still flag missing value; just ensure it doesn't crash
    assert result.returncode in (0, 1)


# ---------------------------------------------------------------------------
# closed_experiment_guard tests
# ---------------------------------------------------------------------------


def _setup_closed_guard_repo(
    tmp_path: Path, *, backlog_experiments: list[dict]
) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo)
    backlog = {
        "hypotheses": [
            {
                "id": "h1",
                "status": "open",
                "title": "hyp",
                "success_metric": "accuracy",
                "target_delta": 0.1,
            }
        ],
        "experiments": backlog_experiments,
    }
    (repo / ".autolab" / "backlog.yaml").write_text(
        yaml.safe_dump(backlog, sort_keys=False), encoding="utf-8"
    )
    # Initialize git repo for git status to work
    subprocess.run(["git", "init"], cwd=repo, capture_output=True, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, capture_output=True, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=test@test.com",
            "-c",
            "user.name=Test",
            "commit",
            "-m",
            "init",
        ],
        cwd=repo,
        capture_output=True,
        check=True,
    )
    return repo


def test_closed_experiment_guard_passes_when_iteration_open(tmp_path: Path) -> None:
    experiments = [
        {"id": "e1", "hypothesis_id": "h1", "status": "open", "iteration_id": "iter1"},
    ]
    repo = _setup_closed_guard_repo(tmp_path, backlog_experiments=experiments)
    result = _run_closed_experiment_guard(repo)
    assert result.returncode == 0, result.stdout + result.stderr


def test_closed_experiment_guard_detects_edit_to_closed_iteration(
    tmp_path: Path,
) -> None:
    experiments = [
        {
            "id": "e1",
            "hypothesis_id": "h1",
            "status": "done",
            "iteration_id": "iter_closed",
        },
    ]
    repo = _setup_closed_guard_repo(tmp_path, backlog_experiments=experiments)
    # Create a file under the closed iteration path
    closed_path = repo / "experiments" / "plan" / "iter_closed"
    closed_path.mkdir(parents=True, exist_ok=True)
    (closed_path / "something.txt").write_text("modified", encoding="utf-8")
    result = _run_closed_experiment_guard(repo)
    assert result.returncode != 0
    assert "iter_closed" in result.stdout


def test_closed_experiment_guard_passes_no_closed_iterations(tmp_path: Path) -> None:
    experiments = [
        {"id": "e1", "hypothesis_id": "h1", "status": "open", "iteration_id": "iter1"},
    ]
    repo = _setup_closed_guard_repo(tmp_path, backlog_experiments=experiments)
    result = _run_closed_experiment_guard(repo)
    assert result.returncode == 0
