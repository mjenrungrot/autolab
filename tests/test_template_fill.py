from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import yaml


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


def _write_state(repo: Path, *, stage: str, last_run_id: str = "") -> None:
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


def _run_template_fill(repo: Path, *, stage: str) -> subprocess.CompletedProcess[str]:
    verifier = repo / ".autolab" / "verifiers" / "template_fill.py"
    return subprocess.run(
        [sys.executable, str(verifier), "--stage", stage],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_template_fill_fails_hypothesis_placeholder_content(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="hypothesis")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "hypothesis.md").write_text(
        "# Hypothesis\n\nPrimaryMetric: TODO; Unit: TODO; Success: TODO\n",
        encoding="utf-8",
    )

    result = _run_template_fill(repo, stage="hypothesis")

    assert result.returncode == 1
    assert "contains placeholder pattern" in result.stdout


def test_template_fill_detects_ellipsis_placeholder(tmp_path: Path) -> None:
    """#8: ASCII ellipsis (...) is flagged as a placeholder pattern."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="hypothesis")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "hypothesis.md").write_text(
        "# Hypothesis\n\nPrimaryMetric: accuracy; Unit: %; Success: +5%\n\nWe expect that ... will improve results.\n",
        encoding="utf-8",
    )

    result = _run_template_fill(repo, stage="hypothesis")

    assert result.returncode == 1
    assert "placeholder pattern" in result.stdout


def test_template_fill_detects_unicode_ellipsis_placeholder(tmp_path: Path) -> None:
    """#8: Unicode ellipsis (\u2026) is flagged as a placeholder pattern."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="hypothesis")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "hypothesis.md").write_text(
        "# Hypothesis\n\nPrimaryMetric: accuracy; Unit: %; Success: +5%\n\nWe expect that\u2026 will improve results.\n",
        encoding="utf-8",
    )

    result = _run_template_fill(repo, stage="hypothesis")

    assert result.returncode == 1
    assert "placeholder pattern" in result.stdout


def test_template_fill_detects_exact_bootstrap_implementation_template(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="implementation")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "implementation_plan.md").write_text(
        "# Implementation Plan\n\n- Implement the design requirements.\n",
        encoding="utf-8",
    )

    result = _run_template_fill(repo, stage="implementation")

    assert result.returncode == 1
    assert "exactly matches template placeholder content" in result.stdout


def _write_launch_fixture(
    repo: Path,
    *,
    host_mode: str,
    include_ledger: bool,
) -> None:
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    (iteration_dir / "launch").mkdir(parents=True, exist_ok=True)
    (iteration_dir / "runs" / "run_001").mkdir(parents=True, exist_ok=True)

    (iteration_dir / "design.yaml").write_text(
        (
            'schema_version: "1.0"\n'
            'id: "e1"\n'
            'iteration_id: "iter1"\n'
            'hypothesis_id: "h1"\n'
            "entrypoint:\n"
            '  module: "pkg.train"\n'
            "compute:\n"
            f'  location: "{host_mode}"\n'
            "metrics:\n"
            "  primary:\n"
            '    name: "accuracy"\n'
            "  secondary: []\n"
            '  success_delta: "0.1"\n'
            '  aggregation: "mean"\n'
            '  baseline_comparison: "baseline"\n'
            "baselines:\n"
            "  - name: baseline\n"
            "    description: baseline\n"
        ),
        encoding="utf-8",
    )

    script_name = "run_slurm.sbatch" if host_mode == "slurm" else "run_local.sh"
    (iteration_dir / "launch" / script_name).write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\necho launch\n", encoding="utf-8"
    )

    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": host_mode,
        "status": "submitted" if host_mode == "slurm" else "running",
        "command": "python -m pkg.train",
        "resource_request": {"cpus": 2, "memory": "8GB", "gpu_count": 0},
        "artifact_sync_to_local": {
            "status": "pending" if host_mode == "slurm" else "ok"
        },
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    if host_mode == "slurm":
        manifest["job_id"] = "12345"
    (iteration_dir / "runs" / "run_001" / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    if include_ledger:
        ledger = repo / "docs" / "slurm_job_list.md"
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(
            "- 2026-01-01 | job_id=12345 | iteration_id=iter1 | run_id=run_001 | status=submitted\n",
            encoding="utf-8",
        )


def test_template_fill_launch_slurm_requires_ledger_from_registry_contract(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="launch", last_run_id="run_001")
    _write_launch_fixture(repo, host_mode="slurm", include_ledger=False)

    result = _run_template_fill(repo, stage="launch")

    assert result.returncode == 1
    assert "docs/slurm_job_list.md" in result.stdout


def test_template_fill_launch_local_does_not_require_slurm_ledger(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="launch", last_run_id="run_001")
    _write_launch_fixture(repo, host_mode="local", include_ledger=False)

    result = _run_template_fill(repo, stage="launch")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "template_fill: PASS" in result.stdout


def test_template_fill_hypothesis_rejects_minimize_with_positive_delta(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="hypothesis")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "hypothesis.md").write_text(
        (
            "# Hypothesis\n\n"
            "PrimaryMetric: validation_loss; Unit: nats; Success: baseline -0.200\n\n"
            "- metric_mode: minimize\n"
            "- target_delta: +0.200\n"
        ),
        encoding="utf-8",
    )

    result = _run_template_fill(repo, stage="hypothesis")

    assert result.returncode == 1
    assert "target_delta must be negative when metric_mode=minimize" in result.stdout


def test_template_fill_implementation_evidence_requires_excerpt_and_command_for_textual_artifacts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="implementation")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "implementation_plan.md").write_text(
        (
            "# Implementation Plan\n\n"
            "- Added deterministic data pipeline wiring.\n\n"
            "- artifact_path: src/pipeline.py\n"
            "  what_it_proves: pipeline code was updated\n"
            "  verifier_output_pointer: .autolab/verification_result.json\n"
        ),
        encoding="utf-8",
    )

    result = _run_template_fill(repo, stage="implementation")

    assert result.returncode == 1
    assert "missing required field 'excerpt'" in result.stdout
    assert "missing required field 'command'" in result.stdout


def test_template_fill_implementation_evidence_allows_binary_artifacts_without_excerpt_or_command(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="implementation")
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "implementation_plan.md").write_text(
        (
            "# Implementation Plan\n\n"
            "- Captured render evidence.\n\n"
            "- artifact_path: artifacts/ablation_plot.png\n"
            "  what_it_proves: visual metric trend after refactor\n"
            "  verifier_output_pointer: .autolab/verification_result.json\n"
        ),
        encoding="utf-8",
    )

    result = _run_template_fill(repo, stage="implementation")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "template_fill: PASS" in result.stdout


def test_template_fill_rejects_registry_required_outputs_with_prompt_mustache_tokens(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    _write_state(repo, stage="launch", last_run_id="run_001")
    _write_launch_fixture(repo, host_mode="local", include_ledger=False)
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

    result = _run_template_fill(repo, stage="launch")

    assert result.returncode == 1
    assert "uses prompt-style mustache token(s)" in result.stdout
