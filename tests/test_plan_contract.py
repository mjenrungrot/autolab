from __future__ import annotations

import json
from pathlib import Path

import yaml

from autolab.plan_contract import check_implementation_plan_contract


def _write_state(repo: Path) -> dict[str, str | int]:
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "implementation",
        "stage_attempt": 0,
        "max_stage_attempts": 3,
    }
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    return state


def _write_design(repo: Path) -> Path:
    path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "id": "e1",
                "iteration_id": "iter1",
                "hypothesis_id": "h1",
                "entrypoint": {"module": "pkg.train", "args": {}},
                "compute": {"location": "local", "gpu_count": 0},
                "metrics": {
                    "primary": {
                        "name": "accuracy",
                        "unit": "%",
                        "mode": "maximize",
                    },
                    "secondary": [],
                    "success_delta": "+0.1",
                    "aggregation": "mean",
                    "baseline_comparison": "vs baseline",
                },
                "baselines": [{"name": "baseline", "description": "existing"}],
                "implementation_requirements": [
                    {
                        "requirement_id": "R_exp",
                        "description": "Experiment-only change.",
                        "scope_kind": "experiment",
                        "expected_artifacts": [
                            "implementation_plan.md",
                            "plan_contract.json",
                        ],
                    },
                    {
                        "requirement_id": "R_shared",
                        "description": "Shared project-wide change.",
                        "scope_kind": "project_wide",
                        "expected_artifacts": [
                            "implementation_plan.md",
                            "plan_contract.json",
                        ],
                    },
                ],
                "extract_parser": {
                    "kind": "command",
                    "command": "python -m tools.extract_results --run-id {run_id}",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return path


def _write_contract(repo: Path, payload: dict[str, object]) -> None:
    canonical = repo / ".autolab" / "plan_contract.json"
    snapshot = repo / "experiments" / "plan" / "iter1" / "plan_contract.json"
    canonical.parent.mkdir(parents=True, exist_ok=True)
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2) + "\n"
    canonical.write_text(text, encoding="utf-8")
    snapshot.write_text(text, encoding="utf-8")


def _write_experiment_discuss_sidecar(
    repo: Path,
    *,
    experiment_id: str = "e1",
) -> None:
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
                "scope_root": "experiments/plan/iter1",
                "iteration_id": "iter1",
                "experiment_id": experiment_id,
                "generated_at": "2026-03-05T00:00:00Z",
                "derived_from": [],
                "stale_if": [],
                "locked_decisions": [],
                "preferences": [
                    {
                        "id": "pref1",
                        "summary": "Keep the patch narrow.",
                        "detail": "This should only resolve when the active experiment identity matches.",
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


def test_plan_contract_rejects_wrong_scope_requirement_mapping(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _write_state(repo)
    _write_design(repo)
    _write_contract(
        repo,
        {
            "schema_version": "1.0",
            "iteration_id": "iter1",
            "stage": "implementation",
            "generated_at": "2026-03-05T00:00:00Z",
            "tasks": [
                {
                    "task_id": "T_exp",
                    "objective": "Incorrectly tries to satisfy project-wide work from experiment scope.",
                    "scope_kind": "experiment",
                    "depends_on": [],
                    "reads": ["experiments/plan/iter1/design.yaml"],
                    "writes": ["experiments/plan/iter1/implementation_plan.md"],
                    "touches": ["experiments/plan/iter1/implementation_plan.md"],
                    "conflict_group": "",
                    "verification_commands": ["python -m pytest -q"],
                    "expected_artifacts": [
                        "implementation_plan.md",
                        "plan_contract.json",
                    ],
                    "failure_policy": "fail_fast",
                    "can_run_in_parallel": False,
                    "covers_requirements": ["R_shared"],
                }
            ],
        },
    )

    passed, _message, details = check_implementation_plan_contract(
        repo, state, write_outputs=False
    )

    assert passed is False
    assert any(
        "design requirement 'R_shared' scope_kind=project_wide is mapped by wrong-scope task(s): T_exp"
        in error
        for error in details["errors"]
    )


def test_plan_contract_rejects_non_experiment_promotion_source(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _write_state(repo)
    _write_design(repo)
    project_sidecar_path = (
        repo / ".autolab" / "context" / "sidecars" / "project_wide" / "discuss.json"
    )
    project_sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    project_sidecar_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sidecar_kind": "discuss",
                "scope_kind": "project_wide",
                "scope_root": ".",
                "generated_at": "2026-03-05T00:00:00Z",
                "derived_from": [],
                "stale_if": [],
                "locked_decisions": [
                    {
                        "id": "decision-1",
                        "summary": "Shared decision",
                        "detail": "This should not be a promotion source.",
                    }
                ],
                "preferences": [],
                "constraints": [],
                "open_questions": [],
                "promotion_candidates": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_contract(
        repo,
        {
            "schema_version": "1.0",
            "iteration_id": "iter1",
            "stage": "implementation",
            "generated_at": "2026-03-05T00:00:00Z",
            "tasks": [
                {
                    "task_id": "T_shared",
                    "objective": "Shared task with invalid promotion source.",
                    "scope_kind": "project_wide",
                    "depends_on": [],
                    "reads": [],
                    "writes": ["src/shared.py"],
                    "touches": ["src/shared.py"],
                    "conflict_group": "",
                    "verification_commands": ["python -m pytest -q"],
                    "expected_artifacts": [
                        "implementation_plan.md",
                        "plan_contract.json",
                    ],
                    "failure_policy": "fail_fast",
                    "can_run_in_parallel": False,
                    "covers_requirements": ["R_shared"],
                    "promotion_source": "project_wide:discuss:locked_decisions:decision-1",
                    "promotion_scope_ok": True,
                }
            ],
        },
    )

    passed, _message, details = check_implementation_plan_contract(
        repo, state, write_outputs=False
    )

    assert passed is False
    assert any(
        "promotion_source 'project_wide:discuss:locked_decisions:decision-1' must target an experiment sidecar item"
        in error
        for error in details["errors"]
    )


def test_plan_contract_rejects_invalid_experiment_sidecar_context_inputs(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _write_state(repo)
    _write_design(repo)
    _write_experiment_discuss_sidecar(repo, experiment_id="wrong-experiment")
    _write_contract(
        repo,
        {
            "schema_version": "1.0",
            "iteration_id": "iter1",
            "stage": "implementation",
            "generated_at": "2026-03-05T00:00:00Z",
            "tasks": [
                {
                    "task_id": "T_exp",
                    "objective": "Experiment task with stale or invalid sidecar lineage.",
                    "scope_kind": "experiment",
                    "depends_on": [],
                    "reads": ["experiments/plan/iter1/design.yaml"],
                    "writes": ["experiments/plan/iter1/implementation_plan.md"],
                    "touches": ["experiments/plan/iter1/implementation_plan.md"],
                    "conflict_group": "",
                    "verification_commands": ["python -m pytest -q"],
                    "expected_artifacts": [
                        "implementation_plan.md",
                        "plan_contract.json",
                    ],
                    "failure_policy": "fail_fast",
                    "can_run_in_parallel": False,
                    "covers_requirements": ["R_exp"],
                    "context_inputs": ["experiment:discuss:preferences:pref1"],
                }
            ],
        },
    )

    passed, _message, details = check_implementation_plan_contract(
        repo, state, write_outputs=False
    )

    assert passed is False
    assert any(
        "context_inputs ref 'experiment:discuss:preferences:pref1' could not be resolved"
        in error
        for error in details["errors"]
    )


def test_plan_contract_requires_project_wide_tasks_to_consume_promoted_constraints(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _write_state(repo)
    design_path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    design_path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text(
        yaml.safe_dump(
            {
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
                        "requirement_id": "R_shared",
                        "description": "Shared requirement promoted from experiment context.",
                        "scope_kind": "project_wide",
                        "promoted_constraints": [
                            {
                                "id": "pc1",
                                "source_ref": "experiment:discuss:constraints:c1",
                                "summary": "Keep the shared interface stable.",
                                "rationale": "Project-wide work must only consume promoted constraints.",
                            }
                        ],
                        "expected_artifacts": ["implementation_plan.md"],
                    }
                ],
                "extract_parser": {
                    "kind": "command",
                    "command": "python -m tools.extract_results --run-id {run_id}",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    sidecar_path = (
        repo
        / "experiments"
        / "plan"
        / "iter1"
        / "context"
        / "sidecars"
        / "discuss.json"
    )
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "sidecar_kind": "discuss",
                "scope_kind": "experiment",
                "scope_root": "experiments/plan/iter1",
                "iteration_id": "iter1",
                "experiment_id": "e1",
                "generated_at": "2026-03-05T00:00:00Z",
                "derived_from": [],
                "stale_if": [],
                "locked_decisions": [],
                "preferences": [],
                "constraints": [
                    {
                        "id": "c1",
                        "summary": "Stable interface",
                        "detail": "Do not break the shared module contract.",
                    }
                ],
                "open_questions": [],
                "promotion_candidates": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_contract(
        repo,
        {
            "schema_version": "1.0",
            "iteration_id": "iter1",
            "stage": "implementation",
            "generated_at": "2026-03-05T00:00:00Z",
            "tasks": [
                {
                    "task_id": "T_shared",
                    "objective": "Shared task missing promoted constraint consumption.",
                    "scope_kind": "project_wide",
                    "depends_on": [],
                    "reads": ["src/shared.py"],
                    "writes": ["src/shared.py"],
                    "touches": ["src/shared.py"],
                    "conflict_group": "",
                    "verification_commands": ["python -m pytest -q"],
                    "expected_artifacts": ["implementation_plan.md"],
                    "failure_policy": "fail_fast",
                    "can_run_in_parallel": False,
                    "covers_requirements": ["R_shared"],
                    "promotion_scope_ok": True,
                }
            ],
        },
    )

    passed, _message, details = check_implementation_plan_contract(
        repo, state, write_outputs=False
    )

    assert passed is False
    assert any(
        "project_wide requirement 'R_shared' missing promoted context inputs: promoted:R_shared:pc1"
        in error
        for error in details["errors"]
    )


def test_plan_contract_rejects_promotion_source_mismatch_for_consumed_constraint(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state = _write_state(repo)
    _write_experiment_discuss_sidecar(repo, experiment_id="e1")
    design_path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    design_path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text(
        yaml.safe_dump(
            {
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
                        "requirement_id": "R_shared",
                        "description": "Shared requirement promoted from experiment context.",
                        "scope_kind": "project_wide",
                        "promoted_constraints": [
                            {
                                "id": "pc1",
                                "source_ref": "experiment:discuss:preferences:pref1",
                                "summary": "Keep the patch narrow.",
                                "rationale": "Shared work must only use promoted inputs.",
                            }
                        ],
                        "expected_artifacts": ["implementation_plan.md"],
                    }
                ],
                "extract_parser": {
                    "kind": "command",
                    "command": "python -m tools.extract_results --run-id {run_id}",
                },
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    _write_contract(
        repo,
        {
            "schema_version": "1.0",
            "iteration_id": "iter1",
            "stage": "implementation",
            "generated_at": "2026-03-05T00:00:00Z",
            "tasks": [
                {
                    "task_id": "T_shared",
                    "objective": "Shared task with wrong promotion source.",
                    "scope_kind": "project_wide",
                    "depends_on": [],
                    "reads": ["src/shared.py"],
                    "writes": ["src/shared.py"],
                    "touches": ["src/shared.py"],
                    "conflict_group": "",
                    "verification_commands": ["python -m pytest -q"],
                    "expected_artifacts": ["implementation_plan.md"],
                    "failure_policy": "fail_fast",
                    "can_run_in_parallel": False,
                    "covers_requirements": ["R_shared"],
                    "context_inputs": ["promoted:R_shared:pc1"],
                    "promotion_source": "experiment:discuss:preferences:missing",
                    "promotion_scope_ok": True,
                }
            ],
        },
    )

    passed, _message, details = check_implementation_plan_contract(
        repo, state, write_outputs=False
    )

    assert passed is False
    assert any(
        "promotion_source 'experiment:discuss:preferences:missing' could not be resolved"
        in error
        or "must match a consumed promoted constraint source_ref" in error
        for error in details["errors"]
    )
