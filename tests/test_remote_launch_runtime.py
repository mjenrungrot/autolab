from __future__ import annotations

import json
from pathlib import Path

import pytest

from autolab.errors import StageCheckError
from autolab.launch_runtime import (
    _execute_launch_runtime,
    _execute_slurm_monitor_runtime,
)
from autolab.remote_profiles import _merge_remote_run_manifest
from autolab.models import (
    RemoteArtifactPullConfig,
    RemoteDataPolicyConfig,
    RemoteEnvConfig,
    RemoteGitSyncConfig,
    RemoteHostDetectionConfig,
    RemoteProfileConfig,
    RevisionLabelInfo,
)


def _write_design(iteration_dir: Path) -> None:
    iteration_dir.mkdir(parents=True, exist_ok=True)
    (iteration_dir / "design.yaml").write_text(
        "compute:\n  location: slurm\n  cpus: 1\n  gpus: 0\n",
        encoding="utf-8",
    )


def _remote_profile(
    mode: str = "git_checkout",
    *,
    submit_command: str = "sbatch",
) -> RemoteProfileConfig:
    return RemoteProfileConfig(
        name="cluster",
        mode=mode,
        enabled_for_host_modes=("slurm",),
        login_host="cluster-login",
        remote_repo_root="/remote/repo",
        bootstrap_command="./scripts/bootstrap_venv.sh",
        python_path="./venv/bin/python",
        submit_command=submit_command,
        host_detection=RemoteHostDetectionConfig(require_commands=("sinfo", "squeue")),
        git_sync=RemoteGitSyncConfig(
            revision_source="git_tag",
            require_clean_worktree=True,
            fetch_command="git fetch --tags origin",
            checkout_command="git checkout --force {revision_label}",
        ),
        artifact_pull=RemoteArtifactPullConfig(
            enabled=True,
            allow_patterns=(
                "experiments/{iteration_id}/runs/{run_id}/metrics.json",
                "experiments/{iteration_id}/runs/{run_id}/run_manifest.json",
            ),
            max_file_size_mb=50.0,
        ),
        data_policy=RemoteDataPolicyConfig(
            local_sync="forbidden",
            deny_patterns=("data/**",),
        ),
        env=RemoteEnvConfig(cache_vars={}),
        smoke_command="",
    )


def test_execute_launch_runtime_remote_git_checkout_records_remote_metadata(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    _write_design(iteration_dir)
    state = {"iteration_id": "iter1", "experiment_id": "", "pending_run_id": "run_001"}

    monkeypatch.setattr(
        "autolab.launch_runtime._detect_priority_host_mode", lambda: "slurm"
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.resolve_remote_profile",
        lambda repo_root, host_mode="", profile_name="": _remote_profile(
            "git_checkout", submit_command="sbatch --qos=high"
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.ensure_remote_profile_launch_ready",
        lambda profile: None,
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.ensure_remote_launch_revision",
        lambda repo_root, profile: RevisionLabelInfo(
            label="v0.4.19", source="git_tag", dirty=False
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.workspace_revision_payload",
        lambda repo_root: {"label": "v0.4.19", "source": "git_tag", "dirty": False},
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.submit_remote_slurm_job",
        lambda profile, **kwargs: (
            "Submitted batch job 456\n",
            "",
            "/remote/repo/experiments/plan/iter1",
            "ssh cluster-login 'cd /remote/repo && sbatch --qos=high launch/run_slurm.sbatch'",
        ),
    )

    result = _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.run_id == "run_001"
    assert payload["status"] == "submitted"
    assert payload["artifact_sync_to_local"]["status"] == "pending"
    assert payload["workspace_revision"]["label"] == "v0.4.19"
    assert payload["remote_execution"]["profile"] == "cluster"
    assert payload["remote_execution"]["mode"] == "git_checkout"
    assert payload["remote_execution"]["code_sync"]["status"] == "ok"
    assert payload["slurm"]["job_id"] == "456"
    assert (
        payload["command"]
        == "ssh cluster-login 'cd /remote/repo && sbatch --qos=high launch/run_slurm.sbatch'"
    )


def test_execute_launch_runtime_remote_profile_readiness_failure(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    _write_design(iteration_dir)
    state = {"iteration_id": "iter1", "experiment_id": "", "pending_run_id": "run_001"}

    monkeypatch.setattr(
        "autolab.launch_runtime._detect_priority_host_mode", lambda: "slurm"
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.resolve_remote_profile",
        lambda repo_root, host_mode="", profile_name="": _remote_profile(
            "git_checkout"
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.ensure_remote_profile_launch_ready",
        lambda profile: (_ for _ in ()).throw(
            StageCheckError("required host commands not available: sinfo")
        ),
    )

    with pytest.raises(StageCheckError, match="required host commands not available"):
        _execute_launch_runtime(repo, state=state)


def test_execute_launch_runtime_remote_submit_failure_keeps_full_command_provenance(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    _write_design(iteration_dir)
    state = {"iteration_id": "iter1", "experiment_id": "", "pending_run_id": "run_001"}

    monkeypatch.setattr(
        "autolab.launch_runtime._detect_priority_host_mode", lambda: "slurm"
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.resolve_remote_profile",
        lambda repo_root, host_mode="", profile_name="": _remote_profile(
            "git_checkout", submit_command="sbatch --qos=high"
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.ensure_remote_profile_launch_ready",
        lambda profile: None,
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.ensure_remote_launch_revision",
        lambda repo_root, profile: RevisionLabelInfo(
            label="v0.4.19", source="git_tag", dirty=False
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.workspace_revision_payload",
        lambda repo_root: {"label": "v0.4.19", "source": "git_tag", "dirty": False},
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.submit_remote_slurm_job",
        lambda profile, **kwargs: (_ for _ in ()).throw(
            StageCheckError("remote submit preflight failed")
        ),
    )

    with pytest.raises(StageCheckError, match="launch execution failed"):
        _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "sbatch --qos=high" in payload["command"]
    assert "--export=ALL,RUN_ID=run_001" in payload["command"]
    assert "cluster-login" in payload["command"]


def test_execute_launch_runtime_verify_only_failure_marks_remote_execution_failed(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    _write_design(iteration_dir)
    state = {"iteration_id": "iter1", "experiment_id": "", "pending_run_id": "run_001"}

    monkeypatch.setattr(
        "autolab.launch_runtime._detect_priority_host_mode", lambda: "slurm"
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.resolve_remote_profile",
        lambda repo_root, host_mode="", profile_name="": _remote_profile("verify_only"),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.ensure_remote_profile_launch_ready",
        lambda profile: None,
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.ensure_remote_launch_revision",
        lambda repo_root, profile: RevisionLabelInfo(
            label="v0.4.19", source="git_tag", dirty=False
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.workspace_revision_payload",
        lambda repo_root: {"label": "v0.4.19", "source": "git_tag", "dirty": False},
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.submit_remote_slurm_job",
        lambda profile, **kwargs: (
            "Submitted batch job 12345",
            "",
            "/remote/repo/experiments/plan/iter1",
            "ssh cluster-login 'sbatch launch/run_slurm.sbatch'",
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.verify_remote_checkout",
        lambda profile, revision_label, timeout_seconds: (_ for _ in ()).throw(
            StageCheckError("remote checkout verification failed")
        ),
    )

    with pytest.raises(StageCheckError, match="launch execution failed"):
        _execute_launch_runtime(repo, state=state)

    manifest_path = iteration_dir / "runs" / "run_001" / "run_manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["remote_execution"]["mode"] == "verify_only"
    assert payload["remote_execution"]["code_sync"]["status"] == "failed"
    assert payload["command"] == "ssh cluster-login 'sbatch launch/run_slurm.sbatch'"


def test_slurm_monitor_remote_artifact_pull_updates_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": "run_001",
                "iteration_id": "iter1",
                "launch_mode": "slurm",
                "host_mode": "slurm",
                "status": "submitted",
                "slurm": {"job_id": "12345"},
                "artifact_sync_to_local": {"status": "pending"},
                "remote_execution": {
                    "profile": "cluster",
                    "mode": "git_checkout",
                    "remote_repo_root": "/remote/repo",
                    "code_sync": {
                        "requested_revision_label": "v0.4.19",
                        "resolved_remote_revision_label": "v0.4.19",
                        "status": "ok",
                    },
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    state = {
        "iteration_id": "iter1",
        "pending_run_id": "run_001",
        "sync_status": "pending",
    }

    monkeypatch.setattr(
        "autolab.remote_profiles.resolve_remote_profile",
        lambda repo_root, host_mode="", profile_name="": _remote_profile(
            "git_checkout"
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.poll_remote_job",
        lambda profile, job_id, timeout_seconds: ("COMPLETED\n", ""),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.pull_remote_artifacts",
        lambda profile, repo_root, iteration_id, run_id, timeout_seconds: {
            "status": "completed",
            "pulled_paths": [
                "experiments/plan/iter1/runs/run_001/metrics.json",
                "experiments/plan/iter1/runs/run_001/run_manifest.json",
            ],
            "failures": [],
        },
    )

    result = _execute_slurm_monitor_runtime(repo, state=state)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.status == "synced"
    assert payload["status"] == "synced"
    assert payload["artifact_sync_to_local"]["status"] == "completed"
    assert payload["artifact_sync_to_local"]["pulled_paths"]


def test_slurm_monitor_remote_artifact_pull_clears_stale_failures(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    iteration_dir = repo / "experiments" / "plan" / "iter1"
    run_dir = iteration_dir / "runs" / "run_001"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": "run_001",
                "iteration_id": "iter1",
                "launch_mode": "slurm",
                "host_mode": "slurm",
                "status": "submitted",
                "slurm": {"job_id": "12345"},
                "artifact_sync_to_local": {
                    "status": "failed",
                    "failures": [{"path": "data/blob.bin", "reason": "forbidden"}],
                    "pulled_paths": ["stale/path.txt"],
                },
                "remote_execution": {
                    "profile": "cluster",
                    "mode": "git_checkout",
                    "remote_repo_root": "/remote/repo",
                    "code_sync": {
                        "requested_revision_label": "v0.4.19",
                        "resolved_remote_revision_label": "v0.4.19",
                        "status": "ok",
                    },
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    state = {
        "iteration_id": "iter1",
        "pending_run_id": "run_001",
        "sync_status": "failed",
    }

    monkeypatch.setattr(
        "autolab.remote_profiles.resolve_remote_profile",
        lambda repo_root, host_mode="", profile_name="": _remote_profile(
            "git_checkout"
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.poll_remote_job",
        lambda profile, job_id, timeout_seconds: ("COMPLETED\n", ""),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.pull_remote_artifacts",
        lambda profile, repo_root, iteration_id, run_id, timeout_seconds: {
            "status": "completed",
            "pulled_paths": [
                "experiments/plan/iter1/runs/run_001/metrics.json",
            ],
            "failures": [],
        },
    )

    result = _execute_slurm_monitor_runtime(repo, state=state)

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert result.status == "synced"
    assert payload["artifact_sync_to_local"]["status"] == "completed"
    assert payload["artifact_sync_to_local"]["pulled_paths"] == [
        "experiments/plan/iter1/runs/run_001/metrics.json"
    ]
    assert "failures" not in payload["artifact_sync_to_local"]


def test_merge_remote_run_manifest_drops_stale_local_sync_metadata(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "run_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "run_id": "run_001",
                "status": "submitted",
                "artifact_sync_to_local": {
                    "status": "failed",
                    "failures": [{"path": "bad.bin", "reason": "stale"}],
                },
                "remote_execution": {"profile": "cluster"},
                "workspace_revision": {"label": "v0.4.19"},
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    _merge_remote_run_manifest(
        manifest_path,
        json.dumps(
            {
                "run_id": "run_001",
                "status": "completed",
            }
        ).encode("utf-8"),
    )

    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert payload["status"] == "completed"
    assert "artifact_sync_to_local" not in payload
    assert payload["remote_execution"] == {"profile": "cluster"}
    assert payload["workspace_revision"] == {"label": "v0.4.19"}
