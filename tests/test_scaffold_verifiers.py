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
    source = Path(__file__).resolve().parents[1] / "src" / "autolab" / "scaffold" / ".autolab"
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


def _run_registry_consistency(repo: Path, *, stage: str | None = None) -> subprocess.CompletedProcess[str]:
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


def _run_consistency_checks(repo: Path, *, stage: str | None = None) -> subprocess.CompletedProcess[str]:
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


def test_registry_consistency_fails_when_policy_requires_unsupported_capability(tmp_path: Path) -> None:
    repo = _setup_review_repo(tmp_path)
    _write_review_result(repo, include_docs_check=True)
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    assert isinstance(policy, dict)
    req = policy.setdefault("requirements_by_stage", {})
    assert isinstance(req, dict)
    design_req = req.setdefault("design", {})
    assert isinstance(design_req, dict)
    design_req["env_smoke"] = True  # design stage capability is false in workflow registry
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")

    result = _run_registry_consistency(repo, stage="design")

    assert result.returncode == 1
    assert "not supported by workflow.yaml verifier_categories" in result.stdout


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
    (run_dir / "run_manifest.json").write_text(json.dumps(run_manifest, indent=2), encoding="utf-8")
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    result = _run_consistency_checks(repo, stage="extract_results")

    assert result.returncode == 1
    assert "does not match design.metrics.primary.name" in result.stdout


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
# docs_targets / run_health verifier tests
# ---------------------------------------------------------------------------


def test_docs_targets_passes_with_no_paper_targets_and_required_rationale(tmp_path: Path) -> None:
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


def test_docs_targets_fails_with_no_paper_targets_and_missing_rationale(tmp_path: Path) -> None:
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
def test_docs_targets_fails_placeholder_patterns(tmp_path: Path, placeholder_text: str) -> None:
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
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = _run_run_health(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "run_health: PASS" in result.stdout


def test_run_health_fails_completion_like_status_missing_completed_at(tmp_path: Path) -> None:
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
    (run_dir / "run_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    result = _run_run_health(repo)

    assert result.returncode == 1
    assert "timestamps.completed_at is required" in result.stdout


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
    _write_plan(repo, (
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
        "- **scope_ok**: true\n"
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
        metrics_payload=payload, hypothesis_target_delta=5.0,
        design_target_delta="", run_id="r1", metric_mode="maximize",
    )
    assert "stop" in s
    # maximize not met
    _, s = _target_comparison_text(
        metrics_payload=payload, hypothesis_target_delta=10.0,
        design_target_delta="", run_id="r1", metric_mode="maximize",
    )
    assert "design" in s
    # minimize met
    payload_min = {"primary_metric": {"name": "m", "delta_vs_baseline": -3.0}}
    _, s = _target_comparison_text(
        metrics_payload=payload_min, hypothesis_target_delta=-2.0,
        design_target_delta="", run_id="r1", metric_mode="minimize",
    )
    assert "stop" in s
    # minimize not met
    payload_min2 = {"primary_metric": {"name": "m", "delta_vs_baseline": -1.0}}
    _, s = _target_comparison_text(
        metrics_payload=payload_min2, hypothesis_target_delta=-2.0,
        design_target_delta="", run_id="r1", metric_mode="minimize",
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
    backlog = {"experiments": [{"id": "e1", "hypothesis_id": "h1", "status": "open", "iteration_id": "iter1"}]}
    (repo / ".autolab" / "backlog.yaml").write_text(yaml.safe_dump(backlog), encoding="utf-8")

    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "hypothesis.md").write_text("# H\n- target_delta: -2.0\n", encoding="utf-8")
    design = {
        "metrics": {"primary": {"name": "loss", "unit": "nats", "mode": "minimize"}, "success_delta": "-2.0"},
    }
    (iteration_dir / "design.yaml").write_text(yaml.safe_dump(design), encoding="utf-8")
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    metrics = {"status": "complete", "primary_metric": {"name": "loss", "value": 1.0, "delta_vs_baseline": -3.0}}
    (run_dir / "metrics.json").write_text(_json.dumps(metrics), encoding="utf-8")

    result, evidence = _suggest_decision_from_metrics(repo, state)
    assert result == "stop"
    assert isinstance(evidence, dict)


def test_implementation_plan_lint_fails_ellipsis_placeholder(tmp_path: Path) -> None:
    """#8: Ellipsis patterns are detected as placeholders."""
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
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
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "placeholder" in result.stdout.lower() or "ellipsis" in result.stdout.lower()


def test_implementation_plan_lint_fails_unicode_ellipsis_placeholder(tmp_path: Path) -> None:
    """#8: Unicode ellipsis \u2026 is detected as a placeholder."""
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
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
    ))

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
    _write_plan(repo, (
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
    ))

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

    _write_plan(repo, (
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
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 1
    assert "outside allowed scope" in result.stdout


def test_implementation_plan_lint_scope_enforcement_auto_mode_defaults_to_fail(tmp_path: Path) -> None:
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

    _write_plan(repo, (
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
    ))

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
        "runner_scope": {"mode": "iteration_plus_core", "workspace_dir": "/w", "allowed_edit_dirs": ["src", "tests"]},
    }
    block = _build_runtime_stage_context_block(context)
    assert "src" in block
    assert "tests" in block
    assert "allowed_edit_dirs" in block


def test_implementation_plan_lint_passes_no_wave_overlap(tmp_path: Path) -> None:
    repo = _setup_lint_repo(tmp_path)
    _write_plan(repo, (
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
    ))

    result = _run_plan_lint(repo)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "implementation_plan_lint: PASS" in result.stdout
