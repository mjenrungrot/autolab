from __future__ import annotations

from pathlib import Path

import pytest

from autolab.errors import StageCheckError
from autolab.remote_profiles import (
    lint_remote_profile,
    load_remote_profiles,
    normalize_profile_mode,
    pull_remote_artifacts,
    resolve_remote_profile,
)


def test_normalize_profile_mode_maps_standalone() -> None:
    assert normalize_profile_mode("standalone") == "git_checkout"
    assert normalize_profile_mode("shared_fs") == "shared_fs"
    assert normalize_profile_mode("verify_only") == "verify_only"


def test_load_remote_profiles_reads_profile(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = repo / ".autolab" / "remote_profiles.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        (
            'schema_version: "1.0"\n'
            'default_profile: "cluster"\n'
            "profiles:\n"
            "  cluster:\n"
            "    mode: standalone\n"
            '    enabled_for_host_modes: ["slurm"]\n'
            '    login_host: "cluster-login"\n'
            '    remote_repo_root: "/remote/repo"\n'
            '    bootstrap_command: "./scripts/bootstrap.sh"\n'
            '    python_path: "./venv/bin/python"\n'
            '    submit_command: "sbatch"\n'
            "    host_detection:\n"
            '      require_commands: ["sinfo", "squeue"]\n'
            "    git_sync:\n"
            "      revision_source: git_tag\n"
            "      require_clean_worktree: true\n"
            '      fetch_command: "git fetch --tags origin"\n'
            '      checkout_command: "git checkout --force {revision_label}"\n'
            "    artifact_pull:\n"
            "      enabled: true\n"
            "      allow_patterns:\n"
            '        - "experiments/{iteration_id}/runs/{run_id}/metrics.json"\n'
            "      max_file_size_mb: 50\n"
            "    data_policy:\n"
            "      local_sync: forbidden\n"
            "      deny_patterns:\n"
            '        - "data/**"\n'
            "    env:\n"
            "      cache_vars:\n"
            '        HF_HOME: "${SLURM_TMPDIR:-{remote_repo_root}}/.cache/hf"\n'
            '    smoke_command: "./venv/bin/python -V"\n'
        ),
        encoding="utf-8",
    )

    config = load_remote_profiles(repo)
    profile = config.profiles["cluster"]
    assert config.default_profile == "cluster"
    assert profile.mode == "git_checkout"
    assert profile.host_detection.require_commands == ("sinfo", "squeue")
    assert profile.artifact_pull.allow_patterns == (
        "experiments/{iteration_id}/runs/{run_id}/metrics.json",
    )
    assert profile.data_policy.deny_patterns == ("data/**",)


def test_lint_remote_profile_flags_denied_allow_pattern(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = repo / ".autolab" / "remote_profiles.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        (
            'default_profile: "cluster"\n'
            "profiles:\n"
            "  cluster:\n"
            "    mode: git_checkout\n"
            '    enabled_for_host_modes: ["slurm"]\n'
            '    login_host: "cluster-login"\n'
            '    remote_repo_root: "/remote/repo"\n'
            '    python_path: "./venv/bin/python"\n'
            '    submit_command: "sbatch"\n'
            "    artifact_pull:\n"
            "      enabled: true\n"
            "      allow_patterns:\n"
            '        - "data/{iteration_id}/{run_id}/metrics.json"\n'
            "      max_file_size_mb: 50\n"
            "    data_policy:\n"
            "      local_sync: forbidden\n"
            "      deny_patterns:\n"
            '        - "data/**"\n'
        ),
        encoding="utf-8",
    )

    profile = load_remote_profiles(repo).profiles["cluster"]
    issues = lint_remote_profile(profile)
    assert issues
    assert "unsafe" in issues[0]


def test_normalize_profile_mode_rejects_unknown_value() -> None:
    with pytest.raises(StageCheckError, match="Unknown remote profile mode"):
        normalize_profile_mode("git-checkout")


def test_resolve_remote_profile_rejects_unknown_mode(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = repo / ".autolab" / "remote_profiles.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        (
            'schema_version: "1.0"\n'
            'default_profile: "cluster"\n'
            "profiles:\n"
            "  cluster:\n"
            "    mode: git-checkout\n"
            '    enabled_for_host_modes: ["slurm"]\n'
            '    login_host: "cluster-login"\n'
            '    remote_repo_root: "/remote/repo"\n'
            '    bootstrap_command: "./scripts/bootstrap.sh"\n'
            '    python_path: "./venv/bin/python"\n'
            '    submit_command: "sbatch"\n'
        ),
        encoding="utf-8",
    )

    with pytest.raises(StageCheckError, match="Unknown remote profile mode"):
        resolve_remote_profile(repo, host_mode="slurm")


def test_pull_remote_artifacts_requires_allowlist_when_local_sync_forbidden(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    config_path = repo / ".autolab" / "remote_profiles.yaml"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        (
            'schema_version: "1.0"\n'
            'default_profile: "cluster"\n'
            "profiles:\n"
            "  cluster:\n"
            "    mode: git_checkout\n"
            '    enabled_for_host_modes: ["slurm"]\n'
            '    login_host: "cluster-login"\n'
            '    remote_repo_root: "/remote/repo"\n'
            '    bootstrap_command: "./scripts/bootstrap.sh"\n'
            '    python_path: "./venv/bin/python"\n'
            '    submit_command: "sbatch --qos=high"\n'
            "    artifact_pull:\n"
            "      enabled: true\n"
            "      allow_patterns: []\n"
            "      max_file_size_mb: 50\n"
            "    data_policy:\n"
            "      local_sync: forbidden\n"
        ),
        encoding="utf-8",
    )

    profile = load_remote_profiles(repo).profiles["cluster"]
    result = pull_remote_artifacts(
        profile,
        repo_root=repo,
        iteration_id="iter1",
        run_id="run_001",
        timeout_seconds=5.0,
    )

    assert result["status"] == "failed"
    assert result["failures"] == [
        {
            "path": "",
            "reason": "artifact pull allow_patterns must be configured when data_policy.local_sync is forbidden",
        }
    ]
