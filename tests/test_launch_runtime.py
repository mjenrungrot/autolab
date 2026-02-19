from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from autolab.launch_runtime import _execute_launch_runtime
from autolab.models import StageCheckError


def _seed_design(iteration_dir: Path, *, mode: str) -> None:
    payload = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": iteration_dir.name,
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": {"location": mode, "cpus": 1, "gpus": 0},
        "metrics": {"primary": {"name": "accuracy", "mode": "maximize"}},
        "baselines": [{"name": "baseline", "value": 0.0}],
    }
    (iteration_dir / "design.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _seed_scripts(iteration_dir: Path, *, local_script: str = "echo local\n") -> None:
    launch_dir = iteration_dir / "launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    (launch_dir / "run_local.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + local_script,
        encoding="utf-8",
    )
    (launch_dir / "run_slurm.sbatch").write_text(
        "#!/usr/bin/env bash\n#SBATCH --job-name=test\necho slurm\n",
        encoding="utf-8",
    )


def _seed_policy(repo: Path, *, launch_block: dict[str, object]) -> None:
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        yaml.safe_dump({"launch": launch_block}, sort_keys=False),
        encoding="utf-8",
    )


def _base_state(*, iteration_id: str, pending_run_id: str) -> dict[str, object]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": "e1",
        "stage": "launch",
        "stage_attempt": 0,
        "last_run_id": "",
        "pending_run_id": pending_run_id,
        "sync_status": "",
        "run_group": [],
        "max_stage_attempts": 3,
        "max_total_iterations": 20,
        "assistant_mode": "off",
        "current_task_id": "",
        "task_cycle_stage": "select",
        "repeat_guard": {
            "last_decision": "",
            "same_decision_streak": 0,
            "last_open_task_count": -1,
            "no_progress_decisions": 0,
            "update_docs_cycle_count": 0,
            "last_verification_passed": False,
        },
        "task_change_baseline": {},
        "history": [],
    }


def test_execute_launch_runtime_local_success(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(iteration_dir, local_script="echo hello-local\n")

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)

    assert result.run_id == "run_001"
    assert state["last_run_id"] == "run_001"
    assert state["sync_status"] == "completed"
    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["host_mode"] == "local"
    assert payload["artifact_sync_to_local"]["status"] == "ok"
    assert (iteration_dir / "runs" / "run_001" / "logs" / "launch.stdout.log").exists()
    assert (iteration_dir / "runs" / "run_001" / "logs" / "launch.stderr.log").exists()


def test_execute_launch_runtime_local_failure_writes_failed_manifest(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(iteration_dir, local_script="exit 2\n")

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    with pytest.raises(StageCheckError):
        _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["artifact_sync_to_local"]["status"] == "failed"


def test_execute_launch_runtime_slurm_submit_success(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="slurm")
    _seed_scripts(iteration_dir)

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["sbatch", "launch/run_slurm.sbatch"],
            returncode=0,
            stdout="Submitted batch job 12345\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)

    assert result.run_id == "run_001"
    assert state["sync_status"] == "pending"
    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "submitted"
    assert payload["job_id"] == "12345"
    ledger = repo / "docs" / "slurm_job_list.md"
    assert ledger.exists()
    assert "run_id=run_001" in ledger.read_text(encoding="utf-8")


def test_execute_launch_runtime_slurm_submit_missing_job_id_fails(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="slurm")
    _seed_scripts(iteration_dir)

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["sbatch", "launch/run_slurm.sbatch"],
            returncode=0,
            stdout="submitted\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    with pytest.raises(StageCheckError):
        _execute_launch_runtime(repo, state=state)

    payload = json.loads(
        (iteration_dir / "runs" / "run_001" / "run_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert payload["status"] == "failed"


def test_execute_launch_runtime_duplicate_local_skips_execution(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(iteration_dir, local_script="exit 9\n")

    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "local",
        "launch_mode": "local",
        "status": "completed",
        "command": "bash launch/run_local.sh",
        "resource_request": {"cpus": 1, "memory": "4GB", "gpu_count": 0},
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:01:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    logs_dir = run_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    (logs_dir / "launch.stdout.log").write_text("already ran\n", encoding="utf-8")

    def _should_not_run(*args, **kwargs):
        raise AssertionError("local execution should have been skipped")

    monkeypatch.setattr(subprocess, "run", _should_not_run)

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)
    assert result.run_id == "run_001"
    assert state["sync_status"] == "completed"


def test_execute_launch_runtime_duplicate_slurm_skips_resubmit(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="slurm")
    _seed_scripts(iteration_dir)

    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "slurm",
        "launch_mode": "slurm",
        "status": "submitted",
        "command": "sbatch launch/run_slurm.sbatch",
        "job_id": "99999",
        "slurm": {"job_id": "99999"},
        "resource_request": {
            "cpus": 1,
            "memory": "16GB",
            "gpu_count": 0,
            "job_id": "99999",
        },
        "artifact_sync_to_local": {"status": "pending"},
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    def _should_not_run(*args, **kwargs):
        raise AssertionError("slurm resubmission should have been skipped")

    monkeypatch.setattr(subprocess, "run", _should_not_run)

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)
    assert result.run_id == "run_001"
    assert state["sync_status"] == "pending"
    ledger = repo / "docs" / "slurm_job_list.md"
    assert ledger.exists()
    assert "run_id=run_001" in ledger.read_text(encoding="utf-8")


def test_execute_launch_runtime_multi_run_local_writes_replicates_and_base(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(iteration_dir, local_script="echo replicate\n")

    state = _base_state(iteration_id="iter1", pending_run_id="run_base")
    state["run_group"] = ["run_base_r1", "run_base_r2"]
    result = _execute_launch_runtime(repo, state=state)

    assert result.run_id == "run_base"
    assert state["sync_status"] == "completed"
    for rid in ("run_base_r1", "run_base_r2", "run_base"):
        manifest_path = iteration_dir / "runs" / rid / "run_manifest.json"
        assert manifest_path.exists()
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert payload["host_mode"] == "local"
        assert payload["status"] == "completed"


def test_execute_launch_runtime_adopts_existing_run_when_pending_missing(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(iteration_dir, local_script="exit 9\n")

    existing_run_dir = iteration_dir / "runs" / "run_existing"
    existing_run_dir.mkdir(parents=True, exist_ok=True)
    existing_manifest = {
        "schema_version": "1.0",
        "run_id": "run_existing",
        "iteration_id": "iter1",
        "launch_mode": "local",
        "host_mode": "local",
        "command": "bash launch/run_local.sh",
        "resource_request": {"cpus": 1, "memory": "4GB", "gpu_count": 0},
        "status": "completed",
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:01:00Z",
        },
    }
    (existing_run_dir / "run_manifest.json").write_text(
        json.dumps(existing_manifest, indent=2) + "\n",
        encoding="utf-8",
    )

    def _should_not_run(*args, **kwargs):
        raise AssertionError(
            "execution should have been skipped via existing run adoption"
        )

    monkeypatch.setattr(subprocess, "run", _should_not_run)

    state = _base_state(iteration_id="iter1", pending_run_id="run_pending_new")
    result = _execute_launch_runtime(repo, state=state)
    assert result.run_id == "run_existing"
    assert state["pending_run_id"] == "run_existing"
    assert state["last_run_id"] == "run_existing"


def test_execute_launch_runtime_honors_launch_execute_false(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(iteration_dir)
    _seed_policy(
        repo,
        launch_block={
            "execute": False,
            "local_timeout_seconds": 900,
            "slurm_submit_timeout_seconds": 30,
        },
    )

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)
    assert result.run_id == "run_001"
    assert result.changed_files == ()
