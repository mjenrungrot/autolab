from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path


def _copy_brownfield_canary(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    fixture = resources.files("autolab").joinpath("example_brownfield_canary")
    with resources.as_file(fixture) as fixture_root:
        shutil.copytree(fixture_root, repo)
    return repo


def _run_cli(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "autolab", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        env=merged_env,
    )


def _run_git(
    repo: Path, *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
        env=merged_env,
    )


def _assert_ok(result: subprocess.CompletedProcess[str], *, label: str) -> None:
    assert result.returncode == 0, (
        f"{label} failed with exit code {result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_brownfield_canary_fixture_runs_requested_cli_sequence(tmp_path: Path) -> None:
    repo = _copy_brownfield_canary(tmp_path)

    _assert_ok(_run_git(repo, "init"), label="git init")

    install_hooks = _run_cli(repo, "hooks", "install")
    _assert_ok(install_hooks, label="autolab hooks install")
    assert "post-commit" in install_hooks.stdout

    commit_env = {
        "GIT_AUTHOR_NAME": "Autolab Canary",
        "GIT_AUTHOR_EMAIL": "autolab-canary@example.invalid",
        "GIT_COMMITTER_NAME": "Autolab Canary",
        "GIT_COMMITTER_EMAIL": "autolab-canary@example.invalid",
    }
    empty_commit = _run_git(
        repo, "commit", "--allow-empty", "-m", "seed canary repo", env=commit_env
    )
    _assert_ok(empty_commit, label="git commit")

    checkpoint_index_path = repo / ".autolab" / "checkpoints" / "index.json"
    if checkpoint_index_path.exists():
        checkpoint_index = json.loads(checkpoint_index_path.read_text(encoding="utf-8"))
        assert any(
            entry.get("trigger") == "commit" and entry.get("stage") == "design"
            for entry in checkpoint_index.get("checkpoints", [])
        )

    sync = _run_cli(repo, "sync-scaffold", "--force")
    _assert_ok(sync, label="autolab sync-scaffold --force")
    assert "autolab sync-scaffold" in sync.stdout

    render = _run_cli(repo, "render", "--stage", "design", "--view", "context")
    _assert_ok(render, label="autolab render --stage design --view context")
    assert "iter_brownfield_canary" in render.stdout

    progress = _run_cli(repo, "progress")
    _assert_ok(progress, label="autolab progress")
    assert "autolab progress" in progress.stdout
    assert "recommended_next_command:" in progress.stdout

    docs_generate = _run_cli(repo, "docs", "generate", "--view", "all")
    _assert_ok(docs_generate, label="autolab docs generate --view all")
    assert "# Project View" in docs_generate.stdout
    assert "# Sidecar View" in docs_generate.stdout

    checkpoint_create = _run_cli(repo, "checkpoint", "create")
    _assert_ok(checkpoint_create, label="autolab checkpoint create")
    assert "autolab checkpoint create" in checkpoint_create.stdout

    reset = _run_cli(repo, "reset", "--to", "stage:design")
    _assert_ok(reset, label="autolab reset --to stage:design")
    assert "target: stage:design" in reset.stdout
    restored_contract = json.loads(
        (repo / ".autolab" / "plan_contract.json").read_text(encoding="utf-8")
    )
    assert {task["scope_kind"] for task in restored_contract["tasks"]} == {
        "experiment",
        "project_wide",
    }
    restored_approval = json.loads(
        (
            repo
            / "experiments"
            / "in_progress"
            / "iter_brownfield_canary"
            / "plan_approval.json"
        ).read_text(encoding="utf-8")
    )
    assert restored_approval["status"] == "approved"

    render_after_reset = _run_cli(
        repo, "render", "--stage", "design", "--view", "context"
    )
    _assert_ok(
        render_after_reset,
        label="autolab render --stage design --view context (after reset)",
    )
    assert "iter_brownfield_canary" in render_after_reset.stdout

    docs_after_reset = _run_cli(repo, "docs", "generate", "--view", "all")
    _assert_ok(docs_after_reset, label="autolab docs generate --view all (after reset)")
    assert "# Project View" in docs_after_reset.stdout

    policy_show = _run_cli(
        repo,
        "policy",
        "show",
        "--effective",
        "--stage",
        "implementation_review",
        "--scope",
        "project_wide",
        "--host",
        "slurm",
    )
    _assert_ok(
        policy_show,
        label="autolab policy show --effective --stage implementation_review --scope project_wide --host slurm",
    )
    assert "autolab policy show --effective" in policy_show.stdout
    assert "uat_required" in policy_show.stdout

    remote_doctor = _run_cli(repo, "remote", "doctor")
    _assert_ok(remote_doctor, label="autolab remote doctor")
    assert "profile: local_shared" in remote_doctor.stdout
    assert "status: ok" in remote_doctor.stdout

    verify = _run_cli(repo, "verify", "--stage", "implementation_review")
    _assert_ok(verify, label="autolab verify --stage implementation_review")
    assert "passed: True" in verify.stdout
