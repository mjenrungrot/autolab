from __future__ import annotations

import json
from pathlib import Path

import pytest

from autolab.models import StageCheckError
from autolab.scope import _resolve_project_wide_root, _resolve_scope_context


def _write_scope_policy(repo: Path, project_wide_root: str) -> None:
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        f"scope_roots:\n  project_wide_root: {project_wide_root}\n",
        encoding="utf-8",
    )


def test_resolve_project_wide_root_defaults_to_repo_root(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    resolved = _resolve_project_wide_root(repo)

    assert resolved == repo.resolve()


def test_resolve_project_wide_root_uses_configured_relative_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    _write_scope_policy(repo, "src")

    resolved = _resolve_project_wide_root(repo)

    assert resolved == (repo / "src").resolve()


@pytest.mark.parametrize(
    "configured_root",
    (
        "/tmp/absolute",
        "../outside",
        "missing_dir",
    ),
)
def test_resolve_project_wide_root_rejects_invalid_paths(
    tmp_path: Path,
    configured_root: str,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_scope_policy(repo, configured_root)

    with pytest.raises(StageCheckError):
        _resolve_project_wide_root(repo)


def test_resolve_scope_context_uses_project_wide_root_for_project_wide_tasks(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    _write_scope_policy(repo, "src")
    autolab_dir = repo / ".autolab"
    autolab_dir.mkdir(parents=True, exist_ok=True)
    (repo / "experiments" / "plan" / "iter1").mkdir(parents=True, exist_ok=True)
    (autolab_dir / "plan_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "tasks": [{"task_id": "T1", "scope_kind": "project_wide"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    scope_kind, scope_root, iteration_dir = _resolve_scope_context(
        repo,
        iteration_id="iter1",
        experiment_id="e1",
    )

    assert scope_kind == "project_wide"
    assert scope_root == (repo / "src").resolve()
    assert iteration_dir == (repo / "experiments" / "plan" / "iter1")


def test_resolve_scope_context_prefers_iteration_snapshot_over_stale_root_contract(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "src").mkdir()
    _write_scope_policy(repo, "src")
    autolab_dir = repo / ".autolab"
    autolab_dir.mkdir(parents=True, exist_ok=True)
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (autolab_dir / "plan_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter_stale",
                "tasks": [{"task_id": "T_stale", "scope_kind": "project_wide"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (iteration_dir / "plan_contract.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "iteration_id": "iter1",
                "tasks": [{"task_id": "T_exp", "scope_kind": "experiment"}],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    scope_kind, scope_root, resolved_iteration_dir = _resolve_scope_context(
        repo,
        iteration_id="iter1",
        experiment_id="e1",
    )

    assert scope_kind == "experiment"
    assert scope_root == iteration_dir
    assert resolved_iteration_dir == iteration_dir
