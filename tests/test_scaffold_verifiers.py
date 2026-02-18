from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml


pytest.importorskip("jsonschema")


def _copy_scaffold(repo: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "autolab" / "scaffold" / ".autolab"
    target = repo / ".autolab"
    shutil.copytree(source, target, dirs_exist_ok=True)


def _write_state(repo: Path, *, stage: str = "implementation_review", last_run_id: str = "") -> None:
    state = {
        "iteration_id": "iter1",
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": last_run_id,
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
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
        "variants": [{"name": "proposed", "changes": {}}],
    }
    path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


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


def _run_schema_checks(repo: Path, *, stage: str | None = None) -> subprocess.CompletedProcess[str]:
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


def _run_prompt_lint(repo: Path, *, stage: str | None = None) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "prompt_lint.py"
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


def _setup_review_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)
    return repo


def test_schema_checks_pass_for_valid_review_payload(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_fail_when_required_check_key_missing(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=False)

    result = _run_schema_checks(repo)

    assert result.returncode == 1
    assert "docs_target_update" in result.stdout


def test_schema_checks_design_stage_override_skips_review_artifacts(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_schema_checks_extract_results_skips_run_checks_when_run_id_missing(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_state(repo, stage="extract_results", last_run_id="")
    _write_review_result(repo, include_docs_check=True)

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_prompt_lint_passes_for_scaffold_prompts(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)

    result = _run_prompt_lint(repo, stage="design")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "prompt_lint: PASS" in result.stdout


def test_prompt_lint_fails_on_unsupported_token(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    prompt_path = repo / ".autolab" / "prompts" / "stage_design.md"
    prompt_path.write_text(
        (
            "# Stage: design\n\n"
            "## ROLE\nx\n\n"
            "## PRIMARY OBJECTIVE\nx\n\n"
            "{{shared:guardrails.md}}\n{{shared:repo_scope.md}}\n{{shared:runtime_context.md}}\n\n"
            "## OUTPUTS (STRICT)\n- x\n\n"
            "## REQUIRED INPUTS\n- x {{unknown_token}}\n\n"
            "## FILE CHECKLIST (machine-auditable)\n{{shared:checklist.md}}\n\n"
            "## FAILURE / RETRY BEHAVIOR\n- x\n"
        ),
        encoding="utf-8",
    )

    result = _run_prompt_lint(repo, stage="design")

    assert result.returncode == 1
    assert "unsupported token" in result.stdout


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


def test_schema_checks_require_todo_files_when_assistant_mode_on(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    state_path = repo / ".autolab" / "state.json"
    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    state_payload["assistant_mode"] = "on"
    state_path.write_text(json.dumps(state_payload, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo, stage="design")

    assert result.returncode == 1
    assert "todo_state.json is required when assistant_mode=on" in result.stdout
