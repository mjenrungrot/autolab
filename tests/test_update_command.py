from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import autolab.update as update_module
from autolab.__main__ import _build_parser


def _completed_process(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["stub"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def test_parse_semver_accepts_stable_formats() -> None:
    assert update_module.parse_semver("v1.2.3") == (1, 2, 3)
    assert update_module.parse_semver("1.2.3") == (1, 2, 3)


def test_parse_semver_rejects_prerelease_and_invalid() -> None:
    with pytest.raises(ValueError):
        update_module.parse_semver("v1.2.3-rc1")
    with pytest.raises(ValueError):
        update_module.parse_semver("not-a-version")


def test_fetch_latest_stable_tag_selects_highest_semver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_output = "\n".join(
        (
            "hash1\trefs/tags/v1.2.0",
            "hash2\trefs/tags/v1.10.0",
            "hash3\trefs/tags/v1.2.3-rc1",
            "hash4\trefs/tags/v2.0.0",
            "hash5\trefs/tags/not-a-tag",
        )
    )
    captured: dict[str, object] = {}

    def _fake_run(*args, **kwargs) -> subprocess.CompletedProcess[str]:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _completed_process(stdout=git_output)

    monkeypatch.setattr(update_module.subprocess, "run", _fake_run)

    latest = update_module.fetch_latest_stable_tag(
        update_module.DEFAULT_RELEASE_REPO_URL
    )

    assert latest == "v2.0.0"
    assert captured["args"] == (
        [
            "git",
            "ls-remote",
            "--tags",
            "--refs",
            update_module.DEFAULT_RELEASE_REPO_URL,
        ],
    )


def test_build_git_install_spec_uses_exact_git_url() -> None:
    assert (
        update_module.build_git_install_spec(
            update_module.DEFAULT_RELEASE_REPO_URL,
            "v1.2.3",
        )
        == "git+https://github.com/mjenrungrot/autolab.git@v1.2.3"
    )


def test_update_command_is_listed_in_help() -> None:
    help_text = _build_parser().format_help()
    assert "update" in help_text


def test_fetch_latest_stable_tag_errors_when_no_stable_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    git_output = "\n".join(
        (
            "hash1\trefs/tags/v1.2.3-rc1",
            "hash2\trefs/tags/not-a-tag",
        )
    )
    monkeypatch.setattr(
        update_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed_process(stdout=git_output),
    )

    with pytest.raises(RuntimeError, match="no stable release tags found"):
        update_module.fetch_latest_stable_tag(update_module.DEFAULT_RELEASE_REPO_URL)


def test_fetch_latest_stable_tag_surfaces_git_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        update_module.subprocess,
        "run",
        lambda *args, **kwargs: _completed_process(
            returncode=128, stderr="fatal: boom"
        ),
    )

    with pytest.raises(RuntimeError, match="unable to query release tags"):
        update_module.fetch_latest_stable_tag(update_module.DEFAULT_RELEASE_REPO_URL)


def test_run_update_returns_noop_when_up_to_date(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(update_module, "get_installed_version", lambda: (1, 2, 3))
    monkeypatch.setattr(
        update_module,
        "fetch_latest_stable_tag",
        lambda _repo_url: "v1.2.3",
    )
    monkeypatch.setattr(
        update_module,
        "run_pip_upgrade",
        lambda _spec: (_ for _ in ()).throw(AssertionError("unexpected pip install")),
    )

    result = update_module.run_update(tmp_path)

    assert result.current_version == "1.2.3"
    assert result.latest_tag == "v1.2.3"
    assert result.upgraded is False
    assert result.synced_scaffold is False
    assert result.sync_skipped_reason is None


def test_run_update_errors_when_pip_upgrade_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(update_module, "get_installed_version", lambda: (1, 2, 2))
    monkeypatch.setattr(
        update_module,
        "fetch_latest_stable_tag",
        lambda _repo_url: "v1.2.3",
    )
    monkeypatch.setattr(
        update_module,
        "run_pip_upgrade",
        lambda _spec: _completed_process(returncode=1, stderr="pip failed"),
    )

    with pytest.raises(RuntimeError, match="pip install failed"):
        update_module.run_update(tmp_path)


def test_run_update_inside_repo_runs_scaffold_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".autolab").mkdir(parents=True)

    monkeypatch.setattr(update_module, "get_installed_version", lambda: (1, 0, 0))
    monkeypatch.setattr(
        update_module,
        "fetch_latest_stable_tag",
        lambda _repo_url: "v1.0.1",
    )
    monkeypatch.setattr(
        update_module,
        "run_pip_upgrade",
        lambda _spec: _completed_process(returncode=0),
    )

    captured: dict[str, Path | None] = {"cwd": None}

    def _fake_sync(*, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
        captured["cwd"] = cwd
        return _completed_process(returncode=0)

    monkeypatch.setattr(update_module, "run_scaffold_sync", _fake_sync)

    result = update_module.run_update(repo)

    assert result.upgraded is True
    assert result.synced_scaffold is True
    assert result.sync_skipped_reason is None
    assert captured["cwd"] == repo.resolve()


def test_run_update_outside_repo_skips_scaffold_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setattr(update_module, "get_installed_version", lambda: (1, 0, 0))
    monkeypatch.setattr(
        update_module,
        "fetch_latest_stable_tag",
        lambda _repo_url: "v1.0.1",
    )
    monkeypatch.setattr(
        update_module,
        "run_pip_upgrade",
        lambda _spec: _completed_process(returncode=0),
    )

    def _unexpected_sync(
        *, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"run_scaffold_sync should not be called (cwd={cwd})")

    monkeypatch.setattr(update_module, "run_scaffold_sync", _unexpected_sync)

    result = update_module.run_update(workspace)

    assert result.upgraded is True
    assert result.synced_scaffold is False
    assert result.sync_skipped_reason == "outside repo"
