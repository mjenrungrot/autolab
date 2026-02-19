from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path


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


def _write_state(repo: Path, *, stage: str) -> None:
    state = {
        "iteration_id": "iter1",
        "stage": stage,
        "stage_attempt": 0,
        "last_run_id": "",
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
