from __future__ import annotations

import json
from pathlib import Path

import yaml

from autolab.design_context_quality import build_design_context_quality


def _write_state(repo: Path) -> dict[str, str]:
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "design",
    }
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def _write_context(repo: Path) -> Path:
    context_dir = repo / ".autolab" / "context"
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    context_dir.mkdir(parents=True, exist_ok=True)
    iteration_dir.mkdir(parents=True, exist_ok=True)
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
    (context_dir / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "generated_at": "2026-03-05T00:00:00Z",
                "focus_iteration_id": "iter1",
                "focus_experiment_id": "e1",
                "project_map_path": ".autolab/context/project_map.json",
                "selected_experiment_delta_path": "experiments/plan/iter1/context_delta.json",
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
                "iteration_id": "iter1",
                "experiment_id": "e1",
                "changed_paths": ["src/model.py"],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return iteration_dir


def _write_design(repo: Path, *, with_context_refs: bool) -> None:
    design = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": "iter1",
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": {"location": "local", "gpu_count": 0},
        "metrics": {
            "primary": {"name": "accuracy", "unit": "%", "mode": "maximize"},
            "secondary": [],
            "success_delta": "+0.1",
            "aggregation": "mean",
            "baseline_comparison": "vs baseline",
        },
        "baselines": [{"name": "baseline", "description": "existing"}],
        "implementation_requirements": [
            {
                "requirement_id": "R1",
                "description": "Implement the experiment-local training path.",
                "scope_kind": "experiment",
                "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            },
            {
                "requirement_id": "R2",
                "description": "Promote a shared-safe constraint when needed.",
                "scope_kind": "project_wide",
                "expected_artifacts": ["implementation_plan.md", "plan_contract.json"],
            },
        ],
        "extract_parser": {
            "kind": "command",
            "command": "python -m tools.extract_results --run-id {run_id}",
        },
    }
    if with_context_refs:
        design["implementation_requirements"][0]["context_refs"] = [
            "experiment:discuss:preferences:pref1"
        ]
        design["implementation_requirements"][1]["promoted_constraints"] = [
            {
                "id": "pc1",
                "source_ref": "experiment:discuss:preferences:pref1",
                "summary": "Carry the chosen experiment preference into the shared code path.",
                "rationale": "The shared parser contract should honor the chosen experiment workflow.",
            }
        ]
    path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(design, sort_keys=False), encoding="utf-8")


def _write_discuss_sidecar(repo: Path) -> None:
    path = (
        repo
        / "experiments"
        / "plan"
        / "iter1"
        / "context"
        / "sidecars"
        / "discuss.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sidecar_kind": "discuss",
                "scope_kind": "experiment",
                "scope_root": str((repo / "experiments" / "plan" / "iter1").resolve()),
                "iteration_id": "iter1",
                "experiment_id": "e1",
                "generated_at": "2026-03-05T00:00:00Z",
                "derived_from": [],
                "stale_if": [],
                "locked_decisions": [],
                "preferences": [
                    {
                        "id": "pref1",
                        "summary": "Keep the implementation patch narrow and reviewable.",
                        "detail": "Do not expand the experiment scope.",
                        "status": "preferred",
                    }
                ],
                "constraints": [],
                "open_questions": [],
                "promotion_candidates": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _write_invalid_discuss_sidecar(repo: Path) -> None:
    path = (
        repo
        / "experiments"
        / "plan"
        / "iter1"
        / "context"
        / "sidecars"
        / "discuss.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sidecar_kind": "discuss",
                "scope_kind": "experiment",
                "scope_root": str((repo / "experiments" / "plan" / "iter1").resolve()),
                "iteration_id": "iter1",
                "experiment_id": "wrong-experiment",
                "generated_at": "2026-03-05T00:00:00Z",
                "derived_from": [],
                "stale_if": [],
                "locked_decisions": [],
                "preferences": [
                    {
                        "id": "pref1",
                        "summary": "This sidecar should be ignored by the resolver.",
                        "detail": "Its experiment identity does not match the active iteration.",
                        "status": "preferred",
                    }
                ],
                "constraints": [],
                "open_questions": [],
                "promotion_candidates": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def test_design_context_quality_reports_absent_context_without_sidecars(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _write_state(repo)
    _write_context(repo)
    _write_design(repo, with_context_refs=False)

    result = build_design_context_quality(repo, state, write_outputs=True)

    assert result.payload["context_mode"] == "absent"
    assert result.payload["score"] == {"value": 0, "max": 4}
    assert result.report_path.exists()


def test_design_context_quality_score_improves_when_discuss_context_is_used(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _write_state(repo)
    _write_context(repo)
    _write_design(repo, with_context_refs=False)
    baseline = build_design_context_quality(repo, state, write_outputs=False)

    _write_discuss_sidecar(repo)
    _write_design(repo, with_context_refs=True)
    improved = build_design_context_quality(repo, state, write_outputs=True)

    assert baseline.payload["context_mode"] == "absent"
    assert improved.payload["context_mode"] == "present"
    assert improved.payload["uptake"]["requirements_with_context_refs"] == 1
    assert improved.payload["uptake"]["requirements_with_resolved_context"] == 2
    assert improved.payload["uptake"]["resolved_context_refs"] == 1
    assert improved.payload["uptake"]["resolved_discuss_context_refs"] == 1
    assert improved.payload["uptake"]["promoted_constraints_total"] == 1
    assert improved.payload["uptake"]["resolved_promoted_constraints"] == 1
    assert improved.payload["score"]["value"] > baseline.payload["score"]["value"]
    assert improved.report_path.exists()


def test_design_context_quality_ignores_invalid_sidecars_for_score_uptake(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _write_state(repo)
    _write_context(repo)
    _write_invalid_discuss_sidecar(repo)
    _write_design(repo, with_context_refs=True)

    result = build_design_context_quality(repo, state, write_outputs=False)

    assert result.payload["context_mode"] == "absent"
    assert result.payload["uptake"]["resolved_context_refs"] == 0
    assert result.payload["uptake"]["resolved_promoted_constraints"] == 0
    assert result.payload["score"]["value"] == 0
    assert any(
        "unresolved design context_ref: experiment:discuss:preferences:pref1"
        == diagnostic
        for diagnostic in result.payload["diagnostics"]
    )
