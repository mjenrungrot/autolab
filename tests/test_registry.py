"""Tests for autolab.registry — StageSpec loading and convenience accessors."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from autolab.constants import (
    ACTIVE_STAGES,
    ALL_STAGES,
    DECISION_STAGES,
    PROMPT_REQUIRED_TOKENS_BY_STAGE,
    RUNNER_ELIGIBLE_STAGES,
    STAGE_PROMPT_FILES,
    TERMINAL_STAGES,
)
from autolab.registry import (
    StageSpec,
    load_registry,
    registry_active_stages,
    registry_all_stages,
    registry_decision_stages,
    registry_prompt_files,
    registry_required_tokens,
    registry_runner_eligible,
    registry_terminal_stages,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCAFFOLD_DIR = _REPO_ROOT / "src" / "autolab" / "scaffold" / ".autolab"


def _copy_scaffold(repo: Path) -> None:
    target = repo / ".autolab"
    shutil.copytree(_SCAFFOLD_DIR, target, dirs_exist_ok=True)


def test_load_registry_from_scaffold(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    assert isinstance(registry, dict)
    assert len(registry) > 0
    for name, spec in registry.items():
        assert isinstance(spec, StageSpec)
        assert spec.name == name


def test_load_registry_returns_empty_without_yaml(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autolab").mkdir()
    assert load_registry(repo) == {}


def test_registry_matches_constants_active_stages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    assert registry_active_stages(registry) == ACTIVE_STAGES


def test_registry_matches_constants_terminal_stages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    assert set(registry_terminal_stages(registry)) == set(TERMINAL_STAGES)


def test_registry_matches_constants_decision_stages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    assert set(registry_decision_stages(registry)) == set(DECISION_STAGES)


def test_registry_matches_constants_runner_eligible(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    assert set(registry_runner_eligible(registry)) == set(RUNNER_ELIGIBLE_STAGES)


def test_registry_matches_constants_all_stages(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    assert registry_all_stages(registry) == ALL_STAGES


def test_registry_matches_constants_prompt_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    assert registry_prompt_files(registry) == STAGE_PROMPT_FILES


def test_registry_matches_constants_required_tokens(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    reg_tokens = registry_required_tokens(registry)
    for stage, expected_tokens in PROMPT_REQUIRED_TOKENS_BY_STAGE.items():
        assert reg_tokens.get(stage) == expected_tokens, f"mismatch for stage '{stage}'"


def test_stage_spec_frozen() -> None:
    spec = StageSpec(
        name="test",
        prompt_file="stage_test.md",
        required_tokens=frozenset(["a"]),
        required_outputs=("out.json",),
        next_stage="next",
        verifier_categories={"schema": True},
    )
    with pytest.raises(AttributeError):
        spec.name = "changed"  # type: ignore[misc]


def test_registry_verifier_categories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    impl_review = registry.get("implementation_review")
    assert impl_review is not None
    assert impl_review.verifier_categories["dry_run"] is True
    assert impl_review.verifier_categories["env_smoke"] is True
    assert impl_review.verifier_categories["tests"] is False
    assert impl_review.verifier_categories["prompt_lint"] is True
    assert impl_review.verifier_categories["consistency"] is True


def test_launch_requires_run_id_token(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    launch = registry.get("launch")
    decide_repeat = registry.get("decide_repeat")
    assert launch is not None
    assert decide_repeat is not None
    assert "run_id" in launch.required_tokens
    assert "run_id" in decide_repeat.required_tokens


def test_registry_optional_tokens_are_loaded(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)

    launch = registry.get("launch")
    implementation_review = registry.get("implementation_review")
    assert launch is not None
    assert implementation_review is not None
    assert "launch_execute" in launch.optional_tokens
    assert "verifier_outputs" in implementation_review.optional_tokens


def test_registry_run_scoped_required_outputs_use_run_id_pattern(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)

    launch = registry.get("launch")
    slurm_monitor = registry.get("slurm_monitor")
    extract_results = registry.get("extract_results")

    assert launch is not None
    assert slurm_monitor is not None
    assert extract_results is not None
    assert launch.required_outputs == ("runs/<RUN_ID>/run_manifest.json",)
    assert slurm_monitor.required_outputs == ("runs/<RUN_ID>/run_manifest.json",)
    assert extract_results.required_outputs == (
        "runs/<RUN_ID>/metrics.json",
        "analysis/summary.md",
    )


def test_launch_registry_conditional_outputs_contract(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)

    launch = registry.get("launch")
    assert launch is not None
    assert launch.required_outputs_any_of == (
        ("launch/run_local.sh", "launch/run_slurm.sbatch"),
    )
    assert launch.required_outputs_if == (
        (
            (("host_mode", "slurm"),),
            ("docs/slurm_job_list.md",),
        ),
    )


def test_workflow_stages_subset_of_state_schema_enum(tmp_path: Path) -> None:
    """Every stage in workflow.yaml must appear in state.schema.json stage enum."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    workflow_stages = set(registry.keys())

    schema_path = repo / ".autolab" / "schemas" / "state.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_stages = set(schema["properties"]["stage"]["enum"])

    missing = workflow_stages - schema_stages
    assert not missing, (
        f"workflow.yaml stages missing from state.schema.json enum: {sorted(missing)}"
    )


def test_state_schema_stage_enum_subset_of_workflow(tmp_path: Path) -> None:
    """Every state.schema.json stage enum value must exist in workflow.yaml."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    registry = load_registry(repo)
    workflow_stages = set(registry.keys())

    schema_path = repo / ".autolab" / "schemas" / "state.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_stages = set(schema["properties"]["stage"]["enum"])

    orphans = schema_stages - workflow_stages
    assert not orphans, (
        f"state.schema.json enum values not in workflow.yaml: {sorted(orphans)}"
    )
