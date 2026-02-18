from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml

import autolab.commands as commands_module


def _copy_scaffold(repo: Path) -> None:
    source = Path(__file__).resolve().parents[1] / "src" / "autolab" / "scaffold" / ".autolab"
    target = repo / ".autolab"
    shutil.copytree(source, target, dirs_exist_ok=True)


def _write_state(repo: Path) -> Path:
    state = {
        "iteration_id": "iter1",
        "experiment_id": "e1",
        "stage": "design",
        "stage_attempt": 0,
        "last_run_id": "",
        "sync_status": "na",
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
    }
    path = repo / ".autolab" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    return path


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
        "id": "e1",
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


def test_verify_command_writes_summary_artifact(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    _write_design(repo)

    exit_code = commands_module.main(["verify", "--state-file", str(state_path)])

    assert exit_code == 0
    summaries = sorted((repo / ".autolab" / "logs").glob("verification_*.json"))
    assert summaries, "expected verification summary artifact"
    latest = json.loads(summaries[-1].read_text(encoding="utf-8"))
    assert latest["passed"] is True
    assert latest["stage_effective"] == "design"


def test_run_with_verify_blocks_stage_transition_on_verification_failure(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _copy_scaffold(repo)
    state_path = _write_state(repo)
    _write_backlog(repo)
    _write_agent_result(repo)
    # Intentionally skip design.yaml to force verifier failure.

    exit_code = commands_module.main(
        [
            "run",
            "--state-file",
            str(state_path),
            "--verify",
            "--no-run-agent",
        ]
    )

    assert exit_code == 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["stage"] == "design"
    assert state["stage_attempt"] == 1
