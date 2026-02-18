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


# ---------------------------------------------------------------------------
# implementation_plan_lint verifier tests
# ---------------------------------------------------------------------------


def _run_plan_lint(repo: Path, *, stage: str | None = None) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "implementation_plan_lint.py"
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
    _write_plan(repo, (
        "## Change Summary\nAdded feature X.\n\n"
        "## Tasks\n\n"
        "### T1: Setup\n"
        "- **depends_on**: []\n"
        "- **location**: src/foo.py\n"
        "- **description**: Create foo module\n"
        "- **touches**: [src/foo.py]\n"
        "- **validation**: run tests\n"
        "- **status**: Not Completed\n"
        "- **log**:\n"
        "- **files edited/created**:\n\n"
        "### T2: Integration\n"
        "- **depends_on**: [T1]\n"
        "- **location**: src/bar.py\n"
        "- **description**: Integrate foo into bar\n"
        "- **touches**: [src/bar.py]\n"
        "- **validation**: run tests\n"
        "- **status**: Not Completed\n"
        "- **log**:\n"
        "- **files edited/created**:\n\n"
        "## Parallel Execution Groups\n"
        "| Wave | Tasks | Can Start When |\n"
        "|------|-------|----------------|\n"
        "| 1 | T1 | Immediately |\n"
        "| 2 | T2 | T1 complete |\n"
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "implementation_plan_lint: PASS" in result.stdout


def test_implementation_plan_lint_fails_missing_change_summary(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
        "## Tasks\n\n"
        "### T1: Setup\n"
        "- **depends_on**: []\n"
        "- **location**: src/foo.py\n"
        "- **description**: Create foo\n"
        "- **touches**: [src/foo.py]\n"
        "- **validation**: run tests\n"
        "- **status**: Not Completed\n"
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "Change Summary" in result.stdout


def test_implementation_plan_lint_fails_missing_depends_on(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
        "## Change Summary\nDone.\n\n"
        "### T1: Setup\n"
        "- **location**: src/foo.py\n"
        "- **description**: Create foo\n"
        "- **validation**: run tests\n"
        "- **status**: Not Completed\n"
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "depends_on" in result.stdout


def test_implementation_plan_lint_fails_circular_dependency(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
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
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "circular" in result.stdout.lower()


def test_implementation_plan_lint_fails_dangling_dependency(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
        "## Change Summary\nDone.\n\n"
        "### T1: First\n"
        "- **depends_on**: [T99]\n"
        "- **location**: src/a.py\n"
        "- **description**: A\n"
        "- **validation**: tests\n"
        "- **status**: Not Completed\n"
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "T99" in result.stdout


def test_implementation_plan_lint_passes_no_task_blocks(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
        "## Change Summary\nSmall refactor of helper function.\n\n"
        "## Files Updated\n- src/utils.py\n"
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "implementation_plan_lint: PASS" in result.stdout


def test_implementation_plan_lint_skips_non_implementation_stage(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)

    result = _run_plan_lint(repo, stage="design")

    assert result.returncode == 0
    assert "SKIP" in result.stdout


def test_implementation_plan_lint_fails_wave_overlap(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
        "## Change Summary\nDone.\n\n"
        "### T1: Edit config\n"
        "- **depends_on**: []\n"
        "- **location**: src/config.py\n"
        "- **description**: Update config\n"
        "- **touches**: [src/config.py]\n"
        "- **validation**: tests\n"
        "- **status**: Not Completed\n\n"
        "### T2: Also edit config\n"
        "- **depends_on**: []\n"
        "- **location**: src/config.py\n"
        "- **description**: Also modify config\n"
        "- **touches**: [src/config.py]\n"
        "- **validation**: tests\n"
        "- **status**: Not Completed\n\n"
        "## Parallel Execution Groups\n"
        "| Wave | Tasks | Can Start When |\n"
        "|------|-------|----------------|\n"
        "| 1 | T1, T2 | Immediately |\n"
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "overlapping touches" in result.stdout.lower()


def test_implementation_plan_lint_fails_wave_conflict_group(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
        "## Change Summary\nDone.\n\n"
        "### T1: Task A\n"
        "- **depends_on**: []\n"
        "- **location**: src/a.py\n"
        "- **description**: A\n"
        "- **touches**: [src/a.py]\n"
        "- **conflict_group**: database\n"
        "- **validation**: tests\n"
        "- **status**: Not Completed\n\n"
        "### T2: Task B\n"
        "- **depends_on**: []\n"
        "- **location**: src/b.py\n"
        "- **description**: B\n"
        "- **touches**: [src/b.py]\n"
        "- **conflict_group**: database\n"
        "- **validation**: tests\n"
        "- **status**: Not Completed\n\n"
        "## Parallel Execution Groups\n"
        "| Wave | Tasks | Can Start When |\n"
        "|------|-------|----------------|\n"
        "| 1 | T1, T2 | Immediately |\n"
    ))

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
        "iteration_id": "iter1",
        "plan_file": "experiments/plan/iter1/implementation_plan.md",
        "tasks_total": 4,
        "tasks_completed": 4,
        "tasks_failed": 0,
        "tasks_blocked": 0,
    }
    path = repo / "experiments" / "plan" / "iter1" / "plan_execution_summary.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    result = _run_schema_checks(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "schema_checks: PASS" in result.stdout


def test_implementation_plan_lint_passes_no_wave_overlap(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
        "## Change Summary\nDone.\n\n"
        "### T1: Edit config\n"
        "- **depends_on**: []\n"
        "- **location**: src/config.py\n"
        "- **description**: Update config\n"
        "- **touches**: [src/config.py]\n"
        "- **validation**: tests\n"
        "- **status**: Not Completed\n\n"
        "### T2: Edit utils\n"
        "- **depends_on**: []\n"
        "- **location**: src/utils.py\n"
        "- **description**: Update utils\n"
        "- **touches**: [src/utils.py]\n"
        "- **validation**: tests\n"
        "- **status**: Not Completed\n\n"
        "## Parallel Execution Groups\n"
        "| Wave | Tasks | Can Start When |\n"
        "|------|-------|----------------|\n"
        "| 1 | T1, T2 | Immediately |\n"
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "implementation_plan_lint: PASS" in result.stdout
