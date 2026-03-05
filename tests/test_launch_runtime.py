from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from autolab.launch_runtime import (
    _ensure_launch_scripts,
    _execute_launch_runtime,
    _execute_slurm_monitor_runtime,
    _fits_current_allocation,
    _parse_memory_to_mb,
    _parse_walltime_to_seconds,
    _stderr_has_fatal_markers,
)
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
        "implementation_requirements": [
            {
                "requirement_id": "R1",
                "description": "Implement launch runtime path",
                "scope_kind": "experiment",
                "expected_artifacts": ["implementation_plan.md"],
            }
        ],
    }
    (iteration_dir / "design.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _seed_scripts(
    iteration_dir: Path,
    *,
    local_script: str = "echo local\n",
    slurm_script: str = "echo slurm\n",
) -> None:
    launch_dir = iteration_dir / "launch"
    launch_dir.mkdir(parents=True, exist_ok=True)
    (launch_dir / "run_local.sh").write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n" + local_script,
        encoding="utf-8",
    )
    (launch_dir / "run_slurm.sbatch").write_text(
        "#!/usr/bin/env bash\n#SBATCH --job-name=test\n" + slurm_script,
        encoding="utf-8",
    )


def _seed_policy(repo: Path, *, launch_block: dict[str, object]) -> None:
    _write_policy(repo, {"launch": launch_block})


def _write_policy(repo: Path, payload: dict[str, object]) -> None:
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        yaml.safe_dump(payload, sort_keys=False),
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


def _seed_slurm_manifest(
    iteration_dir: Path,
    *,
    run_id: str = "run_001",
    status: str = "submitted",
    sync_status: str = "pending",
    job_id: str | None = "12345",
) -> Path:
    run_dir = iteration_dir / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, object] = {
        "schema_version": "1.0",
        "run_id": run_id,
        "iteration_id": iteration_dir.name,
        "host_mode": "slurm",
        "launch_mode": "slurm",
        "status": status,
        "command": "sbatch launch/run_slurm.sbatch",
        "resource_request": {"cpus": 1, "memory": "8GB", "gpu_count": 0},
        "artifact_sync_to_local": {"status": sync_status},
        "timestamps": {"started_at": "2026-01-01T00:00:00Z"},
    }
    if job_id is not None:
        manifest["job_id"] = job_id
        manifest["slurm"] = {"job_id": job_id}
        manifest["resource_request"] = {
            "cpus": 1,
            "memory": "8GB",
            "gpu_count": 0,
            "job_id": job_id,
        }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n",
        encoding="utf-8",
    )
    return run_dir


def _slurm_monitor_state(
    *, iteration_id: str, run_id: str = "run_001"
) -> dict[str, object]:
    return {
        "iteration_id": iteration_id,
        "experiment_id": "e1",
        "pending_run_id": run_id,
        "last_run_id": run_id,
        "sync_status": "pending",
    }


def test_execute_launch_runtime_local_success(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    # Script must produce a real artifact in runs/<run_id>/ for status=completed
    _seed_scripts(
        iteration_dir,
        local_script='mkdir -p "runs/$AUTOLAB_RUN_ID"\necho \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\necho hello-local\n',
    )

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


def test_execute_launch_runtime_local_exit0_no_artifacts_marks_partial(
    tmp_path: Path,
) -> None:
    """Script exits 0 but produces no output files -> status=partial."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(iteration_dir, local_script='echo "starting" && exit 0\n')

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    with pytest.raises(StageCheckError, match="partial"):
        _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "partial"
    assert payload["artifact_sync_to_local"]["status"] == "failed"


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
    _seed_scripts(
        iteration_dir,
        local_script='mkdir -p "runs/$AUTOLAB_RUN_ID"\necho \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\necho replicate\n',
    )

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


# ---------------------------------------------------------------------------
# Launch script generation policy
# ---------------------------------------------------------------------------


def test_launch_script_generation_missing_only_preserves_existing_scripts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(
        iteration_dir,
        local_script='echo "custom-missing-only-local"\n',
        slurm_script='echo "custom-missing-only-slurm"\n',
    )

    local_script = iteration_dir / "launch" / "run_local.sh"
    slurm_script = iteration_dir / "launch" / "run_slurm.sbatch"
    local_before = local_script.read_text(encoding="utf-8")
    slurm_before = slurm_script.read_text(encoding="utf-8")
    design_payload = yaml.safe_load(
        (iteration_dir / "design.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(design_payload, dict)

    changed_files: list[Path] = []
    _ensure_launch_scripts(
        repo_root=repo,
        iteration_dir=iteration_dir,
        iteration_id="iter1",
        design_payload=design_payload,
        script_generation_mode="missing_only",
        changed_files=changed_files,
    )

    assert changed_files == []
    assert local_script.read_text(encoding="utf-8") == local_before
    assert slurm_script.read_text(encoding="utf-8") == slurm_before


def test_launch_script_generation_always_rewrites_existing_scripts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(
        iteration_dir,
        local_script='echo "custom-always-local"\n',
        slurm_script='echo "custom-always-slurm"\n',
    )

    local_script = iteration_dir / "launch" / "run_local.sh"
    slurm_script = iteration_dir / "launch" / "run_slurm.sbatch"
    design_payload = yaml.safe_load(
        (iteration_dir / "design.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(design_payload, dict)

    changed_files: list[Path] = []
    _ensure_launch_scripts(
        repo_root=repo,
        iteration_dir=iteration_dir,
        iteration_id="iter1",
        design_payload=design_payload,
        script_generation_mode="always",
        changed_files=changed_files,
    )

    local_after = local_script.read_text(encoding="utf-8")
    slurm_after = slurm_script.read_text(encoding="utf-8")
    assert "custom-always-local" not in local_after
    assert "custom-always-slurm" not in slurm_after
    assert 'RUN_ID="${AUTOLAB_RUN_ID:-${RUN_ID:-}}"' in local_after
    assert "#SBATCH --job-name=autolab-iter1" in slurm_after
    assert set(changed_files) == {local_script, slurm_script}


# ---------------------------------------------------------------------------
# SLURM monitor command-template runtime
# ---------------------------------------------------------------------------


def test_slurm_monitor_poll_template_requires_job_id(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_slurm_manifest(iteration_dir, job_id=None)
    _write_policy(
        repo,
        {
            "slurm": {
                "monitor": {
                    "poll_command_template": "echo running",
                    "poll_timeout_seconds": 10,
                }
            }
        },
    )

    state = _slurm_monitor_state(iteration_id="iter1")
    with pytest.raises(StageCheckError, match="lacks job_id"):
        _execute_slurm_monitor_runtime(repo, state=state)


def test_slurm_monitor_poll_running_transitions_manifest_status(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    run_dir = _seed_slurm_manifest(
        iteration_dir, status="submitted", sync_status="pending"
    )
    _write_policy(
        repo,
        {
            "slurm": {
                "monitor": {
                    "poll_command_template": "echo R",
                    "poll_timeout_seconds": 10,
                }
            }
        },
    )

    calls: list[object] = []

    def _fake_run(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("args"))
        return subprocess.CompletedProcess(
            args=args[0] if args else kwargs.get("args"),
            returncode=0,
            stdout="R\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    state = _slurm_monitor_state(iteration_id="iter1")
    result = _execute_slurm_monitor_runtime(repo, state=state)

    assert calls, "poll command was not executed"
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "running"
    assert manifest["artifact_sync_to_local"]["status"] == "pending"
    assert state["sync_status"] == "pending"
    assert result.status == "running"
    assert result.sync_status == "pending"
    assert any(
        path.name == "slurm_monitor.poll.stdout.log" for path in result.changed_files
    )
    assert any(
        path.name == "slurm_monitor.poll.stderr.log" for path in result.changed_files
    )


def test_slurm_monitor_poll_then_sync_success_marks_synced(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    run_dir = _seed_slurm_manifest(
        iteration_dir, status="submitted", sync_status="pending"
    )
    _write_policy(
        repo,
        {
            "slurm": {
                "monitor": {
                    "poll_command_template": "echo completed-{job_id}",
                    "poll_timeout_seconds": 10,
                    "sync_command_template": "echo sync-{run_id}",
                    "sync_timeout_seconds": 30,
                }
            }
        },
    )

    call_args: list[object] = []

    def _fake_run(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        call_args.append(command)
        if len(call_args) == 1:
            return subprocess.CompletedProcess(
                args=command,
                returncode=0,
                stdout="COMPLETED\n",
                stderr="",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="synced\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    state = _slurm_monitor_state(iteration_id="iter1")
    result = _execute_slurm_monitor_runtime(repo, state=state)

    assert len(call_args) == 2
    assert "12345" in str(call_args[0])
    assert "run_001" in str(call_args[1])
    manifest = json.loads((run_dir / "run_manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "synced"
    assert manifest["artifact_sync_to_local"]["status"] == "completed"
    assert state["sync_status"] == "completed"
    assert result.status == "synced"
    assert result.sync_status == "completed"
    changed_names = {path.name for path in result.changed_files}
    assert "slurm_monitor.poll.stdout.log" in changed_names
    assert "slurm_monitor.sync.stdout.log" in changed_names


def test_slurm_monitor_poll_command_failure_raises_stage_error(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_slurm_manifest(iteration_dir)
    _write_policy(
        repo,
        {
            "slurm": {
                "monitor": {
                    "poll_command_template": "echo fail",
                    "poll_timeout_seconds": 10,
                }
            }
        },
    )

    def _fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0] if args else kwargs.get("args"),
            returncode=3,
            stdout="",
            stderr="poll failed\n",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    state = _slurm_monitor_state(iteration_id="iter1")
    with pytest.raises(StageCheckError, match="poll command failed"):
        _execute_slurm_monitor_runtime(repo, state=state)


def test_slurm_monitor_poll_timeout_raises_stage_error(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_slurm_manifest(iteration_dir)
    _write_policy(
        repo,
        {
            "slurm": {
                "monitor": {
                    "poll_command_template": "echo timeout",
                    "poll_timeout_seconds": 10,
                }
            }
        },
    )

    def _fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="echo timeout", timeout=10)

    monkeypatch.setattr(subprocess, "run", _fake_run)
    state = _slurm_monitor_state(iteration_id="iter1")
    with pytest.raises(StageCheckError, match="poll command timed out"):
        _execute_slurm_monitor_runtime(repo, state=state)


def test_slurm_monitor_sync_command_failure_raises_stage_error(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_slurm_manifest(iteration_dir, status="completed", sync_status="pending")
    _write_policy(
        repo,
        {
            "slurm": {
                "monitor": {
                    "poll_command_template": "echo completed",
                    "poll_timeout_seconds": 10,
                    "sync_command_template": "echo sync",
                    "sync_timeout_seconds": 10,
                }
            }
        },
    )

    def _fake_run(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if "sync" in str(command):
            return subprocess.CompletedProcess(
                args=command,
                returncode=8,
                stdout="",
                stderr="sync failed\n",
            )
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="COMPLETED\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    state = _slurm_monitor_state(iteration_id="iter1")
    with pytest.raises(StageCheckError, match="sync command failed"):
        _execute_slurm_monitor_runtime(repo, state=state)


def test_slurm_monitor_sync_timeout_raises_stage_error(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_slurm_manifest(iteration_dir, status="completed", sync_status="pending")
    _write_policy(
        repo,
        {
            "slurm": {
                "monitor": {
                    "poll_command_template": "echo completed",
                    "poll_timeout_seconds": 10,
                    "sync_command_template": "echo sync",
                    "sync_timeout_seconds": 10,
                }
            }
        },
    )

    def _fake_run(*args, **kwargs):
        command = args[0] if args else kwargs.get("args")
        if "sync" in str(command):
            raise subprocess.TimeoutExpired(cmd="echo sync", timeout=10)
        return subprocess.CompletedProcess(
            args=command,
            returncode=0,
            stdout="COMPLETED\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_run)
    state = _slurm_monitor_state(iteration_id="iter1")
    with pytest.raises(StageCheckError, match="sync command timed out"):
        _execute_slurm_monitor_runtime(repo, state=state)


# ---------------------------------------------------------------------------
# Parse helpers
# ---------------------------------------------------------------------------


class TestParseMemoryToMb:
    def test_gigabytes(self) -> None:
        assert _parse_memory_to_mb("4GB") == 4096

    def test_megabytes(self) -> None:
        assert _parse_memory_to_mb("16384MB") == 16384

    def test_terabytes(self) -> None:
        assert _parse_memory_to_mb("2TB") == 2 * 1_048_576

    def test_bare_number_treated_as_mb(self) -> None:
        assert _parse_memory_to_mb("8192") == 8192

    def test_empty_string(self) -> None:
        assert _parse_memory_to_mb("") is None

    def test_unparseable(self) -> None:
        assert _parse_memory_to_mb("lots") is None


class TestParseWalltimeToSeconds:
    def test_hh_mm_ss(self) -> None:
        assert _parse_walltime_to_seconds("01:30:00") == 5400

    def test_mm_ss(self) -> None:
        assert _parse_walltime_to_seconds("30:00") == 1800

    def test_days_hh_mm_ss(self) -> None:
        assert _parse_walltime_to_seconds("1-12:00:00") == 129600

    def test_empty(self) -> None:
        assert _parse_walltime_to_seconds("") is None


# ---------------------------------------------------------------------------
# Resource fitness
# ---------------------------------------------------------------------------


class TestFitsCurrentAllocation:
    def test_all_fit(self) -> None:
        design = {
            "compute": {
                "cpus": 2,
                "memory_estimate": "4GB",
                "gpu_count": 1,
                "walltime_estimate": "00:30:00",
            }
        }
        allocation = {
            "cpus": 4,
            "memory_mb": 8192,
            "gpu_count": 2,
            "remaining_seconds": 3600,
        }
        assert _fits_current_allocation(design, allocation) is True

    def test_exceeds_gpu(self) -> None:
        design = {"compute": {"cpus": 1, "gpu_count": 4}}
        allocation = {"cpus": 8, "gpu_count": 2}
        assert _fits_current_allocation(design, allocation) is False

    def test_exceeds_walltime(self) -> None:
        design = {"compute": {"walltime_estimate": "02:00:00"}}
        allocation = {"remaining_seconds": 3600}  # 1h remaining, need 2h
        assert _fits_current_allocation(design, allocation) is False

    def test_missing_fields_treated_as_fits(self) -> None:
        design = {
            "compute": {
                "cpus": 2,
                "memory_estimate": "4GB",
                "gpu_count": 1,
                "walltime_estimate": "00:30:00",
            }
        }
        allocation = {}  # no resource info at all
        assert _fits_current_allocation(design, allocation) is True


# ---------------------------------------------------------------------------
# Interactive SLURM execution
# ---------------------------------------------------------------------------


def _seed_design_with_compute(
    iteration_dir: Path,
    *,
    mode: str,
    cpus: int = 1,
    gpus: int = 0,
    memory: str = "",
    walltime: str = "",
) -> None:
    compute: dict[str, object] = {"location": mode, "cpus": cpus, "gpus": gpus}
    if memory:
        compute["memory_estimate"] = memory
    if walltime:
        compute["walltime_estimate"] = walltime
    payload = {
        "schema_version": "1.0",
        "id": "e1",
        "iteration_id": iteration_dir.name,
        "hypothesis_id": "h1",
        "entrypoint": {"module": "pkg.train", "args": {}},
        "compute": compute,
        "metrics": {"primary": {"name": "accuracy", "mode": "maximize"}},
        "baselines": [{"name": "baseline", "value": 0.0}],
        "implementation_requirements": [
            {
                "requirement_id": "R1",
                "description": "Implement launch runtime path",
                "scope_kind": "experiment",
                "expected_artifacts": ["implementation_plan.md"],
            }
        ],
    }
    (iteration_dir / "design.yaml").write_text(
        yaml.safe_dump(payload, sort_keys=False),
        encoding="utf-8",
    )


def _mock_interactive_slurm(
    monkeypatch,
    *,
    cpus: str = "8",
    mem: str = "16384",
    gpus: str = "2",
    job_id: str = "55555",
) -> None:
    """Set env vars to simulate an interactive SLURM allocation."""
    import sys

    monkeypatch.setenv("SLURM_JOB_ID", job_id)
    monkeypatch.setenv("SLURM_CPUS_ON_NODE", cpus)
    monkeypatch.setenv("SLURM_MEM_PER_NODE", mem)
    monkeypatch.setenv("SLURM_GPUS", gpus)
    monkeypatch.setenv("SLURM_CLUSTER_NAME", "test-cluster")
    # Ensure isatty returns True so interactive detection works
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)


def test_slurm_interactive_runs_directly(tmp_path: Path, monkeypatch) -> None:
    """On interactive node with fitting resources -> direct execution."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design_with_compute(iteration_dir, mode="slurm", cpus=2, gpus=1, memory="4GB")
    _seed_scripts(
        iteration_dir,
        slurm_script='mkdir -p "runs/$AUTOLAB_RUN_ID"\necho \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\necho slurm\n',
    )

    _mock_interactive_slurm(monkeypatch, cpus="8", mem="16384", gpus="2")
    # Patch squeue call for remaining time
    original_run = subprocess.run

    def _patched_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "squeue" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="2:00:00\n", stderr=""
            )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _patched_run)

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)

    assert result.run_id == "run_001"
    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["host_mode"] == "slurm"
    assert payload["artifact_sync_to_local"]["status"] == "ok"
    assert "slurm_environment" in payload


def test_slurm_interactive_exceeds_resources_sbatches(
    tmp_path: Path, monkeypatch
) -> None:
    """On interactive node but requirements exceed -> sbatch submission."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design_with_compute(
        iteration_dir, mode="slurm", cpus=2, gpus=8, memory="64GB"
    )
    _seed_scripts(iteration_dir)

    _mock_interactive_slurm(monkeypatch, cpus="4", mem="8192", gpus="2")
    original_run = subprocess.run

    def _patched_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "squeue" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="1:00:00\n", stderr=""
            )
        if isinstance(cmd, list) and "sbatch" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Submitted batch job 77777\n", stderr=""
            )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _patched_run)

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "submitted"
    assert payload["job_id"] == "77777"


def test_slurm_non_interactive_still_batches(tmp_path: Path, monkeypatch) -> None:
    """Not on interactive node -> normal sbatch (regression guard)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design_with_compute(iteration_dir, mode="slurm", cpus=1, gpus=0)
    _seed_scripts(iteration_dir)

    # No SLURM_JOB_ID means not interactive
    monkeypatch.delenv("SLURM_JOB_ID", raising=False)
    monkeypatch.delenv("SLURM_CPUS_ON_NODE", raising=False)
    monkeypatch.delenv("SLURM_MEM_PER_NODE", raising=False)
    monkeypatch.delenv("SLURM_GPUS", raising=False)

    def _fake_sbatch(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=["sbatch"],
            returncode=0,
            stdout="Submitted batch job 88888\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", _fake_sbatch)

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "submitted"
    assert payload["job_id"] == "88888"
    assert "slurm_environment" not in payload


def test_slurm_interactive_captures_metadata(tmp_path: Path, monkeypatch) -> None:
    """Verify slurm_environment field is populated in manifest."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design_with_compute(iteration_dir, mode="slurm", cpus=1, gpus=0)
    _seed_scripts(
        iteration_dir,
        slurm_script='mkdir -p "runs/$AUTOLAB_RUN_ID"\necho \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\necho slurm\n',
    )

    _mock_interactive_slurm(monkeypatch, cpus="4", mem="8192", gpus="0", job_id="12300")
    original_run = subprocess.run

    def _patched_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args", [])
        if isinstance(cmd, list) and "squeue" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="3:00:00\n", stderr=""
            )
        return original_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", _patched_run)

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    slurm_env = payload.get("slurm_environment", {})
    assert slurm_env.get("SLURM_JOB_ID") == "12300"
    assert slurm_env.get("SLURM_CLUSTER_NAME") == "test-cluster"


def test_slurm_interactive_monitor_advances(tmp_path: Path) -> None:
    """Verify _eval_slurm_monitor advances for host_mode=slurm + status=completed."""
    from autolab.evaluate import _eval_slurm_monitor

    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)

    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": "1.0",
        "run_id": "run_001",
        "iteration_id": "iter1",
        "host_mode": "slurm",
        "launch_mode": "slurm",
        "status": "completed",
        "command": "bash launch/run_slurm.sbatch",
        "job_id": "55555",
        "resource_request": {"cpus": 1, "memory": "4GB", "gpu_count": 0},
        "artifact_sync_to_local": {"status": "ok"},
        "timestamps": {
            "started_at": "2026-01-01T00:00:00Z",
            "completed_at": "2026-01-01T00:05:00Z",
        },
    }
    (run_dir / "run_manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )

    # Write strict lifecycle policy
    policy_path = repo / ".autolab" / "verifier_policy.yaml"
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        yaml.safe_dump({"slurm_lifecycle_strict": True}, sort_keys=False),
        encoding="utf-8",
    )

    state = {"pending_run_id": "run_001", "last_run_id": "run_001", "sync_status": ""}
    result = _eval_slurm_monitor(repo, state, iteration_dir, "iter1")
    assert result.next_stage == "extract_results"
    assert "completed" in result.summary


# ---------------------------------------------------------------------------
# RUN_ID env var, stderr fatal markers, and run-id drift
# ---------------------------------------------------------------------------


def test_run_id_env_var_is_set(tmp_path: Path) -> None:
    """Script echoes $RUN_ID — verify it matches and manifest is completed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(
        iteration_dir,
        local_script=(
            'echo "RUN_ID=$RUN_ID"\n'
            'mkdir -p "runs/$RUN_ID"\n'
            'echo \'{"acc":0.9}\' > "runs/$RUN_ID/output.json"\n'
        ),
    )

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)

    assert result.run_id == "run_001"
    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    stdout_log = (
        iteration_dir / "runs" / "run_001" / "logs" / "launch.stdout.log"
    ).read_text(encoding="utf-8")
    assert "RUN_ID=run_001" in stdout_log


def test_stderr_fatal_marker_forces_failed_despite_artifacts(
    tmp_path: Path,
) -> None:
    """Script produces artifacts but writes RuntimeError to stderr -> failed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(
        iteration_dir,
        local_script=(
            'mkdir -p "runs/$AUTOLAB_RUN_ID"\n'
            'echo \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\n'
            'echo "RuntimeError: Failed to open temp writer" >&2\n'
        ),
    )

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    with pytest.raises(StageCheckError, match="failed"):
        _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["artifact_sync_to_local"]["status"] == "failed"


def test_stderr_without_fatal_markers_allows_completed(tmp_path: Path) -> None:
    """Script produces artifacts with benign stderr warnings -> completed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    _seed_scripts(
        iteration_dir,
        local_script=(
            'mkdir -p "runs/$AUTOLAB_RUN_ID"\n'
            'echo \'{"acc":0.9}\' > "runs/$AUTOLAB_RUN_ID/output.json"\n'
            'echo "UserWarning: some benign deprecation notice" >&2\n'
        ),
    )

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    result = _execute_launch_runtime(repo, state=state)

    assert result.run_id == "run_001"
    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert payload["artifact_sync_to_local"]["status"] == "ok"


class TestStderrFatalMarkerDetection:
    """Unit tests for _stderr_has_fatal_markers."""

    def test_runtime_error(self) -> None:
        assert _stderr_has_fatal_markers("RuntimeError: boom") != ""

    def test_traceback(self) -> None:
        assert _stderr_has_fatal_markers("Traceback (most recent call last)") != ""

    def test_cuda_error(self) -> None:
        assert _stderr_has_fatal_markers("CUDA error: device-side assert") != ""

    def test_out_of_memory(self) -> None:
        assert _stderr_has_fatal_markers("OutOfMemoryError") != ""

    def test_segfault(self) -> None:
        assert _stderr_has_fatal_markers("Segmentation fault (core dumped)") != ""

    def test_killed(self) -> None:
        assert _stderr_has_fatal_markers("process killed by signal") != ""

    def test_fatal_word(self) -> None:
        assert _stderr_has_fatal_markers("FATAL: cannot allocate memory") != ""

    def test_benign_warning(self) -> None:
        assert _stderr_has_fatal_markers("UserWarning: deprecated API") == ""

    def test_empty_string(self) -> None:
        assert _stderr_has_fatal_markers("") == ""

    def test_normal_output(self) -> None:
        assert _stderr_has_fatal_markers("Training epoch 1/10 loss=0.42") == ""


def test_run_id_drift_marks_failed(tmp_path: Path) -> None:
    """Script writes artifacts under wrong directory name -> failed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    iteration_dir.mkdir(parents=True, exist_ok=True)
    _seed_design(iteration_dir, mode="local")
    # Script creates artifacts under a *wrong* run-id directory
    _seed_scripts(
        iteration_dir,
        local_script=(
            'mkdir -p "runs/wrong_run_id"\n'
            'echo \'{"acc":0.9}\' > "runs/wrong_run_id/output.json"\n'
        ),
    )

    state = _base_state(iteration_id="iter1", pending_run_id="run_001")
    with pytest.raises(StageCheckError, match="failed"):
        _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["artifact_sync_to_local"]["status"] == "failed"
