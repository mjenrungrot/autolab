"""Tests for the stage skip guard — force execution when required outputs are missing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from autolab.run_standard import _stage_outputs_satisfied


def _seed_workflow(repo: Path, *, stages: dict) -> None:
    """Write a minimal workflow.yaml with the given stages block."""
    workflow_path = repo / ".autolab" / "workflow.yaml"
    workflow_path.parent.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(
        yaml.safe_dump({"stages": stages}, sort_keys=False),
        encoding="utf-8",
    )


def _base_state(*, iteration_id: str, last_run_id: str = "") -> dict[str, object]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": "e1",
        "stage": "extract_results",
        "last_run_id": last_run_id,
    }


def test_stage_outputs_satisfied_missing_files(tmp_path: Path) -> None:
    """When required output files are absent, _stage_outputs_satisfied returns False."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)

    _seed_workflow(
        repo,
        stages={
            "extract_results": {
                "prompt_file": "stage_extract_results.md",
                "required_tokens": ["iteration_id"],
                "required_outputs": [
                    "runs/<RUN_ID>/metrics.json",
                    "analysis/summary.md",
                ],
                "next_stage": "update_docs",
                "verifier_categories": {},
            }
        },
    )

    state = _base_state(iteration_id="iter1", last_run_id="run_001")
    assert _stage_outputs_satisfied(repo, state, "extract_results") is False


def test_stage_outputs_satisfied_all_present(tmp_path: Path) -> None:
    """When all required output files exist, _stage_outputs_satisfied returns True."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)

    _seed_workflow(
        repo,
        stages={
            "extract_results": {
                "prompt_file": "stage_extract_results.md",
                "required_tokens": ["iteration_id"],
                "required_outputs": [
                    "runs/<RUN_ID>/metrics.json",
                    "analysis/summary.md",
                ],
                "next_stage": "update_docs",
                "verifier_categories": {},
            }
        },
    )

    # Create the required output files
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "metrics.json").write_text('{"accuracy": 0.95}\n', encoding="utf-8")
    analysis_dir = iteration_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "summary.md").write_text(
        "# Summary\nResults look good.\n", encoding="utf-8"
    )

    state = _base_state(iteration_id="iter1", last_run_id="run_001")
    assert _stage_outputs_satisfied(repo, state, "extract_results") is True


def test_stage_outputs_satisfied_empty_file(tmp_path: Path) -> None:
    """A zero-byte required output counts as missing."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)

    _seed_workflow(
        repo,
        stages={
            "extract_results": {
                "prompt_file": "stage_extract_results.md",
                "required_tokens": ["iteration_id"],
                "required_outputs": ["analysis/summary.md"],
                "next_stage": "update_docs",
                "verifier_categories": {},
            }
        },
    )

    analysis_dir = iteration_dir / "analysis"
    analysis_dir.mkdir(parents=True, exist_ok=True)
    (analysis_dir / "summary.md").write_text("", encoding="utf-8")

    state = _base_state(iteration_id="iter1", last_run_id="run_001")
    assert _stage_outputs_satisfied(repo, state, "extract_results") is False


def test_stage_outputs_satisfied_no_required_outputs(tmp_path: Path) -> None:
    """Stage with no required_outputs is always satisfied."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_workflow(
        repo,
        stages={
            "design": {
                "prompt_file": "stage_design.md",
                "required_tokens": ["iteration_id"],
                "required_outputs": [],
                "next_stage": "launch",
                "verifier_categories": {},
            }
        },
    )
    state = _base_state(iteration_id="iter1")
    assert _stage_outputs_satisfied(repo, state, "design") is True


def test_stage_outputs_satisfied_unknown_stage(tmp_path: Path) -> None:
    """Unknown stage returns True (nothing to enforce)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_workflow(repo, stages={})
    state = _base_state(iteration_id="iter1")
    assert _stage_outputs_satisfied(repo, state, "nonexistent_stage") is True
