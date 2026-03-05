from __future__ import annotations

import json
from pathlib import Path

import yaml

import autolab.commands as commands_module


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_json(path: Path, payload: dict) -> None:
    _write(path, json.dumps(payload, indent=2) + "\n")


def _seed_existing_iteration(
    repo: Path,
    *,
    iteration_id: str,
    experiment_id: str,
    hypothesis_id: str,
    experiment_type: str = "plan",
    run_id: str = "20260201T120000Z_demo",
) -> None:
    iteration_dir = repo / "experiments" / experiment_type / iteration_id
    iteration_dir.mkdir(parents=True, exist_ok=True)

    design_payload = {
        "schema_version": "1.0",
        "id": experiment_id,
        "iteration_id": iteration_id,
        "hypothesis_id": hypothesis_id,
        "entrypoint": {"module": "trainer.main", "args": {"config": "config.yaml"}},
        "compute": {
            "location": "local",
            "walltime_estimate": "00:30:00",
            "memory_estimate": "8GB",
            "gpu_count": 0,
        },
        "metrics": {
            "primary": {"name": "accuracy", "unit": "%", "mode": "maximize"},
            "secondary": [],
            "success_delta": 1.5,
            "aggregation": "mean",
            "baseline_comparison": "baseline_v1",
        },
        "baselines": [{"name": "baseline_v1", "description": "initial baseline"}],
    }
    _write(
        iteration_dir / "design.yaml",
        yaml.safe_dump(design_payload, sort_keys=False),
    )
    _write(
        iteration_dir / "hypothesis.md",
        "# Hypothesis\n\nPrimaryMetric: accuracy; Unit: %; Success: baseline +1.5\n",
    )
    _write(iteration_dir / "implementation_plan.md", "# Implementation Plan\n")
    _write(iteration_dir / "analysis" / "summary.md", "# Analysis\n")

    run_dir = iteration_dir / "runs" / run_id
    run_manifest_payload = {
        "schema_version": "1.0",
        "run_id": run_id,
        "iteration_id": iteration_id,
        "host_mode": "local",
        "command": "python -m trainer.main",
        "resource_request": {"cpus": 1, "memory": "8GB", "gpu_count": 0},
        "status": "completed",
        "artifact_sync_to_local": {"status": "completed"},
        "timestamps": {
            "started_at": "2026-02-01T12:00:00Z",
            "completed_at": "2026-02-01T12:30:00Z",
        },
    }
    _write_json(run_dir / "run_manifest.json", run_manifest_payload)
    _write_json(
        run_dir / "metrics.json",
        {
            "schema_version": "1.0",
            "iteration_id": iteration_id,
            "run_id": run_id,
            "status": "completed",
            "primary_metric": {
                "name": "accuracy",
                "value": 88.2,
                "delta_vs_baseline": 1.7,
            },
        },
    )


def test_init_from_existing_replaces_bootstrap_backlog_and_writes_context(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_existing_iteration(
        repo,
        iteration_id="iter_alpha",
        experiment_id="exp_alpha",
        hypothesis_id="h_alpha",
    )
    state_path = repo / ".autolab" / "state.json"

    exit_code = commands_module.main(
        ["init", "--state-file", str(state_path), "--no-interactive", "--from-existing"]
    )
    assert exit_code == 0

    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_payload["iteration_id"] == "iter_alpha"
    assert state_payload["experiment_id"] == "exp_alpha"

    backlog_payload = yaml.safe_load(
        (repo / ".autolab" / "backlog.yaml").read_text(encoding="utf-8")
    )
    experiments = backlog_payload["experiments"]
    hypotheses = backlog_payload["hypotheses"]
    assert any(entry["id"] == "exp_alpha" for entry in experiments)
    assert any(entry["id"] == "h_alpha" for entry in hypotheses)
    assert all(
        str(entry.get("title", "")).strip() != "Bootstrap hypothesis"
        for entry in hypotheses
    )

    project_map_path = repo / ".autolab" / "context" / "project_map.json"
    delta_map_path = repo / "experiments" / "plan" / "iter_alpha" / "context_delta.json"
    bundle_path = repo / ".autolab" / "context" / "bundle.json"
    assert project_map_path.exists()
    assert delta_map_path.exists()
    assert bundle_path.exists()

    bundle_payload = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle_payload["project_map_path"] == ".autolab/context/project_map.json"
    assert bundle_payload["focus_iteration_id"] == "iter_alpha"
    assert bundle_payload["focus_experiment_id"] == "exp_alpha"
    assert bundle_payload["selected_experiment_delta_path"].endswith(
        "experiments/plan/iter_alpha/context_delta.json"
    )

    policy_payload = yaml.safe_load(
        (repo / ".autolab" / "verifier_policy.yaml").read_text(encoding="utf-8")
    )
    bootstrap_meta = policy_payload.get("bootstrap", {}).get("from_existing", {})
    assert bootstrap_meta.get("focus_iteration_id") == "iter_alpha"
    assert bootstrap_meta.get("focus_experiment_id") == "exp_alpha"
    assert bootstrap_meta.get("scan_mode") == "fast_heuristic"


def test_init_from_existing_appends_when_backlog_is_not_placeholder(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _seed_existing_iteration(
        repo,
        iteration_id="iter_beta",
        experiment_id="exp_beta",
        hypothesis_id="h_beta",
    )
    backlog_payload = {
        "hypotheses": [
            {
                "id": "h_custom",
                "status": "open",
                "title": "Custom hypothesis",
                "success_metric": "f1",
                "target_delta": 0.5,
            }
        ],
        "experiments": [
            {
                "id": "exp_custom",
                "hypothesis_id": "h_custom",
                "status": "open",
                "type": "plan",
                "iteration_id": "iter_custom",
            }
        ],
    }
    _write(
        repo / ".autolab" / "backlog.yaml",
        yaml.safe_dump(backlog_payload, sort_keys=False),
    )
    state_path = repo / ".autolab" / "state.json"

    exit_code = commands_module.main(
        ["init", "--state-file", str(state_path), "--no-interactive", "--from-existing"]
    )
    assert exit_code == 0

    updated_backlog = yaml.safe_load(
        (repo / ".autolab" / "backlog.yaml").read_text(encoding="utf-8")
    )
    experiment_ids = {entry["id"] for entry in updated_backlog["experiments"]}
    hypothesis_ids = {entry["id"] for entry in updated_backlog["hypotheses"]}
    assert "exp_custom" in experiment_ids
    assert "h_custom" in hypothesis_ids
    assert "exp_beta" in experiment_ids
    assert "h_beta" in hypothesis_ids

    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert state_payload["iteration_id"] == "iter_custom"
