from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from types import SimpleNamespace

from autolab.cli.handlers_admin import _cmd_remote_show, _cmd_remote_smoke
from autolab.cli.parser import _build_parser


def test_remote_show_parser_accepts_state_file() -> None:
    parser = _build_parser()

    args = parser.parse_args(
        ["remote", "show", "--state-file", "custom-state.json", "--profile", "cluster"]
    )

    assert args.state_file == "custom-state.json"
    assert args.profile == "cluster"
    assert getattr(args.handler, "__name__", "") == _cmd_remote_show.__name__


def test_remote_show_uses_repo_root_and_host_mode(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._resolve_repo_root", lambda path: repo
    )
    monkeypatch.setattr(
        "autolab.utils._detect_host_mode_with_probe",
        lambda: ("slurm", {"sinfo": "ok", "squeue": "ok"}),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.load_remote_profiles",
        lambda repo_root: SimpleNamespace(
            path=repo / ".autolab" / "remote_profiles.yaml"
        ),
    )

    def _resolve_remote_profile(repo_root, host_mode="", profile_name=""):
        calls["repo_root"] = repo_root
        calls["host_mode"] = host_mode
        calls["profile_name"] = profile_name
        return SimpleNamespace(
            name="cluster",
            mode="shared_fs",
            enabled_for_host_modes=("slurm",),
            login_host="cluster-login",
            remote_repo_root="/remote/repo",
            python_path="./venv/bin/python",
            bootstrap_command="./scripts/bootstrap.sh",
            submit_command="sbatch",
            artifact_pull=SimpleNamespace(enabled=True, max_file_size_mb=50.0),
            data_policy=SimpleNamespace(deny_patterns=("data/**",)),
            smoke_command="",
        )

    monkeypatch.setattr(
        "autolab.remote_profiles.resolve_remote_profile", _resolve_remote_profile
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.resolve_workspace_revision",
        lambda repo_root: SimpleNamespace(label="", source="git_tag", dirty=False),
    )

    exit_code = _cmd_remote_show(
        argparse.Namespace(profile="", state_file=str(state_path))
    )

    out = capsys.readouterr().out
    assert exit_code == 0
    assert calls["repo_root"] == repo
    assert calls["host_mode"] == "slurm"
    assert "- host_mode: slurm" in out


def test_remote_smoke_uses_host_mode_for_profile_resolution(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    state_path = repo / ".autolab" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text("{}", encoding="utf-8")

    calls: dict[str, object] = {}

    monkeypatch.setattr(
        "autolab.cli.handlers_admin._resolve_repo_root", lambda path: repo
    )
    monkeypatch.setattr(
        "autolab.utils._detect_host_mode_with_probe",
        lambda: ("slurm", {"sinfo": "ok", "squeue": "ok"}),
    )

    def _resolve_remote_profile(repo_root, host_mode="", profile_name=""):
        calls["repo_root"] = repo_root
        calls["host_mode"] = host_mode
        return SimpleNamespace(
            name="cluster",
            mode="git_checkout",
            enabled_for_host_modes=("slurm",),
            login_host="cluster-login",
            remote_repo_root="/remote/repo",
            python_path="./venv/bin/python",
            bootstrap_command="./scripts/bootstrap.sh",
            submit_command="sbatch",
            host_detection=SimpleNamespace(require_commands=()),
            smoke_command="",
        )

    monkeypatch.setattr(
        "autolab.remote_profiles.resolve_remote_profile", _resolve_remote_profile
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.run_remote_command",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=kwargs.get("command", args[1] if len(args) > 1 else ""),
            returncode=0,
            stdout="ok",
            stderr="",
        ),
    )
    monkeypatch.setattr(
        "autolab.remote_profiles.ensure_remote_python",
        lambda profile, timeout_seconds=120.0: None,
    )

    exit_code = _cmd_remote_smoke(
        argparse.Namespace(profile="", state_file=str(state_path))
    )

    assert exit_code == 0
    assert calls["repo_root"] == repo
    assert calls["host_mode"] == "slurm"
