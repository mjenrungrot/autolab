from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

import autolab.commands as commands_module


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


def _write_state(repo: Path, *, iteration_id: str = "iter1") -> Path:
    payload = {
        "iteration_id": iteration_id,
        "experiment_id": "e1",
        "stage": "design",
        "stage_attempt": 0,
        "last_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return state_path


def _write_design(repo: Path, *, iteration_id: str = "iter1") -> None:
    payload = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": iteration_id,
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": {"location": "local", "gpu_count": 0},
        "metrics": {
            "primary": {"name": "validation_accuracy", "unit": "%", "mode": "maximize"},
            "secondary": [],
            "success_delta": "+0.01",
            "aggregation": "mean",
            "baseline_comparison": "vs baseline",
        },
        "baselines": [{"name": "baseline_current", "description": "baseline"}],
        "implementation_requirements": [
            {
                "requirement_id": "R1",
                "description": "req",
                "scope_kind": "experiment",
            }
        ],
        "extract_parser": {
            "kind": "command",
            "command": "python -m tools.extract_results --run-id {run_id} --iteration-path {iteration_path}",
        },
    }
    design_path = repo / "experiments" / "plan" / iteration_id / "design.yaml"
    design_path.parent.mkdir(parents=True, exist_ok=True)
    design_path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _setup_repo(tmp_path: Path, *, iteration_id: str = "iter1") -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, iteration_id=iteration_id)
    _write_design(repo, iteration_id=iteration_id)
    return repo, state_path


def test_parser_init_creates_module_and_capability_manifests(tmp_path: Path) -> None:
    repo, state_path = _setup_repo(tmp_path)

    exit_code = commands_module.main(
        ["parser", "init", "--state-file", str(state_path)]
    )

    assert exit_code == 0
    parser_module_path = repo / "parsers" / "iter1_extract_parser.py"
    assert parser_module_path.exists()

    design_path = repo / "experiments" / "plan" / "iter1" / "design.yaml"
    design_payload = yaml.safe_load(design_path.read_text(encoding="utf-8"))
    extract_parser = design_payload.get("extract_parser", {})
    assert extract_parser.get("kind") == "command"
    assert "parsers.iter1_extract_parser" in str(extract_parser.get("command", ""))

    capabilities_path = (
        repo / "experiments" / "plan" / "iter1" / "parser_capabilities.json"
    )
    assert capabilities_path.exists()
    capabilities = json.loads(capabilities_path.read_text(encoding="utf-8"))
    assert capabilities["parser"]["kind"] == "command"
    assert "validation_accuracy" in capabilities["supported_metrics"]

    index_path = repo / ".autolab" / "parser_capabilities.json"
    assert index_path.exists()
    index_payload = json.loads(index_path.read_text(encoding="utf-8"))
    assert "iter1" in index_payload["iterations"]


def test_parser_init_requires_force_for_existing_module(
    tmp_path: Path,
    capsys,
) -> None:
    _repo, state_path = _setup_repo(tmp_path)

    assert (
        commands_module.main(["parser", "init", "--state-file", str(state_path)]) == 0
    )
    assert (
        commands_module.main(["parser", "init", "--state-file", str(state_path)]) == 1
    )
    err = capsys.readouterr().err
    assert "already exists" in err


def test_parser_test_isolated_passes_without_mutating_iteration(tmp_path: Path) -> None:
    repo, state_path = _setup_repo(tmp_path)
    assert (
        commands_module.main(["parser", "init", "--state-file", str(state_path)]) == 0
    )

    exit_code = commands_module.main(
        ["parser", "test", "--state-file", str(state_path)]
    )

    assert exit_code == 0
    # Isolated mode should not write parser test outputs back into the working tree.
    metrics_path = (
        repo
        / "experiments"
        / "plan"
        / "iter1"
        / "runs"
        / "parser_test_run"
        / "metrics.json"
    )
    summary_path = repo / "experiments" / "plan" / "iter1" / "analysis" / "summary.md"
    assert not metrics_path.exists()
    assert not summary_path.exists()


def test_parser_test_fixture_pack_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, iteration_id="iter_placeholder")

    exit_code = commands_module.main(
        [
            "parser",
            "test",
            "--state-file",
            str(state_path),
            "--fixture-pack",
            "command_basic",
        ]
    )

    assert exit_code == 0


def test_parser_test_fixture_pack_python_passes(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo, iteration_id="iter_placeholder")

    exit_code = commands_module.main(
        [
            "parser",
            "test",
            "--state-file",
            str(state_path),
            "--fixture-pack",
            "python_basic",
        ]
    )

    assert exit_code == 0


def test_parser_test_json_mode_outputs_machine_readable_payload(
    tmp_path: Path,
    capsys,
) -> None:
    _repo, state_path = _setup_repo(tmp_path)
    assert (
        commands_module.main(["parser", "init", "--state-file", str(state_path)]) == 0
    )
    capsys.readouterr()

    exit_code = commands_module.main(
        ["parser", "test", "--state-file", str(state_path), "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "autolab parser test"
    assert payload["mode"] == "iteration"
    assert payload["workspace_mode"] == "isolated"
    assert payload["passed"] is True
    assert payload["issues"] == []


def test_parser_test_in_place_writes_outputs_to_iteration(tmp_path: Path) -> None:
    repo, state_path = _setup_repo(tmp_path)
    assert (
        commands_module.main(["parser", "init", "--state-file", str(state_path)]) == 0
    )

    run_id = "run_in_place"
    exit_code = commands_module.main(
        [
            "parser",
            "test",
            "--state-file",
            str(state_path),
            "--run-id",
            run_id,
            "--in-place",
        ]
    )

    assert exit_code == 0
    metrics_path = (
        repo / "experiments" / "plan" / "iter1" / "runs" / run_id / "metrics.json"
    )
    summary_path = repo / "experiments" / "plan" / "iter1" / "analysis" / "summary.md"
    assert metrics_path.exists()
    assert summary_path.exists()
    metrics_payload = json.loads(metrics_path.read_text(encoding="utf-8"))
    assert metrics_payload["primary_metric"]["name"] == "validation_accuracy"


def test_parser_test_fails_on_capability_metric_mismatch(
    tmp_path: Path,
    capsys,
) -> None:
    repo, state_path = _setup_repo(tmp_path)
    assert (
        commands_module.main(["parser", "init", "--state-file", str(state_path)]) == 0
    )

    capabilities_path = (
        repo / "experiments" / "plan" / "iter1" / "parser_capabilities.json"
    )
    capabilities = json.loads(capabilities_path.read_text(encoding="utf-8"))
    capabilities["supported_metrics"] = ["other_metric"]
    capabilities_path.write_text(
        json.dumps(capabilities, indent=2) + "\n", encoding="utf-8"
    )

    exit_code = commands_module.main(
        ["parser", "test", "--state-file", str(state_path), "--run-id", "run_mismatch"]
    )

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "parser capability mismatch" in out
